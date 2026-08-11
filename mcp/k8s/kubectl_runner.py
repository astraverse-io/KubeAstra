"""Safe kubectl command runner with timeout and output limits."""

import contextvars
import errno
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from config.settings import get_settings

from . import binaries

logger = logging.getLogger(__name__)

_AUDIT_ENTRY_MAX_BYTES = 4095
_audit_warning_lock = threading.Lock()
_audit_warning_state: dict[str, dict[str, float | int | None]] = {}
_audit_config_logged: set[str] = set()


def _audit_metric_failure(category: str) -> None:
    try:
        from metrics import audit_write_failures_total
        audit_write_failures_total.labels(category=category).inc()
    except Exception:
        pass


def _audit_metric_rotation() -> None:
    try:
        from metrics import audit_rotations_total
        audit_rotations_total.inc()
    except Exception:
        pass


def _audit_failure_category(exc: BaseException) -> str:
    code = getattr(exc, "errno", None)
    if code in (errno.EACCES, errno.EPERM, errno.EROFS):
        return "permission"
    if code in (errno.ENOSPC, errno.EDQUOT):
        return "disk_full"
    if code in (errno.ENOENT, errno.ENOTDIR):
        return "path_missing"
    return "other"


def _warn_audit_failure(
    category: str,
    exc: BaseException,
    *,
    interval_seconds: float,
) -> None:
    """Emit immediately, then at most once per interval with suppression count."""
    now = time.monotonic()
    with _audit_warning_lock:
        state = _audit_warning_state.setdefault(
            category, {"last": None, "suppressed": 0}
        )
        last = state["last"]
        if last is not None and now - last < interval_seconds:
            state["suppressed"] = int(state["suppressed"]) + 1
            return
        suppressed = int(state["suppressed"])
        state["last"] = now
        state["suppressed"] = 0
    logger.warning(
        "audit_write_failed category=%s suppressed=%d error=%s",
        category,
        suppressed,
        exc,
    )


def _bounded_audit_entry(entry: str) -> bytes:
    raw = (entry.rstrip("\n") + "\n").encode("utf-8", errors="replace")
    if len(raw) <= _AUDIT_ENTRY_MAX_BYTES:
        return raw
    suffix = b"...[audit entry truncated]\n"
    return raw[: _AUDIT_ENTRY_MAX_BYTES - len(suffix)] + suffix


# The audit log is created 0600, not 0640. It records every kubectl command
# this process ran, arguments included — namespaces, resource names, the shape
# of the cluster and, in server mode, which operator asked for what. The group
# bit bought nothing: nothing in the deployment reads this file as a group, and
# a container image with an unrelated service in the same group is exactly the
# case where a read-only audit trail turns into reconnaissance.


