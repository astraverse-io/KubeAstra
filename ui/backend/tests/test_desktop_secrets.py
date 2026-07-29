"""API-key storage.

The critical property is that an OS with no usable keychain must be *detected*
and reported, never silently accepted — a plaintext store that looks secure is
worse than an honest fallback.
"""

import importlib
import stat
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import desktop_secrets  # noqa: E402


class _FakeKeyring:
    """Stand-in for a working OS keychain."""

    __module__ = "keyring.backends.macOS"

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def get_keyring(self):
        return self

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise RuntimeError("no such password")
        del self.store[(service, name)]


class _FailKeyring(_FakeKeyring):
    """What keyring resolves to on a headless Linux box."""

    __module__ = "keyring.backends.fail"


@pytest.fixture
def secure(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBEASTRA_STATE_DIR", str(tmp_path))
    fake = _FakeKeyring()
    monkeypatch.setattr(desktop_secrets, "_keyring", lambda: fake)
    return fake


@pytest.fixture
def insecure(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBEASTRA_STATE_DIR", str(tmp_path))
    fake = _FailKeyring()
    monkeypatch.setattr(desktop_secrets, "_keyring", lambda: fake)
    return tmp_path


# ── keychain path ─────────────────────────────────────────────────────────


def test_round_trip(secure):
    desktop_secrets.set_secret("llm.anthropic", "sk-test")
    assert desktop_secrets.get_secret("llm.anthropic") == "sk-test"


def test_missing_secret_is_none(secure):
    assert desktop_secrets.get_secret("llm.nope") is None


def test_delete(secure):
    desktop_secrets.set_secret("llm.openai", "key")
    desktop_secrets.delete_secret("llm.openai")
    assert desktop_secrets.get_secret("llm.openai") is None


def test_delete_missing_is_not_an_error(secure):
    desktop_secrets.delete_secret("llm.never-set")  # must not raise


def test_is_secure_true_for_real_backend(secure):
    assert desktop_secrets.is_secure() is True


def test_list_configured_reports_names_only(secure):
    desktop_secrets.set_secret("llm.anthropic", "a")
    desktop_secrets.set_secret("embeddings.voyage", "b")
    names = desktop_secrets.list_configured()
    assert set(names) == {"llm.anthropic", "embeddings.voyage"}
    assert not any("a" == n or "b" == n for n in names)


# ── insecure fallback ─────────────────────────────────────────────────────


def test_fail_backend_detected_as_insecure(insecure):
    assert desktop_secrets.is_secure() is False


@pytest.mark.parametrize("module", [
    "keyring.backends.fail",
    "keyring.backends.chainer",   # not insecure — control case
])
def test_insecure_detection_matches_known_markers(monkeypatch, tmp_path, module):
    monkeypatch.setenv("KUBEASTRA_STATE_DIR", str(tmp_path))

    class Backend:
        pass

    Backend.__module__ = module
    holder = type("H", (), {"get_keyring": staticmethod(lambda: Backend())})
    monkeypatch.setattr(desktop_secrets, "_keyring", lambda: holder)

    expected_secure = "fail" not in module
    assert desktop_secrets.is_secure() is expected_secure


def test_fallback_round_trip(insecure):
    desktop_secrets.set_secret("llm.gemini", "fallback-key")
    assert desktop_secrets.get_secret("llm.gemini") == "fallback-key"


def test_fallback_file_is_owner_only(insecure):
    desktop_secrets.set_secret("llm.gemini", "fallback-key")
    path = insecure / "secrets.json"
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"secrets file is {oct(mode)}"


def test_fallback_delete(insecure):
    desktop_secrets.set_secret("llm.gemini", "k")
    desktop_secrets.delete_secret("llm.gemini")
    assert desktop_secrets.get_secret("llm.gemini") is None


def test_corrupt_fallback_treated_as_empty(insecure):
    (insecure / "secrets.json").write_text("{not json")
    assert desktop_secrets.get_secret("llm.gemini") is None
    # and remains writable afterwards
    desktop_secrets.set_secret("llm.gemini", "recovered")
    assert desktop_secrets.get_secret("llm.gemini") == "recovered"


def test_keyring_import_failure_reports_insecure(monkeypatch, tmp_path):
    """keyring itself can raise on import on unusual systems."""
    monkeypatch.setenv("KUBEASTRA_STATE_DIR", str(tmp_path))

    def boom():
        raise ImportError("no keyring")

    monkeypatch.setattr(desktop_secrets, "_keyring", boom)
    assert desktop_secrets.is_secure() is False
    assert desktop_secrets.backend_name() == "unavailable"


def test_module_reimports_cleanly():
    """Guard against import-time side effects creeping in."""
    importlib.reload(desktop_secrets)
