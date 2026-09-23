"""kubeastra.yaml — optional repo config mapping envs to overlays and clusters."""
from __future__ import annotations

import yaml
from pydantic import BaseModel, Field


class Environment(BaseModel):
    name: str
    path: str
    cluster: str | None = None


class KubeastraConfig(BaseModel):
    layout: str = "plain"
    environments: list[Environment] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=lambda: ["kubeastra"])
    branch_prefix: str = "kubeastra/"


def parse_config(text: str | None) -> KubeastraConfig:
    if not text:
        return KubeastraConfig()
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return KubeastraConfig()
    section = (raw.get("gitops") or {}) if isinstance(raw, dict) else {}
    return KubeastraConfig(**section)


def overlay_for_env(cfg: KubeastraConfig, env: str | None) -> str | None:
    if not env:
        return None
    for e in cfg.environments:
        if e.name == env:
            return e.path
    return None


def env_for_cluster(cfg: KubeastraConfig, cluster: str | None) -> str | None:
    if not cluster:
        return None
    for e in cfg.environments:
        if e.cluster == cluster:
            return e.name
    return None
