"""Smoke tests for the ``kubeastra`` CLI.

These exercise Typer arg parsing, config load/save, and the friendly-error
behavior when the backend is unreachable. No live network calls — httpx
is monkey-patched where needed.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import httpx
from typer.testing import CliRunner

from kubeastra import __version__
from kubeastra.cli import app
from kubeastra.config import Config, config_path


runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Redirect the config dir to a per-test tmp path.

    Every test gets a fresh config file, so the CLI never touches the
    developer's real ~/.config/kubeastra.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # On Windows the CLI uses APPDATA — set both so the tests are portable.
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    yield


# ── Version + help ─────────────────────────────────────────────────────────


def test_version_flag_prints_version_and_exits():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("ask", "investigate", "connect", "doctor", "config"):
        assert cmd in result.stdout


def test_bare_invocation_shows_help():
    result = runner.invoke(app, [])
    # no_args_is_help=True => exit code 2 by Typer convention, but still
    # shows the help.
    assert "Usage" in result.stdout or "Usage" in result.output


# ── Config commands ────────────────────────────────────────────────────────


def test_config_set_persists_backend_url():
    result = runner.invoke(app, ["config", "set", "backend-url", "https://kubeastra.example.com"])
    assert result.exit_code == 0
    cfg = Config.load()
    assert cfg.backend_url == "https://kubeastra.example.com"


def test_config_get_prints_specific_value():
    runner.invoke(app, ["config", "set", "backend-url", "https://x.example.com"])
    result = runner.invoke(app, ["config", "get", "backend-url"])
    assert result.exit_code == 0
    assert "https://x.example.com" in result.stdout


def test_config_get_all_masks_api_token():
    runner.invoke(app, ["config", "set", "api-token", "ka_prod_abcdefghijklmnopqrst"])
    result = runner.invoke(app, ["config", "get"])
    assert result.exit_code == 0
    # Full token must NOT appear; masked segments should.
    assert "abcdefghijklmnopqrst" not in result.stdout
    assert "ka_p" in result.stdout


def test_config_set_rejects_unknown_key():
    result = runner.invoke(app, ["config", "set", "nonsense-key", "value"])
    assert result.exit_code == 1
    assert "unknown key" in result.stdout.lower() or "unknown key" in (result.stderr or "").lower()


def test_config_get_rejects_unknown_key():
    result = runner.invoke(app, ["config", "get", "nonsense-key"])
    assert result.exit_code == 1


def test_config_path_prints_absolute_path():
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    assert result.stdout.strip().endswith("config.toml")


def test_key_names_accept_hyphens_and_underscores():
    """`backend-url` and `backend_url` should both work."""
    r1 = runner.invoke(app, ["config", "set", "backend_url", "https://a.example.com"])
    assert r1.exit_code == 0
    r2 = runner.invoke(app, ["config", "set", "backend-url", "https://b.example.com"])
    assert r2.exit_code == 0
    cfg = Config.load()
    assert cfg.backend_url == "https://b.example.com"


# ── Config file behavior ───────────────────────────────────────────────────


def test_config_load_returns_defaults_when_no_file_exists():
    cfg = Config.load()
    assert cfg.backend_url == "http://localhost:8000"
    assert cfg.api_token is None


def test_config_load_survives_corrupt_file():
    """A bad config file must NOT crash the CLI — just fall back to defaults."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("this is not valid toml [[[")
    cfg = Config.load()
    assert cfg.backend_url == "http://localhost:8000"  # default preserved


def test_config_save_omits_unset_fields():
    """We don't want empty ``api_token = ""`` in the file — that's noise."""
    cfg = Config(backend_url="https://x.example.com", api_token=None, session_id=None)
    cfg.save()
    content = config_path().read_text()
    assert "backend_url" in content
    assert "api_token" not in content
    assert "session_id" not in content


# ── Investigate — arg validation ───────────────────────────────────────────


def test_investigate_requires_a_target():
    """No --pod / --deployment / --node → fail cleanly."""
    result = runner.invoke(app, ["investigate"])
    assert result.exit_code == 1
    assert "pod" in result.stdout.lower() or "pod" in (result.stderr or "").lower()


# ── Friendly errors when backend is unreachable ────────────────────────────


def test_ask_gives_friendly_error_when_backend_unreachable():
    """A ConnectError from httpx must be mapped to exit code 3 + a docker
    compose hint. Users see this on every first invocation, so it matters
    that it's clear."""

    def _boom(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    with patch("httpx.Client.get", side_effect=_boom), \
         patch("httpx_sse.connect_sse", side_effect=_boom):
        result = runner.invoke(app, ["ask", "why is redis pending"])
    assert result.exit_code == 3
    combined = (result.stdout or "") + (result.stderr or "") + result.output
    assert "connection error" in combined.lower()


def test_doctor_reports_unreachable_backend_as_fail():
    def _boom(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    with patch("httpx.Client.get", side_effect=_boom):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1  # doctor fails hard on any 'fail' check
    combined = (result.stdout or "") + (result.stderr or "") + result.output
    assert "fail" in combined.lower()


def test_doctor_reports_ok_when_backend_reachable():
    fake_response = httpx.Response(
        status_code=200,
        json={"in_cluster": False, "contexts": [{"name": "prod", "cluster": "prod-a"}]},
    )

    with patch("httpx.Client.get", return_value=fake_response):
        result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    combined = result.stdout + (result.stderr or "")
    assert "ok" in combined.lower()
