"""Session management endpoints.

GET  /api/sessions/{session_id}/history      — load chat history on page mount
DELETE /api/sessions/{session_id}/history    — clear history ("New chat")
GET  /api/sessions/{session_id}/ssh-target   — restore saved SSH host/user/port
POST /api/sessions/{session_id}/ssh-target   — save SSH target after connect
DELETE /api/sessions/{session_id}/ssh-target — clear on disconnect
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import auth
import db
from routers.chat import _llm_provider

router = APIRouter()


# ── Models ────────────────────────────────────────────────────────────────────

class SSHTargetBody(BaseModel):
    host: str
    username: str
    port: int = 22


class CreateSessionBody(BaseModel):
    title: Optional[str] = None


# ── History ───────────────────────────────────────────────────────────────────

@router.get("/sessions")
def list_sessions(request: Request, limit: int = 100):
    """Return chat sessions owned by the current user when auth is enabled."""
    if not auth.auth_enabled():
        return {"sessions": []}
    user = auth.require_current_user(request)
    return {"sessions": db.list_sessions_for_user(user["id"], limit=limit)}


@router.post("/sessions")
def create_session(body: CreateSessionBody, request: Request):
    """Create a new account-owned chat session."""
    user_id = None
    if auth.auth_enabled():
        user_id = auth.require_current_user(request)["id"]
    session = db.create_session(user_id=user_id, title=body.title)
    return {"session": session}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request):
    """Archive a chat session owned by the current user."""
    user = auth.require_owned_session(request, session_id)
    if not user:
        db.clear_history(session_id)
        db.clear_user_memory(session_id)
        return {"ok": True}
    return {"ok": db.archive_session(session_id, user["id"])}


@router.get("/sessions/{session_id}/history")
def get_history(session_id: str, request: Request, limit: int = 100):
    """Return persisted messages for a session (oldest first)."""
    access = auth.require_session_read_access(request, session_id)
    messages = db.get_history(session_id, limit=limit)
    session = access.get("session") or {}
    if access.get("access_mode") == "admin_readonly":
        try:
            db.save_session_access_event(
                viewer_user_id=access["user"]["id"],
                target_session_id=session_id,
                owner_user_id=session.get("owner_user_id"),
                access_type="admin_read_history",
            )
        except Exception:
            # Audit failures should not block read-only incident support.
            pass
    return {
        "session_id": session_id,
        "access_mode": access.get("access_mode", "owned"),
        "readonly": bool(access.get("readonly", False)),
        "owner_username": session.get("owner_username"),
        "owner_display_name": session.get("owner_display_name"),
        "title": session.get("title"),
        "messages": messages,
    }


@router.delete("/sessions/{session_id}/history")
def clear_history(session_id: str, request: Request):
    """Delete all messages for a session (New chat). Also wipes the per-user
    conversation-memory blob (Phase 2.2) so the agent doesn't carry stale
    context into a fresh session."""
    auth.require_owned_session(request, session_id)
    db.clear_history(session_id)
    db.clear_user_memory(session_id)
    return {"ok": True}


class AppendMessageBody(BaseModel):
    """A single chat turn to persist out-of-band — used by client-side flows
    that produce a user/assistant exchange without going through the chat
    stream (the /rca slash command is the canonical case)."""
    role: str  # "user" | "assistant"
    content: str
    tool_used: Optional[str] = None


class AppendMessagesBody(BaseModel):
    messages: list[AppendMessageBody]


@router.post("/sessions/{session_id}/messages")
def append_messages(session_id: str, body: AppendMessagesBody, request: Request):
    """Append messages to a session's history in order. Lets client-side
    branches (e.g. the chat page's /rca interceptor) persist their exchanges
    so they survive navigation and appear inline with regular chat turns
    rather than being cached client-side and merged out of order."""
    auth.require_owned_session(request, session_id)
    for msg in body.messages:
        if msg.role not in ("user", "assistant"):
            raise HTTPException(status_code=400, detail=f"invalid role: {msg.role}")
        db.save_message(session_id, msg.role, msg.content, tool_used=msg.tool_used)
    return {"ok": True, "appended": len(body.messages)}


@router.post("/sessions/{session_id}/export")
def export_post_mortem(session_id: str, request: Request):
    """Generate a post-mortem / incident report from the session history."""
    auth.require_owned_session(request, session_id)
    messages = db.get_history(session_id, limit=200)
    if not messages:
        raise HTTPException(status_code=404, detail="Session history is empty")
    
    # Format messages into a script
    history_lines = []
    for m in messages:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        history_lines.append(f"[{role.upper()}]:\n{content}\n")
    
    history_str = "\n---\n".join(history_lines)
    
    system_prompt = (
        "You are an expert DevOps engineer writing a formal Incident Report (Post-Mortem). "
        "Based ONLY on the following chat and investigation history, write a comprehensive markdown Post-Mortem. "
        "Include sections for: Summary, Root Cause, Timeline of Investigation, and Remediation Steps Taken. "
        "Do NOT hallucinate events, commands, or details outside of this history."
    )
    
    prompt = f"Please generate the Post-Mortem for this incident:\n\n{history_str}"
    
    provider = _llm_provider()
    if not provider:
         raise HTTPException(status_code=500, detail="LLM provider is not configured.")
    
    try:
        raw_md = ""
        # The provider exposes generate_stream
        for chunk in provider.generate_stream(prompt, system=system_prompt, temperature=0.2, max_tokens=3000):
            if chunk:
                raw_md += chunk
        return {"markdown": raw_md}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate post-mortem: {e}")


# ── SSH target ────────────────────────────────────────────────────────────────

@router.get("/sessions/{session_id}/ssh-target")
def get_ssh_target(session_id: str, request: Request):
    """Return saved SSH host/username/port, or null if none saved."""
    auth.require_owned_session(request, session_id)
    target = db.get_ssh_target(session_id)
    return {"ssh_target": target}


@router.post("/sessions/{session_id}/ssh-target")
def save_ssh_target(session_id: str, body: SSHTargetBody, request: Request):
    """Persist SSH target after a successful connection. Password never stored."""
    auth.require_owned_session(request, session_id)
    db.save_ssh_target(session_id, body.host, body.username, body.port)
    return {"ok": True}


@router.delete("/sessions/{session_id}/ssh-target")
def delete_ssh_target(session_id: str, request: Request):
    """Clear saved SSH target on disconnect."""
    auth.require_owned_session(request, session_id)
    db.delete_ssh_target(session_id)
    return {"ok": True}
