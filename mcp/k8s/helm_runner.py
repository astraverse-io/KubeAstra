"""Read-only Helm command runner (local + SSH), mirroring KubectlRunner.

Helm-specific by design (not a generic shell runner): only allowlisted read-only
subcommands run, arguments are structured lists (never raw command strings),
``shell=True`` is never used locally, and the SSH path reuses the active kubectl
SSH connection with shell-safe quoting. This keeps Helm on the *same target* as
kubectl without a second connection.
"""
import contextvars
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from config.settings import settings
from k8s.kubectl_runner import get_runner

logger = logging.getLogger(__name__)

# Read-only helm subcommands actually used by the v1 tools. Kept intentionally
# narrow: `show` can fetch external chart data and `env` exposes local config
# paths, so they are excluded until a tool needs them. Everything not listed
# (install / upgrade / uninstall / rollback / repo mutation / push / ...) is
# rejected — this runner never writes.
_READ_ONLY_HELM = {"version", "list", "status", "history", "get"}
# Allowed `helm get <X>` subcommands (all read-only).
_READ_ONLY_GET = {"all", "values", "manifest", "notes", "hooks", "metadata"}


class HelmError(Exception):
    """Raised when a helm command fails or is unavailable."""

    def __init__(self, message: str, returncode: int = -1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class HelmResult:
    stdout: str
    stderr: str
    returncode: int
    command: List[str]
    duration_seconds: float
    truncated: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0


def _validate_read_only(args: List[str]) -> None:
    """Reject any non-read-only helm command. Raises ValueError if forbidden."""
    if not args:
        raise ValueError("Empty helm command")
    sub = args[0].lower()
    if sub not in _READ_ONLY_HELM:
        raise ValueError(
            f"Forbidden helm operation: '{sub}'. Only read-only helm commands "
            f"({', '.join(sorted(_READ_ONLY_HELM))}) are allowed."
        )
    if sub == "get":
        if len(args) < 2 or args[1].lower() not in _READ_ONLY_GET:
            raise ValueError(
                "Only 'helm get values|manifest|notes|hooks|all|metadata' are allowed."
            )


class HelmRunner:
    """Runs read-only helm commands locally or over an existing SSH connection."""

    def __init__(
        self,
        kubeconfig_path: Optional[str] = None,
        context: Optional[str] = None,
        ssh_runner=None,
    ):
        self.timeout = settings.kubectl_timeout_seconds
        self.max_output_bytes = settings.max_output_bytes
        self.kubeconfig_path = kubeconfig_path
        self.context = context
        self.ssh_runner = ssh_runner

    def _build(self, args: List[str], namespace: Optional[str]) -> List[str]:
        parts = ["helm"] + list(args)
        if namespace:
            parts.extend(["-n", namespace])
        if self.context:
            parts.extend(["--kube-context", self.context])
        # Over SSH the remote kubeconfig is the admin default; only pass an
        # explicit --kubeconfig in the local path.
        if self.ssh_runner is None and self.kubeconfig_path:
            parts.extend(["--kubeconfig", str(self.kubeconfig_path)])
        return parts

    def run(self, args: List[str], namespace: Optional[str] = None) -> HelmResult:
        _validate_read_only(args)
        parts = self._build(args, namespace)
        start = datetime.now()

        if self.ssh_runner is not None:
            try:
                stdout, stderr, rc = self.ssh_runner.run_shell_command(parts, timeout=self.timeout)
            except Exception as exc:  # SSH/timeout/connection errors -> graceful HelmError
                raise HelmError(f"helm over SSH failed: {exc}", -1, str(exc))
        else:
            try:
                proc = subprocess.run(
                    parts, capture_output=True, text=True,
                    timeout=self.timeout, check=False,
                )
            except subprocess.TimeoutExpired:
                raise HelmError(f"helm command timed out after {self.timeout}s", -1, "timeout")
            except FileNotFoundError:
                raise HelmError("helm binary not found", -1, "helm: command not found")
            stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode

        duration = (datetime.now() - start).total_seconds()
        truncated = False
        if len(stdout) > self.max_output_bytes:
            stdout = stdout[:self.max_output_bytes] + "\n[... output truncated ...]"
            truncated = True
        return HelmResult(stdout, stderr, rc, parts, duration, truncated)


# ── Per-request helm runner context (mirrors kubectl_runner) ──────────────────
helm_runner_ctx: contextvars.ContextVar = contextvars.ContextVar(
    "helm_runner", default=None
)


def set_helm_runner(runner) -> contextvars.Token:
    """Override the helm runner for the current context (used by tests)."""
    return helm_runner_ctx.set(runner)


def get_helm_runner() -> HelmRunner:
    """Return a HelmRunner targeting the same environment as the active kubectl runner.

    Over SSH, reuse the kubectl SSH connection so helm runs on the remote node.
    Locally, mirror the active kubectl runner's kubeconfig/context.
    """
    override = helm_runner_ctx.get()
    if override is not None:
        return override

    active = get_runner()
    if hasattr(active, "run_shell_command"):  # SSHKubectlRunner
        return HelmRunner(context=getattr(active, "context", None), ssh_runner=active)
    kubeconfig = getattr(active, "kubeconfig_path", None)
    return HelmRunner(
        kubeconfig_path=str(kubeconfig) if kubeconfig else None,
        context=getattr(active, "context", None),
    )
