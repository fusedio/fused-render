"""The OS-side wake stub for scheduled messages (fused_render/schedule_wake.py).

It never sends anything — it asks the platform to have the app RUNNING when
something is due, and the app's own first tick does the rest. On macOS that is a
LaunchAgent; everywhere else it is deliberately nothing (the supervisor's
existing start-at-login toggle is the only mechanism those platforms get).

`plist_xml` is pure, so most of this reads the file it would install without
installing one. The two tests that do go through `sync` stub `launchctl`.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from fused_render import schedule_wake


@pytest.fixture(autouse=True)
def no_launchctl(monkeypatch):
    """launchctl calls, recorded rather than run."""
    calls = []
    monkeypatch.setattr(schedule_wake, "_launchctl",
                        lambda *args: calls.append(args))
    return calls


@pytest.fixture()
def fake_darwin(monkeypatch, tmp_path):
    """Pretend to be macOS with a real app bundle, writing into tmp_path."""
    monkeypatch.setattr(schedule_wake, "_is_darwin", lambda: True)
    bundle = tmp_path / "FusedRender.app"
    bundle.mkdir()
    monkeypatch.setattr(schedule_wake, "app_bundle_path", lambda: str(bundle))
    plist = tmp_path / "LaunchAgents" / f"{schedule_wake.LABEL}.plist"
    monkeypatch.setattr(schedule_wake, "agent_plist_path", lambda: str(plist))
    return plist


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ----------------------------------------------------------- the plist itself

def test_plist_launches_the_bundle_without_stealing_focus():
    xml = schedule_wake.plist_xml("/Applications/FusedRender.app", [_utc(2030, 6, 1, 12)])

    assert "<string>/usr/bin/open</string>" in xml
    assert "<string>-g</string>" in xml  # a 3am wake must not take the foreground
    assert "<string>/Applications/FusedRender.app</string>" in xml
    assert schedule_wake.LABEL in xml
    # this file launches an app; it does not supervise one, and it does not own
    # the start-at-login decision
    assert "KeepAlive" not in xml
    assert "RunAtLoad" not in xml


def test_calendar_intervals_are_in_local_time_not_utc(monkeypatch):
    """StartCalendarInterval is evaluated against the user's own clock. Handing
    launchd a UTC hour would fire the app at the wrong time of day for everyone
    not on UTC."""
    due = _utc(2030, 6, 1, 23, 30)
    local = due.astimezone()

    xml = schedule_wake.plist_xml("/x.app", [due])

    assert f"<key>Hour</key>\n\t\t\t<integer>{local.hour}</integer>" in xml
    assert f"<key>Minute</key>\n\t\t\t<integer>{local.minute}</integer>" in xml
    assert f"<key>Day</key>\n\t\t\t<integer>{local.day}</integer>" in xml


def test_duplicate_times_collapse_and_the_list_is_bounded():
    same = [_utc(2030, 6, 1, 9, 0)] * 5
    assert schedule_wake.plist_xml("/x.app", same).count("<key>Hour</key>") == 1

    many = [_utc(2030, 6, 1) + timedelta(hours=i) for i in range(60)]
    xml = schedule_wake.plist_xml("/x.app", many)
    assert xml.count("<key>Hour</key>") == schedule_wake.MAX_INTERVALS


def test_the_soonest_times_are_the_ones_that_survive_the_cap():
    """The plist is a wake-up list, not the schedule: any launch fires the whole
    overdue set, so the nearest times are the ones that matter.

    Asserted against `_calendar_intervals` rather than the rendered XML — day and
    month depend on the runner's timezone, and comparing the two conversions
    against each other is the only form of this that is true in every zone."""
    times = [_utc(2030, 6, 1) + timedelta(days=i) for i in range(40)]

    # shuffled input: "soonest" is the sort's job, not the caller's
    got = schedule_wake._calendar_intervals(list(reversed(times)))

    assert got == schedule_wake._calendar_intervals(
        times[:schedule_wake.MAX_INTERVALS])
    assert len(got) == schedule_wake.MAX_INTERVALS


def test_an_app_path_with_xml_special_characters_is_escaped():
    xml = schedule_wake.plist_xml("/Apps/Fused & Render.app", [_utc(2030, 1, 1)])
    assert "Fused &amp; Render.app" in xml


# ------------------------------------------------------------------- syncing

def test_sync_writes_and_reloads_the_agent(fake_darwin, no_launchctl):
    installed = schedule_wake.sync(["2030-06-01T09:00:00+00:00"])

    assert installed is True
    assert os.path.exists(fake_darwin)
    assert schedule_wake.LABEL in open(fake_darwin, encoding="utf-8").read()
    # bootout BEFORE bootstrap: bootstrapping an already-loaded label is a no-op
    # that would leave the old intervals live
    assert [c[0] for c in no_launchctl] == ["bootout", "bootstrap"]


def test_sync_accepts_the_stored_iso_strings_and_drops_unreadable_ones(fake_darwin):
    schedule_wake.sync(["2030-06-01T09:00:00+00:00", "not a time",
                        datetime(2030, 6, 2, 9, tzinfo=timezone.utc)])

    # one malformed row must not cost the others their wake
    assert open(fake_darwin, encoding="utf-8").read().count("<key>Hour</key>") == 2


def test_nothing_pending_removes_the_agent(fake_darwin, no_launchctl):
    schedule_wake.sync(["2030-06-01T09:00:00+00:00"])
    assert os.path.exists(fake_darwin)

    assert schedule_wake.sync([]) is False
    assert not os.path.exists(fake_darwin)  # a wake with nothing to fire is waste


def test_no_app_bundle_means_no_agent(monkeypatch, fake_darwin):
    """A dev-from-source run or a plain wheel has nothing to relaunch. That is an
    ordinary state, not an error — and it must not leave a stale plist behind."""
    schedule_wake.sync(["2030-06-01T09:00:00+00:00"])
    monkeypatch.setattr(schedule_wake, "app_bundle_path", lambda: None)

    assert schedule_wake.sync(["2030-06-01T09:00:00+00:00"]) is False
    assert not os.path.exists(fake_darwin)


def test_off_darwin_it_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(schedule_wake, "_is_darwin", lambda: False)
    assert schedule_wake.sync(["2030-06-01T09:00:00+00:00"]) is False
    assert schedule_wake.remove() is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bundle-walk layout")
def test_app_bundle_path_walks_up_to_the_dot_app(monkeypatch, tmp_path):
    bundle = tmp_path / "FusedRender.app"
    exe = bundle / "Contents" / "MacOS" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(schedule_wake.sys, "executable", str(exe))

    assert schedule_wake.app_bundle_path() == str(bundle)


def test_app_bundle_path_is_none_outside_a_bundle(monkeypatch, tmp_path):
    exe = tmp_path / "venv" / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(schedule_wake.sys, "executable", str(exe))

    assert schedule_wake.app_bundle_path() is None
