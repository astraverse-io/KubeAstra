"""Validators for proposed local edits (Phase 2, PR 2).

Every edit the desktop agent proposes is machine-checked before a human sees it
(design §5). ``validate_edit(target, before, after, root=...)`` runs the
applicable checks and returns a ``ValidationResult``:

- ``ok``        — no check failed. A failing edit never becomes a pending write;
                  the agent gets the failures back to self-correct.
- ``validated`` — no *applicable* check was skipped. When False the UI stamps the
                  diff **UNVALIDATED** with ``unvalidated_reason`` (design §5:
                  never silently skipped — e.g. kubeconform not installed).

Checks judge what the edit **introduced** (spec D4): a problem present both
before and after is a ``warn`` (pre-existing), so one old violation in a file
doesn't block every unrelated edit to it.

**No network before approval.** Validation runs on content no human has
approved yet, so it must not be an exfiltration channel: an edit that
introduces a remote kustomize resource fails without running kustomize; a
kustomization tree that already references remote resources skips the build;
and kustomize/helm run with outbound HTTP(S) and git transports disabled.
kubeconform is the one exception — it fetches JSON schemas from its fixed,
built-in schema location, which the edit cannot redirect.

Builds run on a temporary copy of the granted folder (deny-listed files and
heavy directories excluded, size-capped); the user's files are never touched.

See internal_docs/features/DESKTOP_AGENT_PHASE2_SPEC.md §2 (PR 2).
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

import desktop_folders as df

DIFF_BUDGET_LINES = 200
_TIMEOUT_SECONDS = 20
RC_TIMEOUT = 124
RC_NOT_FOUND = 127

_KUSTOMIZATION_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")
# Keys whose list entries kustomize resolves as paths or URLs. generators /
# transformers / validators can name whole kustomization directories (or
# remote URLs), just like resources.
_KUST_REF_KEYS = ("resources", "bases", "components", "crds",
                  "generators", "transformers", "validators")
_PRUNE_DIRS = frozenset({
    ".git", "node_modules", ".terraform", "vendor", "__pycache__", ".venv", "venv",
    ".ssh", ".aws", ".kube",
})
_COPY_MAX_FILES = 3000
_COPY_MAX_BYTES = 30 * 1024 * 1024
_KUST_WALK_CAP = 200
_DETAIL_CAP = 300


# ── result types ──────────────────────────────────────────────────────────────

@dataclass
class Check:
    name: str
    status: str                  # pass | fail | warn | skipped
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class ValidationResult:
    ok: bool
    validated: bool
    checks: list[Check] = field(default_factory=list)
    unvalidated_reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "validated": self.validated,
            "unvalidated_reason": self.unvalidated_reason,
            "checks": [c.to_dict() for c in self.checks],
        }


def _result(checks: list[Check]) -> ValidationResult:
    skipped = [c for c in checks if c.status == "skipped"]
    return ValidationResult(
        ok=not any(c.status == "fail" for c in checks),
        validated=not skipped,
        checks=checks,
        unvalidated_reason="; ".join(f"{c.name}: {c.detail}" for c in skipped) or None,
    )


# ── seams (tests patch these) ─────────────────────────────────────────────────

def _found(tool: str) -> Optional[str]:
    """Absolute path to a validator binary, or None if it isn't installed."""
    try:
        from k8s import binaries
        return binaries.found(tool)
    except ImportError:
        return shutil.which(tool)


