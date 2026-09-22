"""propose_file_edit tool tests (Phase 2, PR 3).

The tool never writes. It resolves a write grant, applies exact edits in
memory, validates, and parks a single-use pending write for the human to
approve. Covers the needs_access(write) contract, the pending_write contract,
that nothing touches disk, the redaction round-trip (a secret the model never
saw survives the edit; the diff shown to model/UI is scrubbed), the
self-correct cap (3 invalid proposals, then stop), refusals, new files,
registration, and what the model sees through the react surface.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import desktop_folders as df  # noqa: E402
import desktop_validators as dv  # noqa: E402
import desktop_writes as dw  # noqa: E402

API = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  annotations:
    deploy-token: ghp_0123456789abcdef0123456789abcdef0123
spec:
  replicas: 2
  selector:
    matchLabels: {app: api}
  template:
    metadata:
      labels: {app: api}
    spec:
      containers:
        - name: api
          image: ghcr.io/acme/api:1.4.2
"""


class Ctx:
    def __init__(self, session_id="s1"):
        self.session_id = session_id


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    import audit
    import desktop_paths
    monkeypatch.setattr(desktop_paths, "config_path", lambda: tmp_path / "cfg.json")
    monkeypatch.setattr(audit, "emit", lambda *a, **k: "evt")
    monkeypatch.setattr(dv, "_found", lambda tool: None)      # deterministic: no binaries
    monkeypatch.setattr(dw, "pending_write_store", dw.PendingWriteStore())
    dw._invalid_attempts.clear()


@pytest.fixture(scope="module", autouse=True)
def restore_registry():
    """Desktop tools exist only in desktop mode; leave the shared registry as
    found so server-mode assertions elsewhere (the documented tool count) hold."""
    import tool_registry as tr
    saved = dict(tr.TOOLS)
    yield
    tr.TOOLS.clear()
    tr.TOOLS.update(saved)


@pytest.fixture
def repo(tmp_path):
    root = (tmp_path / "infra").resolve()
    (root / "prod").mkdir(parents=True)
    (root / "prod" / "api.yaml").write_text(API)
    return root


@pytest.fixture
def write_repo(repo):
    df.add_grant(str(repo), "write")
    return repo


def propose(path, *, edits=None, content=None, reason="scale up", session="s1"):
    params = {"path": str(path), "reason": reason, "edits": edits or []}
    if content is not None:
        params["content"] = content
    return dw._handle_propose_file_edit(params, Ctx(session))


REPLICAS = [{"old": "replicas: 2", "new": "replicas: 4"}]
PRIVILEGED = [{"old": "          image: ghcr.io/acme/api:1.4.2\n",
               "new": "          image: ghcr.io/acme/api:1.4.2\n"
                      "          securityContext:\n            privileged: true\n"}]


