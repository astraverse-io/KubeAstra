"""Unit tests for LocalPathSource — globs, metadata derivation, skips."""
from __future__ import annotations

from pathlib import Path

import pytest

from services.rag.sources.local_path import LocalPathSource


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Build a miniature Ansible repo layout under a tmp dir."""
    (tmp_path / "roles" / "kubernetes" / "kube_check_health" / "tasks").mkdir(parents=True)
    (tmp_path / "roles" / "kubernetes" / "kube_check_health" / "tasks" / "main.yaml").write_text(
        "- name: noop\n  ansible.builtin.debug:\n    msg: hi\n"
    )
    (tmp_path / "roles" / "kubernetes" / "kube_check_health" / "README.md").write_text("# Role")
    (tmp_path / "roles" / "kubernetes" / "kube_check_health" / "templates").mkdir()
    (tmp_path / "roles" / "kubernetes" / "kube_check_health" / "templates" / "x.j2").write_text(
        "hello {{ name }}"
    )
    (tmp_path / "playbooks" / "ops").mkdir(parents=True)
    (tmp_path / "playbooks" / "ops" / "deploy.yaml").write_text("- hosts: all\n")
    (tmp_path / "playbooks" / "README.md").write_text("# Playbooks index")
    (tmp_path / "inventory" / "gcp" / "group_vars").mkdir(parents=True)
    (tmp_path / "inventory" / "gcp" / "group_vars" / "all.yaml").write_text("foo: bar\n")
    (tmp_path / "inventory" / "GCP_README.md").write_text("# GCP")
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "mymod.py").write_text(
        "#!/usr/bin/python\nDOCUMENTATION = r'''\nmodule: mymod\n'''\n"
    )
    # Things that should be skipped
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n")
    (tmp_path / "molecule").mkdir()
    (tmp_path / "molecule" / "scenario.yaml").write_text("- name: ignore me\n")
    (tmp_path / "vaulted.yaml").write_text(
        "$ANSIBLE_VAULT;1.1;AES256\nabcdef\n"
    )
    return tmp_path


def test_discover_picks_up_all_supported_types(fake_repo: Path):
    docs = list(LocalPathSource(str(fake_repo)).discover())
    by_type = {}
    for d in docs:
        by_type.setdefault(d.metadata["file_type"], []).append(d)
    assert sorted(by_type.keys()) == ["ansible_module", "jinja", "markdown", "yaml"]
    # 2 markdown (role README + playbooks index — GCP_README is at inventory depth=2
    #            so it's still ingested, just without environment metadata; that's
    #            tested separately below)
    assert len(by_type["markdown"]) == 3
    assert len(by_type["yaml"]) == 3   # tasks/main.yaml + deploy.yaml + group_vars/all.yaml
    assert len(by_type["jinja"]) == 1
    assert len(by_type["ansible_module"]) == 1


def test_metadata_derived_for_role_task_file(fake_repo: Path):
    docs = {d.metadata["path"]: d for d in LocalPathSource(str(fake_repo)).discover()}
    d = docs["roles/kubernetes/kube_check_health/tasks/main.yaml"]
    assert d.metadata["category"] == "kubernetes"
    assert d.metadata["role"] == "kube_check_health"
    assert d.metadata["role_subdir"] == "tasks"


def test_role_readme_has_no_role_subdir(fake_repo: Path):
    """A README at the role root is at depth 4 — should tag role+category
    but not set role_subdir (which would mis-name a file as a subdir)."""
    docs = {d.metadata["path"]: d for d in LocalPathSource(str(fake_repo)).discover()}
    d = docs["roles/kubernetes/kube_check_health/README.md"]
    assert d.metadata["role"] == "kube_check_health"
    assert d.metadata["category"] == "kubernetes"
    assert "role_subdir" not in d.metadata


def test_playbook_index_readme_is_not_a_play_group(fake_repo: Path):
    """playbooks/README.md at depth 2 should not be tagged with play_group."""
    docs = {d.metadata["path"]: d for d in LocalPathSource(str(fake_repo)).discover()}
    d = docs["playbooks/README.md"]
    assert "play_group" not in d.metadata


def test_inventory_top_level_readme_is_not_an_environment(fake_repo: Path):
    docs = {d.metadata["path"]: d for d in LocalPathSource(str(fake_repo)).discover()}
    d = docs["inventory/GCP_README.md"]
    assert "environment" not in d.metadata


def test_playbook_group_metadata(fake_repo: Path):
    docs = {d.metadata["path"]: d for d in LocalPathSource(str(fake_repo)).discover()}
    d = docs["playbooks/ops/deploy.yaml"]
    assert d.metadata["play_group"] == "ops"


def test_inventory_env_metadata(fake_repo: Path):
    docs = {d.metadata["path"]: d for d in LocalPathSource(str(fake_repo)).discover()}
    d = docs["inventory/gcp/group_vars/all.yaml"]
    assert d.metadata["environment"] == "gcp"
    assert d.metadata["inventory_kind"] == "group_vars"


def test_vault_encrypted_file_skipped(fake_repo: Path):
    docs = list(LocalPathSource(str(fake_repo)).discover())
    titles = [d.title for d in docs]
    assert "vaulted.yaml" not in titles


def test_skip_dir_segments(fake_repo: Path):
    docs = list(LocalPathSource(str(fake_repo)).discover())
    paths = [d.metadata["path"] for d in docs]
    assert not any("molecule" in p.split("/") for p in paths)
    assert not any(".git" in p.split("/") for p in paths)


def test_library_python_picked_up_but_other_python_not(tmp_path: Path):
    (tmp_path / "library").mkdir()
    (tmp_path / "library" / "mod.py").write_text(
        "DOCUMENTATION = r'''m: x'''\n"
    )
    # A python file NOT under library/ should be ignored
    (tmp_path / "helpers").mkdir()
    (tmp_path / "helpers" / "util.py").write_text("def x(): pass\n")
    docs = list(LocalPathSource(str(tmp_path)).discover())
    types = {d.metadata["path"]: d.metadata["file_type"] for d in docs}
    assert types == {"library/mod.py": "ansible_module"}
