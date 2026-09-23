"""GitOps PR proposal endpoints. Two-phase: preview (no push) then open."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import auth
import audit
import db
import log_safety
from config.settings import get_settings
from gitops import resolve_token
from gitops.config import parse_config, overlay_for_env, env_for_cluster
from gitops.edit import apply_span, unified_diff, make_kustomize_patch
from gitops.github import GitHubClient, OpenedPR
from gitops.index import RepoFile, build_index, detect_markers, read_tarball
from gitops.locate import FieldChange, find_span
from gitops.render import render_pr
from gitops.store import new_preview, preview_store

logger = logging.getLogger(__name__)
router = APIRouter()

_ROLLOUT_RESTART = "rollout_restart"


def _require_enabled():
    if not get_settings().gitops_enabled:
        raise HTTPException(status_code=404, detail="gitops feature disabled")


def _admin_gate(request: Request):
    user = auth.require_current_user(request)
    if user and not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


class ConnectRepoBody(BaseModel):
    provider: str = "github"
    owner: str = Field(min_length=1)
    name: str = Field(min_length=1)
    default_branch: str = "main"
    config_path: str = "kubeastra.yaml"


class ChangeBody(BaseModel):
    kind: str
    name: str
    namespace: str | None = None
    field_path: list
    new_value: object
    reason: str = ""


class PreviewBody(BaseModel):
    proposal_id: str
    investigation_id: str = ""
    session_id: str | None = None
    cluster: str = ""
    diagnosis: dict = Field(default_factory=dict)
    change: ChangeBody
    target_env: str | None = None


class OpenBody(BaseModel):
    preview_token: str


# ── Seams the tests patch (keep these names) ──────────────────────────────────

def _fetch_repo_files(repo_row: dict, ref: str) -> list[RepoFile]:
    token = resolve_token()
    if not token:
        raise HTTPException(status_code=401, detail="no GitHub token configured")
    url = (f"https://api.github.com/repos/{repo_row['owner']}/"
           f"{repo_row['name']}/tarball/{ref}")
    r = httpx.get(url, headers={"Authorization": f"Bearer {token}",
                                "User-Agent": "kubeastra"},
                  follow_redirects=True, timeout=30.0)
    r.raise_for_status()
    return read_tarball(r.content)


def _open_pr_on_github(token: str, preview) -> OpenedPR:
    gh = GitHubClient(token)
    return gh.open_pr(owner=preview.owner, name=preview.name, base=preview.base,
                      head=preview.branch, title=preview.title, body=preview.body,
                      files=preview.files, commit_msg=preview.commit_msg,
                      labels=preview.labels)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/gitops/repos")
def list_repos(request: Request):
    _require_enabled()
    auth.require_current_user(request)
    return {"repos": db.list_gitops_repos()}


@router.post("/gitops/repos", status_code=201)
def connect_repo(body: ConnectRepoBody, request: Request):
    _require_enabled()
    _admin_gate(request)
    repo_id = str(uuid.uuid4())
    return db.create_gitops_repo(repo_id=repo_id, provider=body.provider,
                                 owner=body.owner, name=body.name,
                                 default_branch=body.default_branch,
                                 config_path=body.config_path)


@router.delete("/gitops/repos/{repo_id}")
def disconnect_repo(repo_id: str, request: Request):
    _require_enabled()
    _admin_gate(request)
    db.delete_gitops_repo(repo_id)
    return {"deleted": repo_id}


@router.post("/gitops/preview")
def preview(body: PreviewBody, request: Request):
    _require_enabled()
    _admin_gate(request)

    field_path = tuple(body.change.field_path)
    if field_path[:1] == (_ROLLOUT_RESTART,) or _ROLLOUT_RESTART in field_path:
        raise HTTPException(
            status_code=422,
            detail="a restart has no GitOps representation; use direct apply")

    repos = db.list_gitops_repos()
    if not repos:
        raise HTTPException(status_code=400, detail="no repo connected")
    repo = repos[0]

    files = _fetch_repo_files(repo, repo["default_branch"])
    cfg_file = next((f for f in files if f.path == repo["config_path"]), None)
    cfg = parse_config(cfg_file.text if cfg_file else None)
    target_env = body.target_env or env_for_cluster(cfg, body.cluster)

    index = build_index(files)
    matches = index.get((body.change.kind, body.change.name), [])

    change = FieldChange(kind=body.change.kind, name=body.change.name,
                         namespace=body.change.namespace, field_path=field_path,
                         new_value=body.change.new_value, reason=body.change.reason)

    changed_files: dict[str, str] = {}
    if not matches:
        markers = detect_markers(files)
        overlay = overlay_for_env(cfg, target_env)
        if cfg.layout == "kustomize" and overlay:
            changed_files = make_kustomize_patch(change, overlay)   # fallback C
        else:
            hint = ""
            if markers:
                hint = (" This looks like a Helm/Argo repo, where the live "
                        "resource is generated from a values file; KubeAstra "
                        "doesn't map generated resources back to their values yet.")
            raise HTTPException(
                status_code=404,
                detail=f"No {change.kind}/{change.name} found in the repo.{hint}")
    else:
        chosen = matches[0]
        if len(matches) > 1:
            overlay = overlay_for_env(cfg, target_env)
            preferred = [m for m in matches if overlay and m.file_path.startswith(overlay)]
            if len(preferred) == 1:
                chosen = preferred[0]
            else:
                raise HTTPException(status_code=409, detail={
                    "error": "ambiguous match",
                    "candidates": [m.file_path for m in matches]})
        source = next(f for f in files if f.path == chosen.file_path)
        span = find_span(source.text, chosen.doc_index, field_path)
        if span is None:
            raise HTTPException(
                status_code=422,
                detail=f"{change.kind}/{change.name} has no field "
                       f"{'.'.join(map(str, field_path))}")
        edited = apply_span(source.text, span, change.new_value)
        changed_files = {chosen.file_path: edited}

    diff = "".join(
        unified_diff(p, next((f.text for f in files if f.path == p), ""), c)
        for p, c in changed_files.items())

    spec = render_pr(diagnosis=body.diagnosis, diff=diff, change=change,
                     investigation_id=body.investigation_id, cluster=body.cluster,
                     tool_call_count=body.diagnosis.get("tool_call_count", 0),
                     app_base_url=get_settings().app_base_url,
                     session_id=body.session_id, branch_prefix=cfg.branch_prefix)

    pv = new_preview(proposal_id=body.proposal_id, repo_id=repo["id"],
                     files=changed_files, diff=diff, branch=spec.branch,
                     title=spec.title, body=spec.body, commit_msg=spec.commit_msg,
                     labels=cfg.labels or ["kubeastra"], owner=repo["owner"],
                     name=repo["name"], base=repo["default_branch"])
    preview_store.put(pv)
    return {"preview_token": pv.token, "diff": diff, "files": changed_files,
            "branch": spec.branch, "title": spec.title}


@router.post("/gitops/open")
def open_pr(body: OpenBody, request: Request):
    _require_enabled()
    user = _admin_gate(request)

    pv = preview_store.pop(body.preview_token)
    if pv is None:
        raise HTTPException(status_code=404, detail="preview expired or already used")

    # rate cap
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    cap = get_settings().gitops_max_prs_per_hour_per_repo
    if db.count_recent_gitops_prs(pv.repo_id, since) >= cap:
        audit.emit(audit.EventType.RATE_LIMIT_HIT, actor_type="user",
                   actor_id=str((user or {}).get("id", "local")),
                   subject=f"{pv.owner}/{pv.name}", severity="warn",
                   payload={"limit": cap})
        raise HTTPException(status_code=429, detail="too many PRs proposed this hour")

    token = resolve_token()
    if not token:
        raise HTTPException(status_code=401, detail="no GitHub token configured")

    try:
        opened = _open_pr_on_github(token, pv)
    except httpx.HTTPStatusError as exc:
        logger.warning("gitops open failed: %s", log_safety.one_line(str(exc)))
        raise HTTPException(status_code=502, detail="GitHub rejected the PR") from exc

    pr_id = str(uuid.uuid4())
    db.create_gitops_pr(pr_id=pr_id, repo_id=pv.repo_id, session_id=None,
                        investigation_id="", proposal_id=pv.proposal_id,
                        branch=pv.branch, provider_pr_number=opened.number,
                        provider_pr_url=opened.url, status="open",
                        files_changed=list(pv.files.keys()),
                        diff_summary=_diff_summary(pv.diff))

    audit.emit(audit.EventType.GITOPS_PR_OPENED, actor_type="agent",
               actor_id=str((user or {}).get("id", "local")),
               subject=f"{pv.owner}/{pv.name}#{opened.number}", severity="info",
               payload={"pr_url": opened.url, "files": list(pv.files.keys()),
                        "proposal_id": pv.proposal_id})

    return {"pr_id": pr_id, "pr_url": opened.url, "pr_number": opened.number}


@router.get("/gitops/prs")
def list_prs(request: Request):
    _require_enabled()
    auth.require_current_user(request)
    return {"prs": db.list_gitops_prs()}


def _diff_summary(diff: str) -> str:
    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return f"+{added} -{removed}"
