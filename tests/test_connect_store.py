"""connect.store: the file backend (0600, atomic, keyed safely) and the
Keychain backend's `security` calls (faked; the real Keychain is never touched)."""
import logging
import os
import stat
import subprocess

import pytest

from fused_render.connect import store
from fused_render.shell import storage

RECORD = {"kind": "mcp-oauth", "resource": "https://mcp.example/mcp", "client_id": "dcr-1",
          "access": "SECRET-ACCESS-123", "refresh": "SECRET-REFRESH-456",
          "expires_at": 1700000000.5, "account": "ada"}


@pytest.fixture
def filestore(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_STORE", "file")
    monkeypatch.setattr(storage, "home_dir", lambda: str(tmp_path))
    return tmp_path / "connect"


def test_backend_choice(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_STORE", "file")
    assert store.backend() == "file"
    monkeypatch.setenv("FUSED_RENDER_CONNECT_STORE", "keychain")
    assert store.backend() == "keychain"
    monkeypatch.delenv("FUSED_RENDER_CONNECT_STORE")
    monkeypatch.setattr(store.sys, "platform", "linux")
    assert store.backend() == "file"
    monkeypatch.setattr(store.sys, "platform", "darwin")
    monkeypatch.setattr(store.shutil, "which", lambda exe: "/usr/bin/security")
    assert store.backend() == "keychain"


def test_file_roundtrip_mode_and_no_leftovers(filestore):
    assert store.get("notion") is None
    store.put("notion", RECORD)
    path = filestore / "notion.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(filestore).st_mode) == 0o700
    assert store.get("notion") == RECORD
    store.put("notion", {**RECORD, "access": "NEW"})
    assert store.get("notion")["access"] == "NEW"
    assert sorted(os.listdir(filestore)) == ["notion.json"]  # no temp files left
    store.delete("notion")
    assert store.get("notion") is None
    store.delete("notion")  # idempotent


def test_failed_write_keeps_the_old_record(filestore, monkeypatch):
    store.put("slack", RECORD)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", boom)
    with pytest.raises(OSError):
        store.put("slack", {**RECORD, "access": "HALF"})
    assert store.get("slack") == RECORD
    assert sorted(os.listdir(filestore)) == ["slack.json"]


def test_corrupt_file_reads_as_absent(filestore):
    filestore.mkdir()
    (filestore / "github.json").write_text("{nope")
    assert store.get("github") is None
    (filestore / "github.json").write_text("[1, 2]")
    assert store.get("github") is None


@pytest.mark.parametrize("key", ["../etc", "a/b", "", "UPPER", ".hidden", None])
def test_bad_keys_are_refused(filestore, key):
    with pytest.raises(ValueError):
        store.get(key)
    with pytest.raises(ValueError):
        store.put(key, RECORD)


def test_record_must_be_a_dict(filestore):
    with pytest.raises(TypeError):
        store.put("notion", "token")


def test_no_record_content_in_logs(filestore, caplog):
    with caplog.at_level(logging.DEBUG, logger="fused_render.connect.store"):
        store.put("notion", RECORD)
        store.get("notion")
        store.delete("notion")
        (filestore / "notion.json").write_text("{broken SECRET-ACCESS-123")
        store.get("notion")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "notion" in text
    assert "SECRET" not in text


# -------------------------------------------------------------- keychain ---

class FakeSecurity:
    def __init__(self):
        self.items = {}
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        a = list(args)
        svc, acct = a[a.index("-s") + 1], a[a.index("-a") + 1]
        if a[0] == "add-generic-password":
            assert "-U" in a
            self.items[(svc, acct)] = a[a.index("-w") + 1]
            return subprocess.CompletedProcess(args, 0, "", "")
        if a[0] == "find-generic-password":
            assert a[-1] == "-w"
            if (svc, acct) not in self.items:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            return subprocess.CompletedProcess(args, 0, self.items[(svc, acct)] + "\n", "")
        if a[0] == "delete-generic-password":
            if self.items.pop((svc, acct), None) is None:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(a)


def test_keychain_backend_drives_security(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_STORE", "keychain")
    monkeypatch.setattr(storage, "home_dir", lambda: str(tmp_path))
    fake = FakeSecurity()
    monkeypatch.setattr(store, "_security", fake)
    assert store.get("slack") is None
    store.put("slack", {**RECORD, "account": "Zoë"})
    assert ("fused-render.connect.slack", "slack") in fake.items
    stored = fake.items[("fused-render.connect.slack", "slack")]
    assert stored.isascii() and "\n" not in stored
    assert store.get("slack") == {**RECORD, "account": "Zoë"}
    store.delete("slack")
    store.delete("slack")  # not found is fine
    assert store.get("slack") is None
    assert not (tmp_path / "connect").exists()  # nothing went to a file


def test_keychain_write_failure_raises_without_the_record(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_CONNECT_STORE", "keychain")
    monkeypatch.setattr(store, "_security",
                        lambda *a: subprocess.CompletedProcess(a, 1, "", "denied"))
    with pytest.raises(RuntimeError) as e:
        store.put("notion", RECORD)
    assert "SECRET" not in str(e.value)
    assert store.get("notion") is None  # a failed read is absent, not an exception
