from __future__ import annotations
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
MCP_DIR = BACKEND_DIR.parent.parent / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from config.settings import get_settings  # noqa: E402


def test_gitops_settings_have_safe_defaults():
    s = get_settings()
    assert s.gitops_enabled is False
    assert s.gitops_max_prs_per_hour_per_repo == 5
    assert s.app_base_url == ""
