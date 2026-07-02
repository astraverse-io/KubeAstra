from enum import Enum

class AgentErrorType(str, Enum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    LLM_RECOVERABLE = "llm_recoverable"
    USER_FIXABLE = "user_fixable"
    POLICY_BLOCKED = "policy_blocked"
    UNEXPECTED = "unexpected"

def classify_error(error_code: str, error_message: str = "") -> AgentErrorType:
    """Classify a tool or system error code/message into the standardized taxonomy."""
    err_lower = (error_code or "").lower()
    msg_lower = (error_message or "").lower()
    combined = f"{err_lower} {msg_lower}"

    # Policy Blocked
    if err_lower in ("tool_out_of_scope", "approval_rejected", "policy_blocked"):
        return AgentErrorType.POLICY_BLOCKED
    if "rejected" in combined or "policy" in combined:
        return AgentErrorType.POLICY_BLOCKED

    # LLM Recoverable
    if err_lower in ("unknown_tool", "invalid_params", "duplicate_tool_call"):
        return AgentErrorType.LLM_RECOVERABLE

    # Rate Limited
    if "429" in combined or "quota" in combined or "rate limit" in combined or "resource_exhausted" in combined:
        return AgentErrorType.RATE_LIMITED

    # Transient
    if "timeout" in combined or "503" in combined or "504" in combined or "connection refused" in combined or "unavailable" in combined:
        return AgentErrorType.TRANSIENT

    # User Fixable
    if "missing namespace" in combined or "expired approval token" in combined or "ssh auth" in combined or "unauthorized" in combined or "permission denied" in combined:
        return AgentErrorType.USER_FIXABLE

    return AgentErrorType.UNEXPECTED
