"""The CodeQL findings, pinned as behaviour.

Three separate concerns, grouped because they came from one scan:

* **Stack-trace exposure.** Routers answered an unexpected failure with
  ``detail=str(e)``. The exceptions on these paths carry kubeconfig paths,
  cluster context names, absolute paths on the backend host and — when a
  subprocess fails — the whole kubectl command line. In server mode that
  reached any authenticated user, regardless of their own RBAC.

* **Polynomial ReDoS.** Several regexes applied to chat messages had two
  adjacent quantifiers able to match the same characters, so a long
  non-matching input costs O(n²). The message is user-supplied, so that is
  reachable. Fixed by bounding the quantifiers rather than rewriting the
  patterns: Kubernetes names cap at 253 characters, so the bounds cannot change
  the answer for any input the code was ever meant to handle — and these
  regexes drive routing, so changing *which* messages match would be a far
  worse bug than the one being fixed.

* **Path injection.** ``kubeconfig_path`` arrived in the request body and was
  opened on trust, giving an authenticated user a read primitive against the
  backend host in server mode.
"""

import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for p in (str(BACKEND_DIR), str(MCP_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ── stack-trace exposure ──────────────────────────────────────────────────


def test_internal_error_reveals_an_id_and_nothing_else():
    from http_errors import internal_error

    secret = "/home/deploy/.kube/config context=prod-eu-1"
    try:
        raise RuntimeError(f"failed reading {secret}")
    except RuntimeError:
        http_exc = internal_error(context="test")

    assert http_exc.status_code == 500
    assert secret not in http_exc.detail
    assert "error id" in http_exc.detail


def test_each_failure_gets_a_distinct_id():
    """Two users hitting the same bug must be separable in the log."""
    from http_errors import internal_error

    def once() -> str:
        try:
            raise RuntimeError("x")
        except RuntimeError:
            return internal_error().detail

    assert once() != once()


def test_safe_error_text_names_the_type_but_not_the_detail():
    from http_errors import safe_error_text

    try:
        raise PermissionError("/etc/shadow denied")
    except PermissionError:
        text = safe_error_text(context="test")

    assert "PermissionError" in text, "the operator needs a direction to look"
    assert "/etc/shadow" not in text
    assert "error id" in text


def test_the_original_exception_still_chains_without_an_explicit_from():
    """`raise` inside `except` sets __context__, so dropping `from e` costs nothing.

    This is what makes the parameterless signature affordable: the call sites
    gave up `from e`, and an operator reading an unhandled traceback still sees
    the original underneath.
    """
    from http_errors import internal_error

    original = RuntimeError("the actual cause")
    try:
        try:
            raise original
        except RuntimeError:
            raise internal_error(context="test")
    except Exception as raised:
        assert raised.__context__ is original


def test_calling_outside_an_except_block_does_not_explode():
    """Misuse should degrade to a useless-but-safe answer, not a second 500."""
    from http_errors import safe_error_text

    text = safe_error_text(context="misused")
    assert "UnknownError" in text
    assert "error id" in text


def test_no_router_hands_an_exception_string_to_a_500():
    """The guard. A reintroduced `detail=str(e)` leaks silently and forever."""
    offenders = []
    for path in sorted((BACKEND_DIR / "routers").glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "status_code=500" in line and ("str(e)" in line or "str(exc)" in line):
                offenders.append(f"{path.name}:{lineno}")

    assert not offenders, (
        "Use http_errors.internal_error so the detail reaches the log, not the "
        "client:\n  " + "\n  ".join(offenders)
    )


def test_no_call_site_passes_the_exception_in():
    """The structural half of the guarantee.

    ``internal_error(e, …)`` would still be *safe* — the helper never put the
    exception in the response even when it took one. It would not be
    *provable*: a tainted object crossing into a function whose return value
    reaches the client is what a reader, and CodeQL, has to treat as a leak. So
    the signature takes no exception, and this test keeps it that way.
    """
    call = re.compile(r"(?:internal_error|safe_error_text)\(\s*(?!context=|\))")
    offenders = []
    for path in sorted((BACKEND_DIR / "routers").glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if call.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    assert not offenders, (
        "These helpers read sys.exc_info(); call them from inside an except "
        "block with context= only:\n  " + "\n  ".join(offenders)
    )


# ── ReDoS: bounded, but behaviour preserved ───────────────────────────────

_K8S_HINT = re.compile(r"\b[a-z0-9]{1,63}-[a-z0-9-]{0,63}k8s[a-z0-9-]{0,63}\b")
_WORKLOAD = re.compile(r"\b([a-z0-9][a-z0-9.-]{0,126}-[a-z0-9.-]{0,126})\b")
_VERSION = re.compile(r"v?\d{1,9}(\.\d{1,9}){1,3}([-.]?(alpha|beta|rc)\d{0,9})?")
_TASK = re.compile(r"TASK\s{1,20}\[([^\]\n]{1,200})\]")


@pytest.mark.parametrize(
    "text",
    [
        "restart my-app-k8s-worker",
        "check prod-k8s",
        "the deploy-k8s-agent pod",
    ],
)
def test_the_k8s_hint_still_matches_what_it_used_to(text):
    assert _K8S_HINT.search(text)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("scale checkout-service", "checkout-service"),
        ("restart payment-api-v2", "payment-api-v2"),
        ("look at kube-system.pod-1", "kube-system.pod-1"),
    ],
)
def test_workload_names_still_extract(text, expected):
    match = _WORKLOAD.search(text)
    assert match and match.group(1) == expected


@pytest.mark.parametrize("text", ["1.28", "v1.30.2", "1.2.3.4", "v2.0.0-rc1"])
def test_versions_still_match(text):
    assert _VERSION.fullmatch(text)


def test_ansible_task_extraction_survives_and_no_longer_spans_lines():
    assert _TASK.search("TASK [install packages]").group(1) == "install packages"
    # `.+?` would have crossed the newline to find a later `]`; a negated class
    # cannot, which is both faster and more correct.
    assert _TASK.search("TASK [unterminated\nother line]") is None


@pytest.mark.parametrize("pattern", [_K8S_HINT, _WORKLOAD, _VERSION, _TASK])
def test_a_long_hostile_input_does_not_hang(pattern):
    """The point of the change. Unbounded, these took superlinear time."""
    import time

    hostile = "a-" * 20000
    start = time.monotonic()
    pattern.search(hostile)
    assert time.monotonic() - start < 1.0, "regex is still superlinear on hostile input"


def test_email_validation_is_unchanged_for_real_addresses():
    import auth

    assert auth.normalize_email("Pruthvi@Astraverse.dev") == "pruthvi@astraverse.dev"
    for bad in ("no-at-sign", "two@@at.com", "trailing@dot", "@nolocal.com"):
        with pytest.raises(ValueError):
            auth.normalize_email(bad)


# ── path injection ────────────────────────────────────────────────────────


def test_a_kubeconfig_outside_the_managed_locations_is_refused(tmp_path, monkeypatch):
    from routers import cluster

    intruder = tmp_path / "passwd"
    intruder.write_text("root:x:0:0::/root:/bin/sh\n", encoding="utf-8")

    assert cluster._allowed_kubeconfig_path(str(intruder)) is None


def test_a_kubeconfig_this_server_wrote_is_allowed(tmp_path, monkeypatch):
    from routers import cluster

    monkeypatch.setattr(cluster, "_TEMP_DIR", tmp_path)
    written = tmp_path / "session-abc.yaml"
    written.write_text("apiVersion: v1\n", encoding="utf-8")

    assert cluster._allowed_kubeconfig_path(str(written)) == str(written.resolve())


def test_traversal_out_of_the_managed_directory_is_refused(tmp_path, monkeypatch):
    """`resolve()` before comparing, so ../ cannot climb out."""
    from routers import cluster

    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(cluster, "_TEMP_DIR", managed)
    outside = tmp_path / "secret.yaml"
    outside.write_text("apiVersion: v1\n", encoding="utf-8")

    assert cluster._allowed_kubeconfig_path(str(managed / ".." / "secret.yaml")) is None


def test_nothing_supplied_is_not_an_error():
    from routers import cluster

    assert cluster._allowed_kubeconfig_path(None) is None
    assert cluster._allowed_kubeconfig_path("") is None


# ── the Alertmanager URL is a URL, not a path ─────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/shadow",
        "/etc/shadow",
        "ftp://alertmanager.internal",
        "alertmanager.internal:9093",  # no scheme — urlsplit reads it as one
        "",
    ],
)
def test_a_non_http_alertmanager_url_is_refused(value):
    import desktop_alerts

    with pytest.raises(ValueError):
        desktop_alerts._http_url(value)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("http://localhost:9093", "http://localhost:9093"),
        ("https://am.example.com/", "https://am.example.com"),
        ("https://am.example.com/prefix/", "https://am.example.com/prefix"),
        ("  http://localhost:9093  ", "http://localhost:9093"),
    ],
)
def test_a_real_alertmanager_url_survives_unchanged(value, expected):
    import desktop_alerts

    assert desktop_alerts._http_url(value) == expected


# ── third-party actions are pinned ────────────────────────────────────────


def test_third_party_actions_are_pinned_to_a_commit():
    """A moved tag runs someone else's code against a token that can write here.

    Only third-party actions are checked. ``actions/*`` and
    ``github/codeql-action`` are GitHub's own, published from the same
    infrastructure that would have to be compromised for a pin to help — so
    pinning them costs monthly upkeep and buys nothing.
    """
    workflows = BACKEND_DIR.parent.parent / ".github" / "workflows"
    first_party = ("actions/", "github/")
    sha = re.compile(r"^[0-9a-f]{40}$")

    offenders = []
    for path in sorted(workflows.glob("*.yml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(r"uses:\s*([^\s@]+)@([^\s#]+)", line)
            if not match:
                continue
            action, ref = match.group(1), match.group(2).strip()
            if action.startswith(first_party):
                continue
            if not sha.match(ref):
                offenders.append(f"{path.name}:{lineno}: {action}@{ref}")

    assert not offenders, (
        "Pin third-party actions to a full commit SHA, with the version in a "
        "trailing comment:\n  " + "\n  ".join(offenders)
    )
