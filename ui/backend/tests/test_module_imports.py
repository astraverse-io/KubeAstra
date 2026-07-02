"""Module-import smoke tests.

These tests catch a class of bug where someone adds a type annotation
(or any reference to a name) without importing it — Python evaluates
annotations at module-load time, so a missing import = NameError that
only fires when the module is actually imported at runtime.

The May 2026 ``main-fcc2581`` regression was exactly this: ``react.py``
got a ``provider: Any`` annotation but ``Any`` was never added to the
``typing`` import. The module loaded silently in the build (no tests
imported it) and broke every chat in production.

These tests run during ``docker build`` (see Dockerfile) so a broken
import fails the image build before it can be tagged and pushed.
"""
from __future__ import annotations


def _force_annotation_eval(fn) -> None:
    """Resolve all string/lazy annotations on a function. Raises
    NameError if any annotation references an unimported name.

    Python 3.10+ stores annotations as strings when ``from __future__
    import annotations`` is used, and Python 3.14+ does so by default
    (PEP 649). Plain attribute access won't trigger evaluation — we
    need ``inspect.get_annotations(..., eval_str=True)`` to force it.

    Without this, the May 2026 ``provider: Any`` regression would have
    passed silently on Python 3.14+ even though it broke production on
    Python 3.11.
    """
    import inspect
    inspect.get_annotations(fn, eval_str=True)


def test_react_module_imports_cleanly():
    """Catches missing imports in type annotations on react.py."""
    import react  # noqa: F401
    from react import react_loop

    assert callable(react_loop), "react.react_loop is not callable"
    _force_annotation_eval(react_loop)


def test_triage_module_imports_cleanly():
    """Phase 3.0 — proactive cluster triage helper. Guards against the
    same regression class as the react.py one."""
    import triage  # noqa: F401
    from triage import cluster_overview, render_greeting

    assert callable(cluster_overview)
    assert callable(render_greeting)
    _force_annotation_eval(cluster_overview)
    _force_annotation_eval(render_greeting)


def test_chat_router_imports_cleanly():
    """The /api/chat handler imports react + triage lazily inside the
    request handler. Importing the router itself doesn't catch those —
    but it DOES catch missing names in the router module's own
    top-level code or in pydantic schemas.

    Skipped silently if FastAPI / db init fails for env reasons; the
    failure mode we care about is NameError / SyntaxError, not env."""
    try:
        from routers import chat  # noqa: F401
    except (ImportError, ModuleNotFoundError):
        # Missing deps when run outside the production container — skip.
        import pytest
        pytest.skip("optional dependency missing; not a regression we're guarding")
    except (NameError, SyntaxError) as exc:
        # These ARE the bugs we want to fail loudly on
        raise AssertionError(
            f"routers/chat.py has an import-time error: {exc}"
        ) from exc
