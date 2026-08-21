"""storage.read_json / write_json under Windows sharing violations.

POSIX rename(2) is atomic and never refuses a concurrent replace, and a
concurrent open() for read is never refused either — which is what lets
storage.write_json promise "last write wins" with no locking above it (D3).
Windows gives neither for free: os.replace is backed by MoveFileExW, which
holds the destination for the duration of the swap, and a plain open() grants
no FILE_SHARE_DELETE — so a writer AND a reader can each be refused with a
transient PermissionError ([WinError 5] / [Errno 13]) for a few milliseconds.

Both halves are covered here because only fixing the writer left the reader
raising into live requests: the Windows CI run surfaced a PermissionError out
of read_json via shell/mounts/rcd.py's `_rcd_auth`, which is a request path.

`os.name` is patched rather than skipping off-Windows: the retry is gated on
os.name alone, so this pins the real branch on any host.
"""
import json
import os

import pytest

from fused_render.shell import storage


@pytest.fixture()
def no_sleep(monkeypatch):
    """The retry's own delay, removed — these tests assert the retry COUNT,
    and the real budget would otherwise spend seconds sleeping."""
    monkeypatch.setattr(storage.time, "sleep", lambda _s: None)


@pytest.fixture()
def as_windows(monkeypatch):
    monkeypatch.setattr(storage.os, "name", "nt")


def test_read_json_rides_out_a_transient_sharing_violation(
        tmp_path, monkeypatch, as_windows, no_sleep):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"secret": "v2"}), encoding="utf-8")
    real_open, calls = open, []

    def flaky_open(*a, **kw):
        calls.append(1)
        if len(calls) < 3:            # refused twice, then the writer lets go
            raise PermissionError(13, "Permission denied")
        return real_open(*a, **kw)

    monkeypatch.setattr("builtins.open", flaky_open)
    assert storage.read_json(str(path)) == {"secret": "v2"}
    assert len(calls) == 3


def test_a_missing_file_is_answered_at_once_and_never_retried(
        tmp_path, monkeypatch, as_windows, no_sleep):
    """read_json's "absent -> None" is a hot path (the bookmarks `exists`
    flag, the one-time import gate). Retrying it would spend the whole budget
    on the commonest case, so only PermissionError is retried."""
    calls = []
    real_open = open

    def counting_open(*a, **kw):
        calls.append(1)
        return real_open(*a, **kw)

    monkeypatch.setattr("builtins.open", counting_open)
    assert storage.read_json(str(tmp_path / "never-written.json")) is None
    assert len(calls) == 1


def test_a_corrupt_file_is_still_none_and_not_retried(
        tmp_path, monkeypatch, as_windows, no_sleep):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    calls = []
    real_open = open

    def counting_open(*a, **kw):
        calls.append(1)
        return real_open(*a, **kw)

    monkeypatch.setattr("builtins.open", counting_open)
    assert storage.read_json(str(path)) is None
    assert len(calls) == 1


def test_a_violation_that_outlives_the_budget_still_raises(
        tmp_path, monkeypatch, as_windows, no_sleep):
    """A file that is genuinely unreadable is not a millisecond of contention.
    Reporting it as None would silently degrade whatever read it (mount auth,
    say) instead of surfacing a real problem."""
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "builtins.open",
        lambda *a, **kw: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(PermissionError):
        storage.read_json(str(path))


def test_write_json_rides_out_a_transient_sharing_violation(
        tmp_path, monkeypatch, as_windows, no_sleep):
    path = tmp_path / "state.json"
    real_replace, calls = os.replace, []

    def flaky_replace(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", flaky_replace)
    storage.write_json(str(path), {"secret": "v3"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"secret": "v3"}
    assert len(calls) == 3


def test_posix_keeps_its_exact_single_call_behaviour(tmp_path, monkeypatch):
    """The retry must not change POSIX at all: one call, no sleep import in
    the hot path, and a PermissionError propagating immediately."""
    monkeypatch.setattr(storage.os, "name", "posix")
    monkeypatch.setattr(
        storage.time, "sleep",
        lambda _s: pytest.fail("POSIX must never sleep-and-retry"))
    path = tmp_path / "state.json"
    calls = []
    real_replace = os.replace

    def counting_replace(src, dst):
        calls.append(1)
        return real_replace(src, dst)

    monkeypatch.setattr(storage.os, "replace", counting_replace)
    storage.write_json(str(path), {"a": 1})
    assert len(calls) == 1
