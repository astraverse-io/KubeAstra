import errno
import os
from pathlib import Path

from k8s.kubectl_runner import (
    _append_audit_entry,
    _audit_failure_category,
    _bounded_audit_entry,
)


def test_audit_entry_is_one_bounded_line():
    payload = _bounded_audit_entry("x" * 10000)
    assert len(payload) <= 4095
    assert payload.endswith(b"...[audit entry truncated]\n")
    assert payload.count(b"\n") == 1


def test_append_creates_parent_and_writes_entry(tmp_path):
    path = tmp_path / "nested" / "audit.log"
    _append_audit_entry(path, "first entry", 1024)
    assert path.read_text() == "first entry\n"


def test_rotation_uses_previous_file_and_keeps_new_entries(tmp_path):
    path = tmp_path / "audit.log"
    path.write_text("old-entry\n" * 20)
    _append_audit_entry(path, "new-entry", 32)

    rotated = tmp_path / "audit.log.1"
    assert rotated.exists()
    assert "old-entry" in rotated.read_text()
    current = path.read_text()
    assert "AUDIT_LOG_ROTATED" in current
    assert current.endswith("new-entry\n")


def test_append_retries_once_when_file_temporarily_missing(monkeypatch, tmp_path):
    path = tmp_path / "audit.log"
    real_open = os.open
    attempts = {"count": 0}

    def flaky_open(candidate, flags, mode=0o777):
        if Path(candidate) == path and attempts["count"] == 0:
            attempts["count"] += 1
            raise FileNotFoundError(errno.ENOENT, "rotation gap", str(candidate))
        return real_open(candidate, flags, mode)

    monkeypatch.setattr(os, "open", flaky_open)
    _append_audit_entry(path, "after-rotation", 1024)
    assert attempts["count"] == 1
    assert path.read_text() == "after-rotation\n"


def test_audit_failure_categories():
    assert _audit_failure_category(PermissionError(errno.EACCES, "denied")) == "permission"
    assert _audit_failure_category(OSError(errno.ENOSPC, "full")) == "disk_full"
    assert _audit_failure_category(FileNotFoundError(errno.ENOENT, "missing")) == "path_missing"
    assert _audit_failure_category(RuntimeError("unknown")) == "other"
