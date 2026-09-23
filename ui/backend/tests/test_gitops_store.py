from __future__ import annotations
import sys, time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from gitops.store import PreviewStore, new_preview  # noqa: E402


def _p(**over):
    kw = dict(proposal_id="p1", repo_id="r1", files={"a.yaml": "x"}, diff="d",
              branch="kubeastra/x", title="t", body="b", commit_msg="c",
              labels=["kubeastra"], owner="o", name="n", base="main")
    kw.update(over)
    return new_preview(**kw)


def test_put_get_roundtrip():
    store = PreviewStore()
    pv = _p()
    store.put(pv)
    assert store.get(pv.token).proposal_id == "p1"


def test_pop_is_single_use():
    store = PreviewStore()
    pv = _p()
    store.put(pv)
    assert store.pop(pv.token) is not None
    assert store.get(pv.token) is None


def test_expired_preview_is_gone():
    store = PreviewStore()
    pv = _p()
    pv.expires_at = time.time() - 1
    store.put(pv)
    assert store.get(pv.token) is None