def _rotate_audit_file(path: Path, max_bytes: int) -> None:
    """Rotate by atomic rename. Appenders remain lock-free and open per entry."""
    try:
        if path.stat().st_size < max_bytes:
            return
    except FileNotFoundError:
        return

    lock_path = path.with_name(path.name + ".rotate.lock")
    lock_fd: Optional[int] = None
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError):
            return

        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if size < max_bytes:
            return

        rotated = path.with_name(path.name + ".1")
        os.replace(path, rotated)
        marker = _bounded_audit_entry(
            f"{datetime.now().isoformat()} | AUDIT_LOG_ROTATED | previous_size={size}"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, marker)
        finally:
            os.close(fd)
        _audit_metric_rotation()
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _append_audit_entry(path: Path, entry: str, max_bytes: int) -> None:
    """Append one bounded entry with one O_APPEND write and retry ENOENT once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_audit_file(path, max_bytes)
    payload = _bounded_audit_entry(entry)
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            return
        except FileNotFoundError:
            if attempt == 0:
                time.sleep(0.005)
                continue
            raise


def _log_audit_configuration(path: Path) -> None:
    path_key = str(path.absolute())
    with _audit_warning_lock:
        if path_key in _audit_config_logged:
            return
        _audit_config_logged.add(path_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_stat = path.parent.stat()
        parent_mode = oct(parent_stat.st_mode & 0o777)
        writable = os.access(path.parent, os.W_OK)
        logger.info(
            "audit_log resolved=%s uid=%s gid=%s parent_mode=%s writable=%s",
            path.resolve(),
            os.geteuid(),
            os.getegid(),
            parent_mode,
            str(writable).lower(),
        )
    except Exception as exc:
        logger.warning(
            "audit_log resolved=%s uid=%s gid=%s parent_unavailable=true error=%s",
            path.absolute(),
            os.geteuid(),
            os.getegid(),
            exc,
        )


class KubectlError(Exception):
    """Raised when kubectl command fails."""
    
    def __init__(self, message: str, returncode: int, stderr: str):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class KubectlTimeoutError(KubectlError):
    """Raised when kubectl command times out."""
    pass


@dataclass
class KubectlResult:
    """Result from kubectl command execution."""
    
    stdout: str
    stderr: str
    returncode: int
    command: List[str]
    duration_seconds: float
    truncated: bool = False
    
    @property
    def success(self) -> bool:
        """Check if command succeeded."""
        return self.returncode == 0
    
    def raise_for_status(self) -> None:
        """Raise exception if command failed."""
        if not self.success:
            raise KubectlError(
                f"kubectl command failed: {' '.join(self.command)}",
                self.returncode,
                self.stderr
            )


class KubectlRunner:
    """Safe kubectl command runner."""
    
    def __init__(
        self,
        kubeconfig_path: Optional[str] = None,
        context: Optional[str] = None,
    ):
        settings = get_settings()
        self.timeout = settings.kubectl_timeout_seconds
        self.max_output_bytes = settings.max_output_bytes
        self.kubeconfig_path = Path(kubeconfig_path).expanduser().resolve() if kubeconfig_path else settings.kubeconfig_path_resolved
        self.context = context
        self.audit_enabled = settings.enable_audit_log
        self.audit_log_path = Path(settings.audit_log_path)
        self.audit_log_max_bytes = settings.audit_log_max_bytes
        self.audit_warning_interval_seconds = settings.audit_log_warning_interval_seconds
        if self.audit_enabled:
            _log_audit_configuration(self.audit_log_path)
    
    def run(
        self,
        args: List[str],
        namespace: Optional[str] = None,
        capture_output: bool = True,
        max_output: Optional[int] = None,
        stdin_data: Optional[str] = None,
    ) -> KubectlResult:
        """
        Run kubectl command safely.
        
        Args:
            args: Command arguments (e.g., ["get", "pods"])
            namespace: Optional namespace to inject
            capture_output: Whether to capture stdout/stderr
            stdin_data: Optional string to pass to standard input
            
        Returns:
            KubectlResult with command output
            
        Raises:
            KubectlError: If command fails
            KubectlTimeoutError: If command times out
        """
        # SAFETY: Validate that command is read-only
        self._validate_read_only_command(args)
        
        # Build command. Resolved rather than bare: a GUI launch gets a
        # minimal PATH that contains no Kubernetes tooling. See k8s.binaries.
        cmd = [binaries.kubectl()]
        
        # Add kubeconfig if configured
        if self.kubeconfig_path:
            cmd.extend(["--kubeconfig", str(self.kubeconfig_path)])

        # Add context if configured
        if self.context:
            cmd.extend(["--context", self.context])
        
        # Add namespace if provided
        if namespace:
            cmd.extend(["--namespace", namespace])
        
        # Add user arguments
        cmd.extend(args)
        
        # Log command for audit
        self._audit_log("EXECUTE", cmd, namespace)
        
        start_time = datetime.now()
        truncated = False
        
        try:
            # Run command with timeout
            # NEVER use shell=True for security
            result = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=capture_output,
                text=True,
                timeout=self.timeout,
                check=False  # We handle errors manually
            )
            
            duration = (datetime.now() - start_time).total_seconds()
            
            # Truncate output if too large
            stdout = result.stdout
            stderr = result.stderr
            limit = max_output if max_output is not None else self.max_output_bytes

            if len(stdout) > limit:
                stdout = stdout[:limit] + "\n[... output truncated ...]"
                truncated = True
            
            if len(stderr) > self.max_output_bytes:
                stderr = stderr[:self.max_output_bytes] + "\n[... output truncated ...]"
                truncated = True
            
            kubectl_result = KubectlResult(
                stdout=stdout,
                stderr=stderr,
                returncode=result.returncode,
                command=cmd,
                duration_seconds=duration,
                truncated=truncated
            )
            
            # Audit log result
            status = "SUCCESS" if kubectl_result.success else "FAILED"
            self._audit_log(status, cmd, namespace, duration)
            
            return kubectl_result
            
        except subprocess.TimeoutExpired as e:
            duration = (datetime.now() - start_time).total_seconds()
            self._audit_log("TIMEOUT", cmd, namespace, duration)
            
            raise KubectlTimeoutError(
                f"kubectl command timed out after {self.timeout}s: {' '.join(cmd)}",
                -1,
                f"Command exceeded timeout of {self.timeout} seconds"
            )
        
        except Exception as e:
            duration = (datetime.now() - start_time).total_seconds()
            self._audit_log("ERROR", cmd, namespace, duration, str(e))
            raise
    
    def _validate_read_only_command(self, args: List[str]) -> None:
        """
        Validate that kubectl command is read-only.
        
        Raises:
            ValueError: If command contains write operations
        """
        if not args:
            return
        
        # List of forbidden write operations
        WRITE_OPERATIONS = {
            "create", "apply", "patch", "delete", "edit", "replace",
            "scale", "autoscale", "set", "label", "annotate",
            "expose", "run", "exec", "attach", "port-forward", "proxy",
            "cp", "drain", "cordon", "uncordon", "taint", "top"
        }
        
        # SPECIAL CASE: "rollout" is forbidden EXCEPT "rollout status" which is read-only
        ALLOWED_ROLLOUT_SUBCOMMANDS = {"status", "history"}
        
        command = args[0].lower() if args else ""
        
        # Check for rollout command
        if command == "rollout":
            if len(args) < 2 or args[1].lower() not in ALLOWED_ROLLOUT_SUBCOMMANDS:
                raise ValueError(
                    f"Forbidden kubectl rollout operation. "
                    f"Only 'rollout status' and 'rollout history' are allowed (read-only)."
                )
        elif command in WRITE_OPERATIONS:
            raise ValueError(
                f"Forbidden kubectl operation: '{command}'. "
                f"Only read-only operations are allowed (get, describe, logs, etc.)"
            )
        
        # Additional safety: check for dangerous flags
        DANGEROUS_FLAGS = {"--all-namespaces", "--all", "-A"}
        for arg in args:
            if arg in DANGEROUS_FLAGS:
                logger.warning(f"Potentially dangerous flag detected: {arg}")
                # Allow but log for audit purposes
    
    def run_json(
        self,
        args: List[str],
        namespace: Optional[str] = None
    ) -> dict:
        """
        Run kubectl command and parse JSON output.

        Uses a large output cap (MAX_JSON_BYTES) so that JSON is
        never truncated mid-stream — a truncated document is not "less data",
        it is unparseable. If the cap *is* hit, this raises rather than
        letting json.loads fail: the resulting JSONDecodeError blamed kubectl
        for corruption this code had introduced, which sent at least one
        debugging session in entirely the wrong direction.

        Individual callers are responsible for limiting the number of items
        they return to the user.

        Args:
            args: Command arguments
            namespace: Optional namespace

        Returns:
            Parsed JSON as dict

        Raises:
            KubectlError: If command fails or output is not valid JSON
        """
        # Ensure JSON output format
        if "-o" not in args and "--output" not in args:
            args = args + ["-o", "json"]

        json_max_bytes = get_settings().max_json_bytes

        result = self.run(args, namespace=namespace, max_output=json_max_bytes)
        result.raise_for_status()

        # Check before parsing. `run` already told us it cut the output short;
        # parsing it anyway produces a JSONDecodeError that reads as though
        # kubectl emitted malformed JSON, when in fact we truncated valid
        # JSON. Say what actually happened, and what to do about it.
        if result.truncated:
            megabytes = json_max_bytes / (1024 * 1024)
            raise KubectlError(
                f"kubectl returned more than {megabytes:.0f} MB of JSON, so the "
                f"output was cut short and cannot be parsed. Nothing is wrong "
                f"with the cluster — the query is simply too broad. Narrow it "
                f"with a namespace or a label selector, or raise "
                f"MAX_JSON_BYTES if this size is expected.",
                result.returncode,
                result.stderr,
            )

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise KubectlError(
                f"Failed to parse kubectl JSON output: {e}",
                result.returncode,
                result.stderr
            )
    
    def _audit_log(
        self,
        status: str,
        command: List[str],
        namespace: Optional[str],
        duration: Optional[float] = None,
        error: Optional[str] = None
    ) -> None:
        """Write audit log entry."""
        if not self.audit_enabled:
            return
        
        try:
            timestamp = datetime.now().isoformat()
            cmd_str = " ".join(command)
            
            log_entry = f"{timestamp} | {status} | ns={namespace or 'N/A'} | "
            if duration is not None:
                log_entry += f"duration={duration:.2f}s | "
            log_entry += f"cmd={cmd_str}"
            
            if error:
                log_entry += f" | error={error}"
            
            _append_audit_entry(
                self.audit_log_path,
                log_entry,
                self.audit_log_max_bytes,
            )
                
        except Exception as e:
            category = _audit_failure_category(e)
            _audit_metric_failure(category)
            _warn_audit_failure(
                category,
                e,
                interval_seconds=self.audit_warning_interval_seconds,
            )


# Global runner instance (used when no SSH session is active)
kubectl = KubectlRunner()

# ── Per-request runner context ────────────────────────────────────────────────
# Stores an override runner for the current asyncio Task / thread.
# When the ContextVar is empty (default), get_runner() returns the global
# local kubectl instance.  chat.py sets this to an SSHKubectlRunner for
# requests that arrive with SSH credentials.
runner_ctx: contextvars.ContextVar = contextvars.ContextVar(
    "kubectl_runner", default=None
)


def get_runner():
    """Return the active runner for this request context.

    Falls back to the global local KubectlRunner when no SSH runner has
    been set for the current asyncio task / thread.
    """
    return runner_ctx.get() or kubectl


def set_runner(runner) -> contextvars.Token:
    """Override the runner for the current request context.

    Returns a Token that must be passed to runner_ctx.reset() in a
    finally block to restore the previous value when the request ends.

    Example:
        token = set_runner(ssh_runner)
        try:
            ...
        finally:
            runner_ctx.reset(token)
    """
    return runner_ctx.set(runner)