class TestContract:
    def test_ungranted_asks_for_write_access(self, repo):
        out = propose(repo / "prod" / "api.yaml", edits=REPLICAS)
        assert out["needs_access"]["mode"] == "write"
        assert out["needs_access"]["reason"] == "scale up"

    def test_read_grant_is_not_enough(self, repo):
        df.add_grant(str(repo), "read")
        out = propose(repo / "prod" / "api.yaml", edits=REPLICAS)
        assert out["needs_access"]["mode"] == "write"

    def test_valid_edit_parks_a_pending_write_and_touches_nothing(self, write_repo):
        target = write_repo / "prod" / "api.yaml"
        out = propose(target, edits=REPLICAS)
        pw = out["pending_write"]
        assert out["success"] is True
        assert pw["token"].startswith("pwr_")
        assert pw["path"] == "prod/api.yaml" and pw["created"] is False
        assert "+  replicas: 4" in pw["diff"] and "-  replicas: 2" in pw["diff"]
        assert pw["validation"]["ok"] is True
        assert pw["validation"]["validated"] is False             # no kubeconform here
        assert target.read_text() == API                           # NOT written
        stored = dw.pending_write_store.get(pw["token"])
        assert stored.abs_path == str(target) and stored.session_id == "s1"

    def test_secret_the_model_never_saw_survives_and_diff_is_scrubbed(self, write_repo):
        target = write_repo / "prod" / "api.yaml"
        # Edit right next to the secret so it lands in the diff's context lines.
        out = propose(target, edits=[{"old": "  name: api\n", "new": "  name: api\n  labels: {tier: web}\n"}])
        pw = out["pending_write"]
        assert "ghp_0123456789abcdef0123456789abcdef0123" not in pw["diff"]
        stored = dw.pending_write_store.get(pw["token"])
        assert "ghp_0123456789abcdef0123456789abcdef0123" in stored.after   # real bytes kept

    def test_editing_a_line_the_model_could_not_see_is_refused(self, write_repo):
        # A partial anchor would match the real bytes of a redacted line; the
        # diff's "-" line would then leak the secret, and the edit would corrupt
        # it. The model must never change a line it only saw redacted.
        target = write_repo / "prod" / "api.yaml"
        out = propose(target, edits=[{"old": "deploy-token: ", "new": "deploy-token: rotated-"}])
        assert out["error"] == "refused: touches_redacted_line"
        assert "ghp_" not in str(out)
        assert dw.pending_write_store._items == {}

    def test_private_key_block_lines_are_off_limits(self, write_repo):
        # read_file redacts a private-key PEM block whole, so the model can't
        # anchor inside it: to the model the text isn't there at all.
        pem = ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: tls\ndata:\n  key: |\n"
               "    -----BEGIN RSA PRIVATE KEY-----\n    MIIEowIBAAKCAQEAabc\n"
               "    -----END RSA PRIVATE KEY-----\n  other: x\n")
        (write_repo / "prod" / "tls.yaml").write_text(pem)
        out = propose(write_repo / "prod" / "tls.yaml",
                      edits=[{"old": "MIIEowIBAAKCAQEAabc", "new": "MIIEowIBAAKCAQEAxyz"}])
        assert out["error"] == "refused: edit_not_found"
        inserted = propose(write_repo / "prod" / "tls.yaml",
                           edits=[{"old": "    MIIEowIBAAKCAQEAabc\n",
                                   "new": "    MIIEowIBAAKCAQEAabc\n    extra\n"}])
        assert inserted["error"] == "refused: edit_not_found"
        # Lines outside the block are still editable.
        ok = propose(write_repo / "prod" / "tls.yaml", edits=[{"old": "other: x", "new": "other: y"}])
        assert "pending_write" in ok

    def test_certificate_is_visible_so_editable(self, write_repo):
        cert = ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: ca\ndata:\n  ca: |\n"
                "    -----BEGIN CERTIFICATE-----\n    MIIBszCCAVmg\n    -----END CERTIFICATE-----\n")
        (write_repo / "prod" / "ca.yaml").write_text(cert)
        out = propose(write_repo / "prod" / "ca.yaml",
                      edits=[{"old": "MIIBszCCAVmg", "new": "MIIBszCCAVmh"}])
        assert "pending_write" in out

    def test_new_file(self, write_repo):
        out = propose(write_repo / "prod" / "patch.yaml",
                      content="apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: extra\n")
        assert out["pending_write"]["created"] is True
        assert not (write_repo / "prod" / "patch.yaml").exists()

    def test_existing_file_rejects_full_content(self, write_repo):
        out = propose(write_repo / "prod" / "api.yaml", content="apiVersion: v1\n")
        assert out["success"] is False and out["error"] == "refused: use_edits"

    def test_missing_file_rejects_edits(self, write_repo):
        out = propose(write_repo / "prod" / "nope.yaml", edits=REPLICAS)
        assert out["success"] is False and out["error"] == "refused: file_missing"


class TestRefusals:
    def test_deny_list(self, write_repo):
        out = propose(write_repo / ".env", content="A=1\n")
        assert out["error"] == "refused: deny_list"

    def test_redacted_anchor(self, write_repo):
        out = propose(write_repo / "prod" / "api.yaml",
                      edits=[{"old": "deploy-token: ***redacted***", "new": "deploy-token: x"}])
        assert out["error"] == "refused: redaction_marker"
        assert "attempts_remaining" in out

    def test_anchor_not_found_hints_at_read_file(self, write_repo):
        out = propose(write_repo / "prod" / "api.yaml", edits=[{"old": "replicas: 9", "new": "replicas: 3"}])
        assert out["error"] == "refused: edit_not_found"
        assert "read_file" in out["hint"]

    def test_security_refusals_do_not_count_as_attempts(self, write_repo):
        for _ in range(5):
            out = propose(write_repo / ".env", content="A=1\n")
        assert "stop" not in out