def _run(cmd: list[str], *, cwd: Optional[str] = None, env: Optional[dict] = None,
         timeout: int = _TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """List-form subprocess (never a shell). Returns (rc, stdout, stderr);
    RC_TIMEOUT / RC_NOT_FOUND instead of raising."""
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return RC_TIMEOUT, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return RC_NOT_FOUND, "", "binary not found"


def _offline_env() -> dict:
    """Environment for kustomize/helm: outbound HTTP(S) points at a closed local
    port and git may only use local transports, so a build can't fetch (or leak
    data to) anything remote."""
    env = dict(os.environ)
    dead = "http://127.0.0.1:9"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[k] = dead
    env.pop("NO_PROXY", None)
    env.pop("no_proxy", None)
    env["GIT_ALLOW_PROTOCOL"] = "file"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


# ── helpers ───────────────────────────────────────────────────────────────────

def _first_line(*texts: str) -> str:
    for t in texts:
        for line in (t or "").splitlines():
            if line.strip():
                return line.strip()[:_DETAIL_CAP]
    return ""


def _parse(text: Optional[str]) -> tuple[bool, list, str]:
    if text is None:
        return True, [], ""
    try:
        return True, [d for d in yaml.safe_load_all(text) if d is not None], ""
    except yaml.YAMLError as exc:
        return False, [], _first_line(str(exc))


def _is_under(p: Path, d: Path) -> bool:
    return p.parts[: len(d.parts)] == d.parts


def _ancestors_within(target: Path, root: Path) -> Iterable[Path]:
    """target's directory, then each parent up to and including root."""
    d = target.parent
    while _is_under(d, root):
        yield d
        if d == root:
            return
        d = d.parent


def _chart_dir(target: Path, root: Path) -> Optional[Path]:
    for d in _ancestors_within(target, root):
        if (d / "Chart.yaml").is_file():
            return d
    return None


def _kustomize_dir(target: Path, root: Path) -> Optional[Path]:
    for d in _ancestors_within(target, root):
        if any((d / n).is_file() for n in _KUSTOMIZATION_NAMES):
            return d
    return None


def _status_from(before: set, after: set, *, warn_extra: Iterable[str] = ()) -> tuple[str, str]:
    introduced = after - before
    existing = after & before
    warns = sorted(existing) + sorted(set(warn_extra))
    if introduced:
        return "fail", "; ".join(sorted(introduced))[:_DETAIL_CAP]
    if warns:
        return "warn", ("pre-existing / advisory: " + "; ".join(warns))[:_DETAIL_CAP]
    return "pass", ""


# ── manifest checks (pure) ────────────────────────────────────────────────────

def _manifests(docs: list, target: Path) -> list[dict]:
    if target.name in _KUSTOMIZATION_NAMES:
        return []
    return [d for d in docs if isinstance(d, dict) and "kind" in d and d.get("kind") != "Kustomization"]


def _ident(doc: dict) -> str:
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    return f"{doc.get('kind', '?')}/{meta.get('name') or meta.get('generateName') or '?'}"


def _shape_violations(docs: list[dict]) -> set[str]:
    out = set()
    for d in docs:
        kind = d.get("kind", "?")
        if not d.get("apiVersion"):
            out.add(f"{kind}: missing apiVersion")
        meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
        if not (meta.get("name") or meta.get("generateName")):
            out.add(f"{kind}: missing metadata.name")
    return out


def _pod_specs(o):
    if isinstance(o, dict):
        if isinstance(o.get("containers"), list):
            yield o
        for v in o.values():
            yield from _pod_specs(v)
    elif isinstance(o, list):
        for v in o:
            yield from _pod_specs(v)


def _bad_image(image: str) -> bool:
    """`:latest` or no tag at all. A digest pin is always fine. The last path
    segment carries the tag, so a registry port (`host:5000/app`) isn't a tag."""
    if "@" in image:
        return False
    last = image.rsplit("/", 1)[-1]
    if ":" not in last:
        return True
    return last.rsplit(":", 1)[1] == "latest"


def _policy_findings(docs: list[dict]) -> tuple[set[str], set[str]]:
    """(violations, advisories). Violations fail when introduced; advisories
    (hostPath) are always a warn."""
    violations, advisories = set(), set()
    for d in docs:
        who = _ident(d)
        for spec in _pod_specs(d):
            containers = []
            for key in ("containers", "initContainers", "ephemeralContainers"):
                if isinstance(spec.get(key), list):
                    containers += [c for c in spec[key] if isinstance(c, dict)]
            for c in containers:
                cname = c.get("name", "?")
                sc = c.get("securityContext") if isinstance(c.get("securityContext"), dict) else {}
                if sc.get("privileged") is True:
                    violations.add(f"{who}: container {cname} is privileged")
                if sc.get("allowPrivilegeEscalation") is True:
                    violations.add(f"{who}: container {cname} allows privilege escalation")
                img = c.get("image")
                if isinstance(img, str) and _bad_image(img):
                    violations.add(f"{who}: container {cname} image {img} is :latest or untagged")
            for v in spec.get("volumes") or []:
                if isinstance(v, dict) and "hostPath" in v:
                    advisories.add(f"{who}: hostPath volume {v.get('name', '?')}")
    return violations, advisories


def _check_diff_budget(before: str, after: str) -> Check:
    changed = sum(
        1 for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++") and not line.startswith("---")
    )
    if changed > DIFF_BUDGET_LINES:
        return Check("diff_budget", "fail", f"{changed} changed lines (budget {DIFF_BUDGET_LINES})")
    return Check("diff_budget", "pass", f"{changed} changed lines")


# ── remote-reference guard ────────────────────────────────────────────────────

_REMOTE_HOST_RE = re.compile(r"^(github\.com|gitlab\.com|bitbucket\.org)/", re.IGNORECASE)


def _is_remote_ref(ref: str) -> bool:
    s = (ref or "").strip()
    return "://" in s or s.startswith("git@") or "?ref=" in s or bool(_REMOTE_HOST_RE.match(s))


def _kust_refs(text: Optional[str]) -> tuple[set[str], list[str]]:
    """(remote refs, local refs) named by a kustomization's resource-ish keys.
    ``helmCharts`` counts as remote (inflation pulls chart repos)."""
    ok, docs, _ = _parse(text)
    remote, local = set(), []
    if not ok:
        return remote, local
    for d in docs:
        if not isinstance(d, dict):
            continue
        if d.get("helmCharts"):
            remote.add("helmCharts")
        for key in _KUST_REF_KEYS:
            for entry in d.get(key) or []:
                if not isinstance(entry, str):
                    continue
                (remote.add(entry) if _is_remote_ref(entry) else local.append(entry))
    return remote, local


def _read_kustomization(d: Path) -> Optional[str]:
    for n in _KUSTOMIZATION_NAMES:
        p = d / n
        if p.is_file() and not p.is_symlink():
            try:
                return p.read_text(errors="replace")
            except OSError:
                return None
    return None


def _reachable_remote_refs(kdir: Path, root: Path) -> set[str]:
    """Remote refs anywhere in the kustomization tree reachable from ``kdir``
    (following local directory references that stay inside ``root``)."""
    seen, queue, remote = set(), [kdir], set()
    while queue and len(seen) < _KUST_WALK_CAP:
        d = queue.pop()
        if d in seen:
            continue
        seen.add(d)
        r, local = _kust_refs(_read_kustomization(d))
        remote |= r
        for entry in local:
            nxt = (d / entry).resolve()
            if nxt.is_dir() and df._contained(nxt, root):
                queue.append(nxt)
    return remote


# ── temp copy of the granted folder ───────────────────────────────────────────

def _copy_tree(src_root: Path, dest: Path) -> tuple[bool, str]:
    """Copy a granted folder for a build: regular files only (no symlinks),
    deny-listed files and heavy dirs excluded, capped. (False, reason) if too big."""
    files = total = 0
    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
        dirnames[:] = [n for n in dirnames if n.lower() not in _PRUNE_DIRS]
        here = Path(dirpath)
        rel = here.parts[len(src_root.parts):]
        (dest.joinpath(*rel)).mkdir(parents=True, exist_ok=True)
        for name in filenames:
            p = here / name
            if p.is_symlink() or not p.is_file() or df.is_denied(p, src_root):
                continue
            size = p.stat().st_size
            files += 1
            total += size
            if files > _COPY_MAX_FILES or total > _COPY_MAX_BYTES:
                return False, "folder too large to copy for a build check"
            shutil.copy2(p, dest.joinpath(*rel, name))
    return True, ""


def _rel_parts(p: Path, root: Path) -> tuple:
    return p.parts[len(root.parts):]


def _place(copy_root: Path, rel: tuple, text: Optional[str]) -> Path:
    t = copy_root.joinpath(*rel)
    if text is None:
        if t.exists():
            t.unlink()
    else:
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(text.encode("utf-8"))
    return t


def _build_check(name: str, cmd_for, *, copy_root: Path, rel: tuple,
                 before: Optional[str], after: str) -> Check:
    """Run a build against the after-state; on failure, re-run on the before-state
    to tell an introduced failure (fail) from a pre-existing one (warn)."""
    _place(copy_root, rel, after)
    rc, out, err = _run(cmd_for(), env=_offline_env())
    if rc in (RC_TIMEOUT, RC_NOT_FOUND):
        return Check(name, "skipped", _first_line(err, out) or "could not run")
    if rc == 0:
        return Check(name, "pass")
    after_err = _first_line(err, out)
    _place(copy_root, rel, before)
    rc_b, out_b, err_b = _run(cmd_for(), env=_offline_env())
    if rc_b not in (0, RC_TIMEOUT, RC_NOT_FOUND):
        return Check(name, "warn", f"pre-existing build failure: {after_err}")
    return Check(name, "fail", after_err or "build failed")


# ── binary-backed checks ──────────────────────────────────────────────────────

def _check_kustomize(kdir: Path, target: Path, before: Optional[str], after: str,
                     root: Path) -> Check:
    name = "kustomize_build"
    if target.name in _KUSTOMIZATION_NAMES:
        remote_after, local_after = _kust_refs(after)
        remote_before, local_before = _kust_refs(before)
        introduced = remote_after - remote_before
        if introduced:
            return Check(name, "fail",
                         "edit introduces remote resource(s), not allowed in agent edits: "
                         + ", ".join(sorted(introduced))[:_DETAIL_CAP])
        # The build runs on a temp copy of the grant; a new reference escaping it
        # would resolve against the user's real filesystem. Refuse before running.
        escaping = sorted(
            ref for ref in set(local_after) - set(local_before)
            if os.path.isabs(ref) or not df._contained((target.parent / ref).resolve(), root)
        )
        if escaping:
            return Check(name, "fail",
                         "edit references path(s) outside the granted folder: "
                         + ", ".join(escaping)[:_DETAIL_CAP])
    if _reachable_remote_refs(kdir, root):
        return Check(name, "skipped",
                     "kustomization references remote resources, which are not fetched during validation")
    kubectl = _found("kubectl")
    if not kubectl:
        return Check(name, "skipped", "kubectl not installed")
    with tempfile.TemporaryDirectory(prefix="kubeastra-validate-") as td:
        copy_root = Path(td) / "repo"
        ok, why = _copy_tree(root, copy_root)
        if not ok:
            return Check(name, "skipped", why)
        ckdir = copy_root.joinpath(*_rel_parts(kdir, root))
        return _build_check(name, lambda: [kubectl, "kustomize", str(ckdir)],
                            copy_root=copy_root, rel=_rel_parts(target, root),
                            before=before, after=after)


def _check_helm(chart: Path, target: Path, before: Optional[str], after: str) -> Check:
    name = "helm_template"
    helm = _found("helm")
    if not helm:
        return Check(name, "skipped", "helm not installed")
    with tempfile.TemporaryDirectory(prefix="kubeastra-validate-") as td:
        copy_root = Path(td) / "chart"
        ok, why = _copy_tree(chart, copy_root)
        if not ok:
            return Check(name, "skipped", why)
        rel = _rel_parts(target, chart)
        extra = []
        if len(rel) == 1 and target.name.startswith("values") and target.name not in ("values.yaml", "values.yml"):
            extra = ["-f", str(copy_root.joinpath(*rel))]
        return _build_check(name, lambda: [helm, "template", "kubeastra-validate", str(copy_root), *extra],
                            copy_root=copy_root, rel=rel, before=before, after=after)


def _check_kubeconform(before: Optional[str], after: str) -> Check:
    name = "kubeconform"
    kc = _found("kubeconform")
    if not kc:
        return Check(name, "skipped", "kubeconform not installed")
    with tempfile.TemporaryDirectory(prefix="kubeastra-validate-") as td:
        def run(text: str) -> tuple[int, str, str]:
            p = Path(td) / "manifest.yaml"
            p.write_text(text)
            return _run([kc, "-strict", "-ignore-missing-schemas", "-summary", str(p)])

        rc, out, err = run(after)
        if rc in (RC_TIMEOUT, RC_NOT_FOUND):
            return Check(name, "skipped", _first_line(err, out) or "could not run")
        if rc == 0:
            return Check(name, "pass")
        detail = _first_line(out, err)
        if before is not None:
            rc_b, _, _ = run(before)
            if rc_b not in (0, RC_TIMEOUT, RC_NOT_FOUND):
                return Check(name, "warn", f"pre-existing schema errors: {detail}")
        return Check(name, "fail", detail or "schema validation failed")


# ── server-side dry run against the chat's connected cluster ─────────────────
#
# `kubectl apply --dry-run=server` runs the edit through the real API server:
# admission webhooks, quotas, immutable fields — things no offline check sees.
# Nothing is persisted. It uses ONLY the cluster the chat session explicitly
# connected to (cluster_session.resolve), never the machine's ambient kubeconfig
# context, which on a laptop is often a cluster the user never chose for this.
# Problems with the environment (unreachable, not logged in, RBAC, missing
# namespace, CRD not installed) skip the check — they say nothing about the edit.

_NO_MATCH_RE = re.compile(r'no matches for kind "([^"]+)" in version "([^"]+)"')
_NS_NOT_FOUND_RE = re.compile(r'namespaces? "[^"]+" not found')
_RBAC_RE = re.compile(r'is forbidden: User "|cannot (get|list|create|patch|update) resource', re.IGNORECASE)
_UNREACHABLE = (
    "unable to connect to the server", "connection refused", "i/o timeout", "no such host",
    "dial tcp", "couldn't get current server api group list", "tls handshake timeout",
    "context deadline exceeded", "must be logged in", "(unauthorized)", "provide credentials",
    "getting credentials",
)
_BUILTIN_GROUPS = frozenset({"", "apps", "batch", "autoscaling", "policy", "extensions"})


def _dry_run_environment_problem(text: str) -> Optional[str]:
    """Why a dry-run failure is about the environment rather than the edit, or
    None when it's a genuine rejection of the manifest."""
    low = text.lower()
    if "admission webhook" in low and "denied" in low:
        return None                     # a policy said no to this object — real
    m = _NO_MATCH_RE.search(text)
    if m:
        version = m.group(2)
        group = version.split("/")[0] if "/" in version else ""
        if group in _BUILTIN_GROUPS or group.endswith(".k8s.io"):
            return None                 # a typo in a built-in kind is a real error
        return f"kind {m.group(1)} ({version}) isn't installed in the connected cluster"
    if _NS_NOT_FOUND_RE.search(text):
        return "the namespace doesn't exist in the connected cluster"
    if _RBAC_RE.search(text):
        return "not permitted to dry-run this in the connected cluster"
    if any(n in low for n in _UNREACHABLE):
        return "the connected cluster is unreachable or not authenticated"
    return None


def _check_server_dry_run(before: Optional[str], after: str, cluster: Optional[dict]) -> Check:
    name = "server_dry_run"
    if cluster is None:
        return Check(name, "skipped", "no cluster connected to this chat")
    context = cluster.get("context_name")
    if cluster.get("unavailable") or not context:
        return Check(name, "skipped", "this chat's cluster connection is unavailable")
    kubectl = _found("kubectl")
    if not kubectl:
        return Check(name, "skipped", "kubectl not installed")
    kubeconfig = cluster.get("kubeconfig_path")
    # Single-token flags: a value can never be parsed as a separate flag.
    base = [kubectl, f"--context={context}"] + ([f"--kubeconfig={kubeconfig}"] if kubeconfig else [])

    with tempfile.TemporaryDirectory(prefix="kubeastra-validate-") as td:
        def run(text: str) -> tuple[int, str, str]:
            p = Path(td) / "manifest.yaml"
            p.write_text(text)
            return _run(base + ["apply", "--dry-run=server", "-o", "name", "-f", str(p)])

        rc, out, err = run(after)
        if rc in (RC_TIMEOUT, RC_NOT_FOUND):
            return Check(name, "skipped", _first_line(err, out) or "could not run")
        if rc == 0:
            return Check(name, "pass")
        problem = _dry_run_environment_problem(f"{err}\n{out}")
        if problem:
            return Check(name, "skipped", problem)
        detail = _first_line(err, out)
        if before is not None:
            rc_b, out_b, err_b = run(before)
            if rc_b not in (0, RC_TIMEOUT, RC_NOT_FOUND) and not _dry_run_environment_problem(f"{err_b}\n{out_b}"):
                return Check(name, "warn", f"pre-existing: the cluster also rejects the current file: {detail}")
        return Check(name, "fail", detail or "the cluster rejected the change")


# ── entry point ───────────────────────────────────────────────────────────────

_UNSET = object()


def validate_edit(target, before: Optional[str], after: str, *, root, cluster=_UNSET) -> ValidationResult:
    """Validate replacing ``before`` (None for a new file) with ``after`` at
    ``target`` inside the granted ``root``.

    ``cluster`` is the chat session's explicit connection
    ({context_name, kubeconfig_path}), None when the chat has none, or
    {"unavailable": True} when it has one that's broken. Omit it to leave the
    server dry run out entirely."""
    root = Path(root).resolve()
    target = Path(target).resolve()
    checks: list[Check] = []

    chart = _chart_dir(target, root)
    in_templates = chart is not None and _is_under(target, chart / "templates")
    is_values = chart is not None and not in_templates and target.parent == chart \
        and target.name.startswith("values")

    manifests_after: list[dict] = []
    if not in_templates:
        # Helm templates aren't plain YAML ({{ }}); helm_template covers them.
        ok, docs_after, err = _parse(after)
        if not ok:
            return _result([Check("yaml_parse", "fail", err)])
        checks.append(Check("yaml_parse", "pass"))
        if not is_values:
            manifests_after = _manifests(docs_after, target)
            manifests_before = _manifests(_parse(before)[1], target)
            if manifests_after:
                status, detail = _status_from(_shape_violations(manifests_before),
                                              _shape_violations(manifests_after))
                checks.append(Check("k8s_shape", status, detail))
                v_before, _ = _policy_findings(manifests_before)
                v_after, adv_after = _policy_findings(manifests_after)
                status, detail = _status_from(v_before, v_after, warn_extra=adv_after)
                checks.append(Check("policy", status, detail))

    checks.append(_check_diff_budget(before or "", after))

    if chart is not None and (in_templates or is_values):
        checks.append(_check_helm(chart, target, before, after))
    else:
        kdir = _kustomize_dir(target, root)
        if kdir is not None:
            checks.append(_check_kustomize(kdir, target, before, after, root))

    if manifests_after:
        checks.append(_check_kubeconform(before, after))
        if cluster is not _UNSET:
            checks.append(_check_server_dry_run(before, after, cluster))

    return _result(checks)
