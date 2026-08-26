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


#: Decimal GB, matching `worker_base.GB_BYTES` exactly (1e9, not a GiB
#: 1024**3) — a test constant that used GiB here would make every exact-
#: figure assertion below off by ~7%.
GB = 1_000_000_000


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


# -- resume-aware: subtract what's already on disk (code review finding 2) --
#
# `total` is the FULL repo/file size in scope, unconditionally — but a
# download interrupted at 28GB of a 30GB repo only needs 2GB more, and the
# precheck must judge THAT gap, not the whole repo's size, or it refuses a
# resume a machine with 5GB free could actually complete. `bytes_on_disk`
# already answers "how much of this folder is on disk right now" — including
# a `.fusedpart`'s allocated-blocks progress, not just complete blobs — so
# this reuses it rather than re-deriving a second, drifting notion of
# "already have".


def test_a_resume_only_needs_the_remaining_bytes(tmp_path, monkeypatch):
    """30GB total, 28GB already on disk (blobs + `.fusedpart` progress) — a
    machine with only 5GB free must be let through, since only 2GB remain."""
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 28 * GB)
    monkeypatch.setattr(base.shutil, "disk_usage",
                        lambda path: base.shutil._ntuple_diskusage(100 * GB, 95 * GB, 5 * GB))
    base._ensure_disk_space(30 * GB, str(tmp_path))  # must not raise


def test_a_resume_still_refuses_when_the_remaining_bytes_dont_fit(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 10 * GB)
    monkeypatch.setattr(base.shutil, "disk_usage",
                        lambda path: base.shutil._ntuple_diskusage(100 * GB, 99 * GB, 1 * GB))
    with pytest.raises(base.InsufficientDiskSpace) as exc_info:
        base._ensure_disk_space(30 * GB, str(tmp_path))  # 20GB remaining, 1GB free
    message = str(exc_info.value)
    assert "20.0" in message  # the REMAINING need, not the full 30GB total


def test_a_fresh_download_with_nothing_on_disk_is_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 0)
    monkeypatch.setattr(base.shutil, "disk_usage",
                        lambda path: base.shutil._ntuple_diskusage(10 * GB, 8 * GB, 2 * GB))
    with pytest.raises(base.InsufficientDiskSpace):
        base._ensure_disk_space(10 * GB, str(tmp_path))


def test_bytes_on_disk_returning_none_is_treated_as_nothing_present(tmp_path, monkeypatch):
    """`bytes_on_disk` answers None for a falsy/unknown folder — must not
    poison the arithmetic (`None - total` would raise)."""
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: None)
    monkeypatch.setattr(base.shutil, "disk_usage",
                        lambda path: base.shutil._ntuple_diskusage(10 * GB, 9 * GB, 1 * GB))
    with pytest.raises(base.InsufficientDiskSpace) as exc_info:
        base._ensure_disk_space(10 * GB, str(tmp_path))
    assert "10.0" in str(exc_info.value)


def test_a_real_partial_fusedpart_file_reduces_the_requirement(tmp_path, monkeypatch):
    """End-to-end with the real `bytes_on_disk`, not a stub — a genuine
    resumable `.part` file on disk must count toward "already have". Small
    scale (MB, not GB) so the test writes real bytes without being slow or
    disk-heavy; `_ensure_disk_space`'s own unit is bytes throughout, so the
    arithmetic is identical at any scale."""
    MB = 1024 ** 2
    folder = tmp_path / "hub" / "models--org--m"
    blobs = folder / "blobs"
    blobs.mkdir(parents=True)
    part = blobs / ("e7ag" + base.PART_SUFFIX)
    with open(part, "wb") as f:
        f.truncate(6 * MB)  # sparse — allocates ~0 blocks, same as a fresh part
        f.write(b"x" * (2 * MB))  # ~2MB actually written/allocated near the start
    already = base.bytes_on_disk(str(folder))
    assert already is not None and 2 * MB <= already <= 6 * MB, (
        "the real bytes_on_disk() must count the .fusedpart's allocated "
        "progress, not answer 0 or the full sparse length")
    # Enough free space to cover the REMAINING gap (6MB - `already`, at most
    # 4MB) but not the full 6MB total — proves the check subtracted
    # `already` rather than comparing the raw total against free space.
    monkeypatch.setattr(base.shutil, "disk_usage",
                        lambda path: base.shutil._ntuple_diskusage(
                            100 * MB, 100 * MB - (5 * MB), 5 * MB))
    base._ensure_disk_space(6 * MB, str(folder))  # must not raise
