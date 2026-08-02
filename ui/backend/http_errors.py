"""Turn an unexpected exception into a response that helps without telling.

Every router had the same line:

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

which hands the client whatever the exception happened to say. For this
application that is not an abstract concern. The exceptions raised down these
paths carry kubeconfig paths, cluster context names, absolute filesystem
locations from the backend host, SSH hostnames, and — when a subprocess fails —
the full kubectl command line, arguments included. In server mode that reaches
any authenticated user regardless of what their own RBAC permits.

The instinct is to replace it with a bare "internal error", but that trades one
failure for another: an operator who cannot see why a call failed will retry it,
escalate it, or work around it, and the detail they needed was right there. So
each failure gets an **error id** — logged in full server-side with a traceback,
returned to the client as an opaque token. The person debugging can find the
whole story in the logs by grepping one string; the client learns nothing it
could not have guessed.

Deliberately narrow: this is for *unexpected* exceptions only. A 4xx that
deliberately explains itself — a validation message, the 409 telling an operator
which cluster an alert is bound to — is not a leak and must keep its wording.
Routing those through here would be a downgrade dressed as a fix.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_GENERIC = (
    "The server hit an unexpected error handling this request. "
    "The details were logged; quote error id {error_id} when reporting it."
)


def safe_error_text(exc: BaseException, *, context: str = "") -> str:
    """The same treatment for endpoints that return a dict rather than raising.

    Several endpoints report failure in the body (``{"ok": false, "error": …}``)
    because the frontend renders it inline. Those still must not carry raw
    exception text, and still need to be diagnosable, so they get the same id.

    The exception *type* is included deliberately: "TimeoutError" or
    "PermissionError" tells an operator which way to look without naming a
    path, a host, or a command line.
    """
    error_id = uuid.uuid4().hex[:12]
    logger.exception(
        "unhandled error id=%s%s",
        error_id,
        f" context={context}" if context else "",
        exc_info=exc,
    )
    return f"{type(exc).__name__} (error id {error_id}) — see server logs for detail"


def internal_error(exc: BaseException, *, context: str = "") -> HTTPException:
    """Log ``exc`` with a fresh id and return a 500 that reveals only that id.

    Returns rather than raises so the call site keeps `raise ... from exc`,
    which preserves the original traceback in the server log.
    """
    error_id = uuid.uuid4().hex[:12]
    logger.exception(
        "unhandled error id=%s%s",
        error_id,
        f" context={context}" if context else "",
        exc_info=exc,
    )
    return HTTPException(status_code=500, detail=_GENERIC.format(error_id=error_id))
