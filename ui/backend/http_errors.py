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

**Both helpers take the exception from ``sys.exc_info()`` rather than as an
argument, and must therefore be called from inside an ``except`` block.** That
is not a stylistic preference. Passing the exception in was the obvious first
design, and it worked — but it also meant a tainted object crossed into a
function that returns something the client receives, which is indistinguishable
from a leak to anything reading the code, human or static analyser. CodeQL
reported all 24 call sites as ``py/stack-trace-exposure`` for exactly that
reason. Reading from ``sys.exc_info()`` removes the path instead of annotating
it, so the guarantee is structural: there is no parameter through which
exception text could reach the response, and a future edit cannot add one
without changing this signature.
"""

from __future__ import annotations

import logging
import sys
import uuid

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_GENERIC = (
    "The server hit an unexpected error handling this request. "
    "The details were logged; quote error id {error_id} when reporting it."
)


def _log_current_exception(context: str) -> tuple[str, str]:
    """Log whatever is being handled right now; return its id and type name.

    ``logger.exception`` already reads ``sys.exc_info()``. This reads it too,
    but only for the type *name* — "TimeoutError", "PermissionError" — which is
    the one piece of an exception that is safe to hand back, and useless to an
    attacker who cannot see the message it came with.
    """
    error_id = uuid.uuid4().hex[:12]
    exc_type = sys.exc_info()[0]
    logger.exception(
        "unhandled error id=%s%s",
        error_id,
        f" context={context}" if context else "",
    )
    # Called outside an except block: a bug in the caller, not something to
    # crash a request over. The id and the "no active exception" log line are
    # still enough to find it.
    return error_id, exc_type.__name__ if exc_type else "UnknownError"


def safe_error_text(*, context: str = "") -> str:
    """The same treatment for endpoints that return a dict rather than raising.

    Several endpoints report failure in the body (``{"ok": false, "error": …}``)
    because the frontend renders it inline. Those still must not carry raw
    exception text, and still need to be diagnosable, so they get the same id.

    The exception *type* is included deliberately: "TimeoutError" or
    "PermissionError" tells an operator which way to look without naming a
    path, a host, or a command line.
    """
    error_id, exc_name = _log_current_exception(context)
    return f"{exc_name} (error id {error_id}) — see server logs for detail"


def internal_error(*, context: str = "") -> HTTPException:
    """Log the exception being handled, and return a 500 revealing only its id.

    Returns rather than raises so the call site reads ``raise internal_error(…)``
    and stays one statement. Raising it from inside an ``except`` block sets
    ``__context__`` to the original automatically, so the chain survives without
    an explicit ``from`` — and without ``from`` there is no expression at the
    call site holding the exception either.
    """
    error_id, _ = _log_current_exception(context)
    return HTTPException(status_code=500, detail=_GENERIC.format(error_id=error_id))
