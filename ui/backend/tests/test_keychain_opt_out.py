"""The keychain must be skippable, because reading it can block on a human.

macOS identifies an application by its code signature. An ad-hoc signed build
gets a fresh identity on every rebuild, so a newly built backend asking for a
stored secret is an *unknown* application asking — and the OS puts up a dialog
and waits. There is no timeout on that dialog.

That is correct behaviour for the app and fatal for a build: `build.sh`
launches the frozen backend and waits for READY, and a build machine has nobody
to click the dialog. It hung for twenty-two minutes producing no output. CI
never reproduced it, because a fresh runner has an empty keychain and the
lookup misses without asking — so the failure only appears on a developer
machine that has actually used the app.

`KUBEASTRA_NO_KEYCHAIN=1` makes every read miss, exactly as on a first-run
install.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import desktop_secrets  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    desktop_secrets.clear_cache()
    yield
    desktop_secrets.clear_cache()


@pytest.fixture
def exploding_keyring(monkeypatch):
    """Any keychain call at all is a failure — that is the thing being prevented."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("the keychain was touched despite the opt-out")

    class _Keyring:
        get_password = staticmethod(_boom)
        set_password = staticmethod(_boom)
        delete_password = staticmethod(_boom)

    monkeypatch.setattr(desktop_secrets, "_keyring", lambda: _Keyring())
    monkeypatch.setattr(desktop_secrets, "is_secure", lambda: True)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_the_opt_out_is_recognised(monkeypatch, value):
    monkeypatch.setenv("KUBEASTRA_NO_KEYCHAIN", value)
    assert desktop_secrets.keychain_disabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", " "])
def test_anything_else_leaves_the_keychain_on(monkeypatch, value):
    """Fail closed: an unrecognised value must not silently disable credentials."""
    monkeypatch.setenv("KUBEASTRA_NO_KEYCHAIN", value)
    assert desktop_secrets.keychain_disabled() is False


def test_unset_leaves_the_keychain_on(monkeypatch):
    monkeypatch.delenv("KUBEASTRA_NO_KEYCHAIN", raising=False)
    assert desktop_secrets.keychain_disabled() is False


def test_get_secret_never_reaches_the_keychain(monkeypatch, exploding_keyring):
    monkeypatch.setenv("KUBEASTRA_NO_KEYCHAIN", "1")

    assert desktop_secrets.get_secret("llm.gemini") is None


def test_has_secret_never_reaches_the_keychain(monkeypatch, exploding_keyring):
    """`has_secret` delegates to `get_secret`, so one guard has to cover both."""
    monkeypatch.setenv("KUBEASTRA_NO_KEYCHAIN", "1")

    assert desktop_secrets.has_secret("llm.anthropic") is False


def test_a_warm_cache_does_not_defeat_the_opt_out(monkeypatch, exploding_keyring):
    """The guard sits before the cache, so the answer can't depend on call order."""
    desktop_secrets._secret_cache["llm.gemini"] = "sk-cached-value"
    monkeypatch.setenv("KUBEASTRA_NO_KEYCHAIN", "1")

    assert desktop_secrets.get_secret("llm.gemini") is None


def test_restore_to_environ_is_a_no_op_and_exports_nothing(monkeypatch, exploding_keyring):
    """The startup path build.sh actually exercises."""
    monkeypatch.setenv("KUBEASTRA_NO_KEYCHAIN", "1")
    for var in ("LLM_PROVIDER", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr(desktop_secrets, "_resolve_provider", lambda: None)
    assert desktop_secrets.restore_to_environ() is None

    import os

    assert "GEMINI_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ


def test_without_the_opt_out_the_keychain_is_consulted(monkeypatch):
    """The negative case: the guard must not disable credentials by default."""
    monkeypatch.delenv("KUBEASTRA_NO_KEYCHAIN", raising=False)
    seen = {}

    class _Keyring:
        @staticmethod
        def get_password(service, name):
            seen["name"] = name
            return "sk-live-value"

    monkeypatch.setattr(desktop_secrets, "_keyring", lambda: _Keyring())
    monkeypatch.setattr(desktop_secrets, "is_secure", lambda: True)

    assert desktop_secrets.get_secret("llm.gemini") == "sk-live-value"
    assert seen["name"] == "llm.gemini"
