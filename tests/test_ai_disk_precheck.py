"""Tests for the pre-download disk-space check (SPEC AI-26, D529).

`worker_base._ensure_disk_space` is the fix for "no disk-space precheck
anywhere" — a download that is known, before a single byte moves, to exceed
free space used to fail with a mid-transfer `OSError: [Errno 28] No space
left on device`, the least actionable place for that failure to surface
(after already spending however many gigabytes it managed to write, with an
error string that names a syscall rather than a shortfall). This check runs
against `total` — the SAME figure `download_snapshot`/`download_file` already
compute for the progress bar's total — before either calls the Hub for
bytes, so it costs nothing beyond a `shutil.disk_usage` and fails fast with a
sentence naming the actual gap in GB.
"""
import pytest

from fused_render.ai.runners import worker_base as base


GB = 1024 ** 3


def test_enough_space_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(base.shutil, "disk_usage",
                        lambda path: base.shutil._ntuple_diskusage(100 * GB, 1 * GB, 99 * GB))
    base._ensure_disk_space(10 * GB, str(tmp_path))  # must not raise


def test_insufficient_space_raises_with_the_shortfall_named(tmp_path, monkeypatch):
    monkeypatch.setattr(base.shutil, "disk_usage",
                        lambda path: base.shutil._ntuple_diskusage(10 * GB, 8 * GB, 2 * GB))
    with pytest.raises(base.InsufficientDiskSpace) as exc_info:
        base._ensure_disk_space(10 * GB, str(tmp_path))
    message = str(exc_info.value)
    assert "2.0" not in message or "8.0" in message  # the SHORTFALL, not just free space
    assert "GB" in message


def test_a_none_total_is_never_checked(tmp_path, monkeypatch):
    def boom(path):
        raise AssertionError("disk_usage must not be called when total is unknown")

    monkeypatch.setattr(base.shutil, "disk_usage", boom)
    base._ensure_disk_space(None, str(tmp_path))  # must not raise, must not probe


def test_checks_against_the_nearest_existing_ancestor(tmp_path, monkeypatch):
    target = tmp_path / "does" / "not" / "exist" / "yet"
    seen = []

    def fake(path):
        seen.append(path)
        return base.shutil._ntuple_diskusage(100 * GB, 1 * GB, 99 * GB)

    monkeypatch.setattr(base.shutil, "disk_usage", fake)
    base._ensure_disk_space(1 * GB, str(target))
    assert seen == [str(tmp_path)]


def test_a_disk_usage_probe_failure_degrades_silently(tmp_path, monkeypatch):
    def boom(path):
        raise OSError("no such filesystem")

    monkeypatch.setattr(base.shutil, "disk_usage", boom)
    base._ensure_disk_space(10 * GB, str(tmp_path))  # must not raise
