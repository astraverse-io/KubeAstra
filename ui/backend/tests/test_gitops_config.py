from __future__ import annotations
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from gitops.config import parse_config, overlay_for_env, env_for_cluster  # noqa: E402

CFG = """version: 1
gitops:
  layout: kustomize
  environments:
    - name: prod
      path: overlays/prod
      cluster: gke-prod-east-1
  labels: [kubeastra, auto-proposed]
"""


def test_missing_file_defaults_to_plain():
    cfg = parse_config(None)
    assert cfg.layout == "plain"
    assert cfg.labels == ["kubeastra"]


def test_parse_maps_env_and_cluster():
    cfg = parse_config(CFG)
    assert cfg.layout == "kustomize"
    assert overlay_for_env(cfg, "prod") == "overlays/prod"
    assert env_for_cluster(cfg, "gke-prod-east-1") == "prod"


def test_unknown_env_returns_none():
    cfg = parse_config(CFG)
    assert overlay_for_env(cfg, "dev") is None
