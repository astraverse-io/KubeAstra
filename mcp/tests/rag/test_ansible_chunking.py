"""Unit tests for chunking_ansible.py.

Focus areas:
  - Per-task chunking emits one chunk per top-level task with the right
    task_name, module, and role propagation.
  - Module extraction filters Ansible keywords and prefers FQCN-shaped
    keys; handles block/rescue recursion.
  - Per-play chunking emits one chunk per play with hosts/roles_list.
  - Custom-module chunking extracts the DOCUMENTATION block when present
    and falls back gracefully when not.
  - Per-role aggregate emitter pulls README + task names + default keys.
"""
from __future__ import annotations

import textwrap

from services.rag.chunking_ansible import (
    _extract_module,
    chunk_ansible_module,
    chunk_ansible_playbook,
    chunk_ansible_tasks,
)


# ── _extract_module ─────────────────────────────────────────────────────────

def test_extract_module_fqcn_preferred_over_bare():
    task = {"name": "Get nodes", "kubernetes.core.k8s_info": {"kind": "node"}}
    assert _extract_module(task) == "kubernetes.core.k8s_info"


def test_extract_module_skips_keywords():
    task = {
        "name": "Run shell command",
        "when": "x == 1",
        "register": "result",
        "vars": {"foo": "bar"},
        "tags": ["x"],
        "ansible.builtin.shell": "echo hi",
    }
    assert _extract_module(task) == "ansible.builtin.shell"


def test_extract_module_recurses_into_block():
    task = {
        "name": "Block for delegation",
        "block": [
            {"name": "Run shell", "ansible.builtin.shell": "uptime"},
        ],
        "rescue": [{"name": "Recover", "ansible.builtin.debug": {"msg": "x"}}],
    }
    assert _extract_module(task) == "ansible.builtin.shell"


def test_extract_module_bare_name_when_no_fqcn():
    task = {"name": "Old-style task", "shell": "ls"}
    assert _extract_module(task) == "shell"


def test_extract_module_returns_none_for_keywords_only():
    task = {"name": "Empty", "when": "true", "tags": ["x"]}
    assert _extract_module(task) is None


# ── chunk_ansible_tasks ─────────────────────────────────────────────────────

def test_chunk_tasks_emits_one_per_task():
    content = textwrap.dedent("""\
        - name: First task
          ansible.builtin.set_fact:
            x: 1
        - name: Second task
          ansible.builtin.shell: echo hi
    """)
    meta = {"role": "myrole", "category": "kubernetes", "role_subdir": "tasks"}
    chunks = chunk_ansible_tasks(content, meta)
    assert len(chunks) == 2
    assert chunks[0].extra["task_name"] == "First task"
    assert chunks[0].extra["module"] == "ansible.builtin.set_fact"
    assert chunks[0].extra["role"] == "myrole"
    assert chunks[0].extra["category"] == "kubernetes"
    assert chunks[0].extra["kind"] == "task"
    assert chunks[1].extra["task_name"] == "Second task"
    assert chunks[1].extra["module"] == "ansible.builtin.shell"


def test_chunk_tasks_block_flag_and_rescue():
    content = textwrap.dedent("""\
        - name: Block wrapper
          block:
            - name: Inner shell
              ansible.builtin.shell: echo
          rescue:
            - name: Recover
              ansible.builtin.debug:
                msg: oops
    """)
    chunks = chunk_ansible_tasks(content, {"role": "r", "role_subdir": "tasks"})
    assert len(chunks) == 1
    c = chunks[0]
    assert c.extra["task_name"] == "Block wrapper"
    assert c.extra["module"] == "ansible.builtin.shell"     # inner module
    assert c.extra["is_block"] is True
    assert c.extra["has_rescue"] is True


def test_chunk_tasks_yaml_parse_failure_falls_back_to_text():
    # Unquoted Jinja-in-value-position is the classic Ansible YAML edge case.
    content = "- name: bad\n  set_fact:\n    replicas: {{ replica | int }}\n"
    chunks = chunk_ansible_tasks(content, {"role": "r", "role_subdir": "tasks"})
    assert len(chunks) >= 1
    assert any(c.extra.get("parse_error") for c in chunks)


def test_chunk_tasks_handler_kind():
    content = "- name: restart\n  ansible.builtin.service:\n    name: x\n    state: restarted\n"
    chunks = chunk_ansible_tasks(content, {"role": "r", "role_subdir": "handlers"})
    assert chunks[0].extra["kind"] == "handler"


# ── chunk_ansible_playbook ──────────────────────────────────────────────────

def test_chunk_playbook_emits_one_per_play_with_metadata():
    content = textwrap.dedent("""\
        - hosts: web
          name: Deploy web
          roles:
            - common
            - role: nginx
              vars:
                port: 80
        - hosts: db
          name: Deploy db
          roles:
            - postgres
    """)
    chunks = chunk_ansible_playbook(content, {"play_group": "ops"})
    assert len(chunks) == 2
    assert chunks[0].extra["play_name"] == "Deploy web"
    assert chunks[0].extra["hosts"] == "web"
    assert chunks[0].extra["roles_list"] == "common,nginx"
    assert chunks[1].extra["play_name"] == "Deploy db"
    assert chunks[1].extra["roles_list"] == "postgres"


def test_chunk_playbook_unnamed_play_uses_index():
    content = "- hosts: localhost\n  tasks: []\n"
    chunks = chunk_ansible_playbook(content, {"play_group": "g"})
    assert chunks[0].extra["play_name"] == "play[0]"


# ── chunk_ansible_module ────────────────────────────────────────────────────

def test_chunk_module_extracts_documentation_block():
    content = textwrap.dedent("""\
        #!/usr/bin/python
        DOCUMENTATION = r'''
        ---
        module: my_module
        short_description: Does a thing
        options:
          name:
            type: str
        '''

        def main():
            pass
    """)
    chunks = chunk_ansible_module(content, {"module_name": "my_module"})
    assert len(chunks) == 1
    assert chunks[0].extra["doc_extracted"] is True
    assert "short_description" in chunks[0].text
    assert chunks[0].extra["module_name"] == "my_module"
    assert chunks[0].extra["kind"] == "custom_module"


def test_chunk_module_no_doc_block_falls_back():
    content = "import os\n\ndef main():\n    print('hi')\n"
    chunks = chunk_ansible_module(content, {"module_name": "mymod"})
    assert len(chunks) >= 1
    assert chunks[0].extra["doc_extracted"] is False
