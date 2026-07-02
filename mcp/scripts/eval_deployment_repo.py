#!/usr/bin/env python3
"""Retrieval evaluation for the deployment_repo collection.

Reads ``tests/rag/eval_deployment_repo.jsonl`` (one record per line, each
with ``error_paste``, ``expected_paths``, ``expected_roles``). Ingests
the local repo clone into an in-memory Qdrant, runs each query, and
reports recall@5 and recall@10 aggregated across the eval set.

A hit at rank K means at least one of the top-K results matched on:
  - ``path`` substring (e.g. ``roles/kubernetes/kube_check_health``
    contains expected path ``roles/kubernetes/kube_check_health``), or
  - ``role`` exact equality with one of ``expected_roles``.

Ship bar (plan §11.6): recall@5 ≥ 0.70, recall@10 ≥ 0.85.

Usage:
    venv/bin/python3 scripts/eval_deployment_repo.py
    venv/bin/python3 scripts/eval_deployment_repo.py --verbose   # per-query detail
    venv/bin/python3 scripts/eval_deployment_repo.py --repo /path/to/ansible
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from qdrant_client import QdrantClient  # noqa: E402

from services.embeddings import embeddings  # noqa: E402
from services.rag.ansible_roles import RoleAggregateSource  # noqa: E402
from services.rag.ingestion import ingest  # noqa: E402
from services.rag.schema import DEPLOYMENT_REPO  # noqa: E402
from services.rag.sources.local_path import LocalPathSource  # noqa: E402
from services import vector_db as vdb_module  # noqa: E402


DEFAULT_REPO = (
    "/Users/pruthvidavineni/AI_DevOps_Assistant/k8s-devops-ai-assistant/"
    "deployment-provisioning/ansible"
)
DEFAULT_EVAL = _PROJECT_ROOT / "tests" / "rag" / "eval_deployment_repo.jsonl"

# Ship bar — keep in sync with plan §11.6.
RECALL_AT_5_BAR = 0.70
RECALL_AT_10_BAR = 0.85


def _load_eval(path: Path) -> list[dict]:
    records: list[dict] = []
    with open(path) as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"WARN: skipping malformed line {line_no}: {exc}", file=sys.stderr)
    return records


def _ingest_repo(repo: str) -> None:
    """Build an in-memory Qdrant and load both passes."""
    client = QdrantClient(":memory:")
    vdb_module.vector_db._client = client
    vdb_module.vector_db.ensure_collection_for(DEPLOYMENT_REPO)
    print(f"== Ingesting {repo}")
    s1 = ingest(LocalPathSource(repo), target=DEPLOYMENT_REPO)
    print(f"   files: new={s1.new}, failed={s1.failed}, dur={s1.to_dict()['duration_seconds']}s")
    s2 = ingest(
        RoleAggregateSource(
            repo,
            repo_url="https://github.com/kubeastra/deployment-provisioning.git",
            branch="main",
            path_prefix="ansible",
        ),
        target=DEPLOYMENT_REPO,
    )
    print(f"   aggs:  new={s2.new}, failed={s2.failed}")


def _hit_in_topk(
    hits: list[dict],
    expected_paths: list[str],
    expected_roles: list[str],
    k: int,
) -> tuple[bool, int | None]:
    """Return (matched, matching_rank_1based) for top-K. ``matching_rank``
    is the 1-based rank of the first matching hit or None if no match."""
    for rank, h in enumerate(hits[:k], start=1):
        path = (h.get("path") or "").lower()
        role = h.get("role") or ""
        if any(p.lower() in path for p in expected_paths if p):
            return True, rank
        if expected_roles and role and role in expected_roles:
            return True, rank
    return False, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--eval", default=str(DEFAULT_EVAL))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    if not Path(args.repo).is_dir():
        print(f"ERROR: --repo {args.repo} not a directory", file=sys.stderr)
        return 2
    if not Path(args.eval).is_file():
        print(f"ERROR: --eval {args.eval} not found", file=sys.stderr)
        return 2

    _ingest_repo(args.repo)
    records = _load_eval(Path(args.eval))
    if not records:
        print("ERROR: no eval records loaded", file=sys.stderr)
        return 2

    print(f"\n== Running {len(records)} queries (top-10)\n")

    hits_at_5 = 0
    hits_at_10 = 0
    per_query_results: list[dict] = []

    for rec in records:
        q = rec["error_paste"]
        qvec = embeddings.embed(q)
        hits = vdb_module.vector_db.search_in(
            collection=DEPLOYMENT_REPO.name,
            query_vector=qvec,
            limit=10,
        )
        ok5, rank5 = _hit_in_topk(hits, rec.get("expected_paths", []), rec.get("expected_roles", []), 5)
        ok10, rank10 = _hit_in_topk(hits, rec.get("expected_paths", []), rec.get("expected_roles", []), 10)
        hits_at_5 += int(ok5)
        hits_at_10 += int(ok10)
        per_query_results.append({
            "id": rec.get("id", "(?)"),
            "ok5": ok5, "rank5": rank5,
            "ok10": ok10, "rank10": rank10,
            "top_sim": hits[0].get("similarity") if hits else None,
            "top_path": hits[0].get("path", "") if hits else "",
            "expected_paths": rec.get("expected_paths", []),
            "expected_roles": rec.get("expected_roles", []),
        })

        # With --verbose, dump the full top-10 for this query so we can
        # see why a miss missed (related-but-wrong content vs nothing
        # relevant at all). Quiet by default to keep the summary readable.
        if args.verbose:
            print(f"\n── Q[{rec.get('id', '?')}]  expected_paths={rec.get('expected_paths', [])}"
                  f"  expected_roles={rec.get('expected_roles', [])}")
            for rank, h in enumerate(hits, start=1):
                sim = h.get("similarity")
                marker = " ←" if (
                    any((p or "").lower() in (h.get("path") or "").lower()
                        for p in rec.get("expected_paths", []))
                    or h.get("role") in rec.get("expected_roles", [])
                ) else ""
                print(f"   #{rank:2d}  sim={sim:.3f}  kind={h.get('kind', ''):<14s} "
                      f"role={h.get('role', ''):<24s} path={h.get('path', '')}{marker}")

    # ── Per-query report ────────────────────────────────────────────────
    print(f"{'id':32s}  {'r@5':>4s} {'r@10':>5s} {'top_sim':>7s}  top_match (or top_path)")
    print("-" * 100)
    for r in per_query_results:
        mark5 = f"#{r['rank5']}" if r["ok5"] else "  - "
        mark10 = f"#{r['rank10']}" if r["ok10"] else "  - "
        sim = f"{r['top_sim']:.3f}" if r["top_sim"] is not None else "  -  "
        match_info = (
            f"hit @ {r['top_path']}" if r["ok5"]
            else f"miss; top={r['top_path']}"
        )
        print(f"{r['id']:32s}  {mark5:>4s} {mark10:>5s} {sim:>7s}  {match_info}")

    # ── Aggregate ───────────────────────────────────────────────────────
    n = len(records)
    r5 = hits_at_5 / n
    r10 = hits_at_10 / n
    print()
    print("== AGGREGATE")
    print(f"   recall@5  = {hits_at_5}/{n} = {r5:.1%}   (ship bar {RECALL_AT_5_BAR:.0%})")
    print(f"   recall@10 = {hits_at_10}/{n} = {r10:.1%}   (ship bar {RECALL_AT_10_BAR:.0%})")
    print()

    pass_r5 = r5 >= RECALL_AT_5_BAR
    pass_r10 = r10 >= RECALL_AT_10_BAR
    if pass_r5 and pass_r10:
        print("PASS")
        return 0
    print("FAIL — below ship bar")
    if not pass_r5:
        print(f"   recall@5 missed by {(RECALL_AT_5_BAR - r5):.1%}")
    if not pass_r10:
        print(f"   recall@10 missed by {(RECALL_AT_10_BAR - r10):.1%}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