class TestNoSecretOracle:
    """Every answer must depend only on the redacted view the model read, or a
    compromised model could learn a secret from which error it gets back."""

    SECRET = "ghp_0123456789abcdef0123456789abcdef0123"

    def test_a_prefix_of_the_secret_looks_like_any_wrong_guess(self, write_repo):
        target = write_repo / "prod" / "api.yaml"
        right = propose(target, edits=[{"old": "deploy-token: ghp_01", "new": "x"}], session="a")
        wrong = propose(target, edits=[{"old": "deploy-token: zzzzzz", "new": "x"}], session="b")
        for k in ("error", "hint", "attempts_remaining"):
            assert right[k] == wrong[k]
        assert right["error"] == "refused: edit_not_found"

    def test_anchor_that_also_occurs_inside_a_secret_is_not_reported_ambiguous(self, write_repo):
        # "0123456789abcdef" is visible once (note:) and also hidden inside the
        # token. Answering "ambiguous" would reveal it's part of the secret.
        target = write_repo / "prod" / "api.yaml"
        text = target.read_text().replace("spec:\n", "  labels: {note: 0123456789abcdef}\nspec:\n", 1)
        target.write_text(text)
        out = propose(target, edits=[{"old": "0123456789abcdef", "new": "fedcba9876543210"}])
        assert out["error"] == "refused: touches_redacted_line"
        assert self.SECRET not in str(out)

    def test_hidden_line_probes_are_capped_and_valid_edits_do_not_reset_it(self, write_repo):
        target = write_repo / "prod" / "api.yaml"
        probe = [{"old": "deploy-token: ", "new": "deploy-token: x"}]
        outs = []
        for _ in range(3):
            outs.append(propose(target, edits=probe))
            assert "pending_write" in propose(target, edits=REPLICAS)   # a valid edit in between
        assert all(o["error"] == "refused: touches_redacted_line" for o in outs)
        assert propose(target, edits=probe)["stop"] is True

    def test_once_stopped_nothing_is_evaluated(self, write_repo):
        target = write_repo / "prod" / "api.yaml"
        for _ in range(4):
            propose(target, edits=PRIVILEGED)
        after_stop = propose(target, edits=REPLICAS)         # would be valid
        assert after_stop["stop"] is True
        assert "pending_write" not in after_stop

    def test_stop_carries_no_reason(self, write_repo):
        target = write_repo / "prod" / "api.yaml"
        for _ in range(3):
            propose(target, edits=[{"old": "deploy-token: ", "new": "deploy-token: x"}])
        stop = propose(target, edits=[{"old": "deploy-token: ", "new": "deploy-token: y"}])
        assert stop["stop"] is True and stop["failures"] == []
        assert "touches_redacted_line" not in str(stop)


class TestSelfCorrectCap:
    def test_three_invalid_then_stop(self, write_repo):
        target = write_repo / "prod" / "api.yaml"
        seen = [propose(target, edits=PRIVILEGED) for _ in range(4)]
        assert [o.get("attempts_remaining") for o in seen[:3]] == [2, 1, 0]
        assert all(o["invalid"] is True for o in seen[:3])
        assert seen[3]["stop"] is True
        assert "privileged" in seen[3]["failures"][0]["detail"]
        assert not any("pending_write" in o for o in seen)

    def test_valid_proposal_resets_the_counter(self, write_repo):
        target = write_repo / "prod" / "api.yaml"
        propose(target, edits=PRIVILEGED)
        propose(target, edits=PRIVILEGED)
        assert "pending_write" in propose(target, edits=REPLICAS)
        assert propose(target, edits=PRIVILEGED)["attempts_remaining"] == 2

    def test_counter_is_per_session(self, write_repo):
        target = write_repo / "prod" / "api.yaml"
        for _ in range(3):
            propose(target, edits=PRIVILEGED, session="a")
        assert propose(target, edits=PRIVILEGED, session="b")["attempts_remaining"] == 2


class TestRegistrationAndObservation:
    def test_registered_with_the_read_tools(self):
        import tool_registry as tr
        df.register_desktop_tools()
        t = tr.TOOLS["propose_file_edit"]
        assert t.category == "local_folder"
        assert set(t.surfaces) == {"react", "chat"}

    def test_model_sees_diff_and_that_nothing_is_written(self, write_repo):
        import react
        import tool_registry as tr
        df.register_desktop_tools()
        res = tr.dispatch("propose_file_edit",
                          {"path": str(write_repo / "prod" / "api.yaml"), "reason": "scale",
                           "edits": REPLICAS},
                          tr.DispatchContext(surface="react", session_id="s1"))
        d = res.model_dump(by_alias=True)
        d["payload"] = res.payload
        obs = react._truncate_observation(d, "propose_file_edit")
        assert "NOT written" in obs
        assert "+  replicas: 4" in obs
        assert "UNVALIDATED" in obs
        assert "pwr_" not in obs                  # the approval token never reaches the model

    def test_model_sees_failures_to_self_correct(self, write_repo):
        out = propose(write_repo / "prod" / "api.yaml", edits=PRIVILEGED)
        text = dw.render_propose_observation(out)
        assert "privileged" in text and "2 attempt" in text
