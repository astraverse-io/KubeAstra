"""Phase 5 (part 2): the resumable/idempotent Upgrade apply runner.

Pure orchestration with injected verify/execute/record — tested without a
cluster. See PILOTS_PLAN.md §Phase 5.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
for _p in (BACKEND_DIR, MCP_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import db  # noqa: E402
from pilots.upgrade_runner import run_plan  # noqa: E402


def _steps():
    return [
        {"id": "op", "order": 1, "kind": "operator_bump", "risk": "medium"},
        {"id": "m1", "order": 2, "kind": "manifest_pr", "risk": "low"},
        {"id": "cp", "order": 3, "kind": "control_plane", "risk": "high", "advisory": True},
    ]


def test_all_authorized_applies_then_control_plane_is_advisory():
    applied: list[str] = []
    out = run_plan(
        run_id="r", steps=_steps(), authorized_step_ids={"op", "m1"},
        verify=lambda s: s["id"] in applied,
        execute=lambda s: (applied.append(s["id"]), True)[1],
    )
    assert out.status == "done"
    assert {o.step_id: o.status for o in out.steps} == {"op": "applied", "m1": "applied", "cp": "advisory"}


def test_idempotent_skips_already_satisfied():
    applied = ["m1"]  # m1 already satisfied on the cluster
    executed: list[str] = []
    out = run_plan(
        run_id="r", steps=_steps(), authorized_step_ids={"op", "m1"},
        verify=lambda s: s["id"] in applied,
        execute=lambda s: (executed.append(s["id"]), applied.append(s["id"]), True)[-1],
    )
    assert "m1" not in executed  # verify-first skipped it
    by = {o.step_id: o.status for o in out.steps}
    assert by["m1"] == "verified" and by["op"] == "applied"


def test_unauthorized_non_high_risk_blocks_the_run():
    out = run_plan(
        run_id="r", steps=_steps(), authorized_step_ids=set(),
        verify=lambda s: False, execute=lambda s: True,
    )
    assert out.status == "blocked"
    assert out.steps[-1].step_id == "op" and out.steps[-1].status == "pending"


def test_high_risk_needs_individual_approval_not_plan_approval():
    steps = [{"id": "danger", "order": 1, "kind": "migrate_api_version", "risk": "high"}]
    applied: list[str] = []
    v = lambda s: s["id"] in applied  # noqa: E731
    e = lambda s: (applied.append(s["id"]), True)[1]  # noqa: E731

    # In the plan approval but high-risk -> NOT authorized by it.
    assert run_plan(run_id="r", steps=steps, authorized_step_ids={"danger"}, verify=v, execute=e).status == "blocked"
    # With its own individual approval -> runs.
    applied.clear()
    out = run_plan(run_id="r", steps=steps, authorized_step_ids=set(),
                   individual_approved_step_ids={"danger"}, verify=v, execute=e)
    assert out.status == "done" and out.steps[0].status == "applied"


def test_execute_failure_halts():
    out = run_plan(
        run_id="r", steps=[{"id": "m1", "order": 1, "kind": "manifest_pr", "risk": "low"}],
        authorized_step_ids={"m1"}, verify=lambda s: False, execute=lambda s: False,
    )
    assert out.status == "halted" and out.steps[0].status == "failed"


def test_post_apply_verify_failure_halts():
    out = run_plan(
        run_id="r", steps=[{"id": "m1", "order": 1, "kind": "manifest_pr", "risk": "low"}],
        authorized_step_ids={"m1"}, verify=lambda s: False, execute=lambda s: True,
    )
    assert out.status == "halted" and "verify" in out.steps[0].detail


def test_lock_blocks_everything():
    out = run_plan(
        run_id="r", steps=_steps(), authorized_step_ids={"op", "m1"},
        verify=lambda s: False, execute=lambda s: True, lock_ok=False,
    )
    assert out.status == "locked" and out.steps == []


def test_control_plane_is_never_executed():
    executed: list = []
    out = run_plan(
        run_id="r",
        steps=[{"id": "cp", "order": 1, "kind": "control_plane", "risk": "high", "advisory": True}],
        authorized_step_ids=set(),
        verify=lambda s: False, execute=lambda s: (executed.append(s), True)[1],
    )
    assert executed == [] and out.status == "done" and out.steps[0].status == "advisory"


def test_record_persists_step_state_and_supports_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "steps.db"))
    db.init_db()
    applied: list[str] = []

    def record(**kw):
        db.upsert_pilot_step("run1", kw["step_id"], ord=kw["ord"], kind=kw["kind"], status=kw["status"])

    out = run_plan(
        run_id="run1", steps=_steps(), authorized_step_ids={"op", "m1"},
        verify=lambda s: s["id"] in applied,
        execute=lambda s: (applied.append(s["id"]), True)[1],
        record=record,
    )
    assert out.status == "done"
    rows = {r["step_id"]: r["status"] for r in db.get_pilot_steps("run1")}
    assert rows == {"op": "applied", "m1": "applied", "cp": "advisory"}
