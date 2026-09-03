"""Self-fix — a Claude session on the app's own installation (SPEC §48, D655).

What is actually at stake here is a promise the UI makes on the app's behalf:
*this copy has been changed, and reinstalling gets you a clean one.* Both halves
can be broken silently. A mark that fails to appear leaves a user running
somebody's patch with a confident version number on it; a mark that fails to
CLEAR turns a permanent badge onto an installation that is, by then, pristine.

So the tests below are mostly about the three ways an installation stops being
modified — an upgrade, a same-version reinstall, and the user saying so — and
about the one thing the digest must not notice: the app merely being run.
"""
import json
import os
import platform
import shlex
import threading

import pytest
from fastapi.testclient import TestClient

from fused_render import __version__, claude_spawn, selffix
from fused_render.server import create_app
from fused_render.server.routers import selffix as selffix_routes

# Captured before the autouse pin below replaces it, so the one test that is
# ABOUT the resolution can exercise the real thing.
REAL_CLAUDE_FOUND = selffix_routes._claude_found
# Same idea for the stamp watcher, which `_no_detached_watcher` stubs out for
# every test: the two that are ABOUT it call this and get the real thing.
REAL_WATCH_FIX = selffix_routes._watch_fix


@pytest.fixture()
def install(tmp_path, monkeypatch):
    """A fake installation. Every location in the module resolves through
    `install_root`, so redirecting that one function moves the whole feature
    into a tmp dir — no test may write into the developer's real package."""
    root = tmp_path / "site-packages" / "fused_render"
    (root / "server").mkdir(parents=True)
    (root / "__init__.py").write_text('__version__ = "9.9.9"\n')
    (root / "jobs.py").write_text("RUNNING = 'running'\n")
    (root / "server" / "app.py").write_text("def create_app(): ...\n")
    monkeypatch.setattr(selffix, "install_root", lambda: str(root))
    # The home dir too, for the same reason one line up. The module reads BOTH
    # records homes now (`record_homes`), and the suite's home is per-worker
    # rather than per-test — so without this a test that writes out of tree
    # (every diagnostic one) leaves its reports in the listing every later test
    # in the same worker sees.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return root


@pytest.fixture(autouse=True)
def _pin_the_claude_cli(monkeypatch):
    """Every test starts believing Claude Code is installed.

    The start route refuses before it reads the body when the CLI is absent
    (SF-13f), so without this pin every session test would pass or fail on
    whether the machine running the suite happens to have `claude` on its PATH
    — green on a developer's laptop, 502 on a CI runner that has no Claude.
    Same reasoning as conftest's `_pin_the_script_interpreter_resolution`: the
    ordinary case is stated, and the tests that are ABOUT the missing CLI say so
    themselves.
    """
    monkeypatch.setattr(selffix_routes, "_claude_found", lambda: True)


@pytest.fixture(autouse=True)
def _no_detached_watcher(monkeypatch):
    """No test may leave the real stamp watcher running.

    `/api/selffix/start` finishes by launching `_watch_fix` on a DAEMON thread
    (routers/selffix.py) — that is the whole point of it: nobody polls a run
    started from an HTTP request, so the stamp has to outlive the request. It
    also outlives the test, and that is the problem. The thread re-resolves
    `state_dir()` on every ~16s tick, so once this test's `install` fixture has
    torn its `install_root` patch down, the next tick stamps the DEVELOPER'S
    REAL PACKAGE — which is exactly what happened: a `modified.json` under
    `fused_render/.fused-render-selffix/` whose recorded incident path was a
    relative climb back out to the tmp install it was actually started in.

    Gitignored, so it never showed up in `git status`; the tell was a later test
    reading a marker it had not written.

    Same shape and same remedy as conftest's `_no_real_user_plugin_sync` and
    `_no_schedule_loop_thread`: the redirect fixtures move WHERE things are
    written, but a thread nobody joins outlives the redirect, so the only sound
    boundary is stopping the spawn. The two tests that are ABOUT the watcher go
    through `REAL_WATCH_FIX`, captured above — the same escape hatch
    `REAL_CLAUDE_FOUND` already gives the missing-CLI test, for the same reason.
    """
    monkeypatch.setattr(selffix_routes, "_watch_fix", lambda *a, **k: None)


@pytest.fixture()
def client(tmp_path, install):
    return TestClient(create_app(start_dir=str(tmp_path)))


def post(client, url, body=None):
    return client.post(url, json=body if body is not None else {},
                       headers={"X-Fused": "1"})


# The digest the current test's session starts from. `settle` measures against
# THIS, not against the release — see its docstring — so a test that stamps has
# to open a session first, exactly as the start route does.
BEFORE = [""]


def _pristine():
    """Begin a session on the install as it currently stands."""
    _, BEFORE[0] = selffix.begin_session()
    return BEFORE[0]


# ---------------------------------------------------------------- the digest


def test_running_the_app_is_not_a_modification(install):
    """Byte-caches are written by the act of importing. If they counted, every
    installation would be 'modified' the first time it started."""
    before = selffix.tree_digest()
    cache = install / "__pycache__"
    cache.mkdir()
    (cache / "jobs.cpython-312.pyc").write_bytes(b"\x00\x01")
    (install / "server" / "app.pyc").write_bytes(b"\x00")
    assert selffix.tree_digest() == before


def test_a_file_that_is_briefly_unlocked_does_not_look_like_an_edit(install):
    """A read that fails ONCE must not move the digest.

    `settle` reads any difference between two walks as "this session changed
    the install", so a file that is unreadable in one walk and readable in the
    next stamps a marker and points the badge at a session that edited nothing
    — the exact outcome SF-7b exists to prevent. On Windows that is routine
    rather than exotic: an antivirus or the indexer holds a sharing lock for a
    few milliseconds and lets go.
    """
    before = selffix.tree_digest()

    real = selffix._file_digest
    target = str(install / "jobs.py")
    failed: list[str] = []

    def flaky(path: str) -> str:
        if path == target and not failed:
            failed.append(path)
            raise OSError(32, "The process cannot access the file")
        return real(path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(selffix, "_file_digest", flaky)
        assert selffix.tree_digest() == before
    assert failed, "the test never exercised the failing read"


def test_a_file_that_stays_unreadable_still_reads_as_a_different_TREE(install):
    """The property the retry must not cost: a tree we cannot fully read must
    not hash equal to one we can — otherwise an unreadable file that really did
    change would hide behind the read failure. It must also be STABLE, since a
    permanent failure that folded a different token each walk would be the same
    false stamp by another route."""
    before = selffix.tree_digest()

    real = selffix._file_digest
    target = str(install / "jobs.py")

    def always_locked(path: str) -> str:
        if path == target:
            raise OSError(13, "Permission denied")
        return real(path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(selffix, "_file_digest", always_locked)
        locked = selffix.tree_digest()
        assert locked != before
        assert locked == selffix.tree_digest()


def test_the_retry_is_BUDGETED_so_an_unreadable_subtree_stays_cheap(install):
    """The other half of the retry, and it needs its own test because the
    transient case passes with or without a cap.

    A retry per failed read is right for a lock that clears; it is wrong for a
    directory that is genuinely unreadable, where it doubles the reads and adds
    a sleep to each one — on a tree of thousands of files, minutes added to a
    walk that runs every ~16s during a live session. Past the budget the token
    is folded straight in, which is the old behaviour and correct for a failure
    that is not transient.
    """
    for i in range(8):
        (install / f"mod{i}.py").write_text(f"X = {i}\n")

    calls: list[str] = []

    def always_locked(path: str) -> str:
        calls.append(path)
        raise OSError(13, "Permission denied")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(selffix, "_file_digest", always_locked)
        mp.setattr(selffix, "_UNREADABLE_RETRIES", 2)
        mp.setattr(selffix, "_UNREADABLE_RETRY_S", 0)
        selffix.tree_digest()

    files = len(set(calls))
    assert files >= 9, f"expected the tree to hold the 8 new files, saw {files}"
    # One read each, plus AT MOST the budget in retries — never one retry per
    # file, which is what an uncapped retry would spend (it would be 2 * files).
    assert len(calls) == files + 2


def test_a_developers_in_tree_venv_is_not_part_of_the_installation(install):
    """`.venv` is derived, never shipped, and never ours.

    D630 (main's) made `<project>/.venv` the standard layout, and the one place
    that can land inside the install tree is a source checkout where a developer
    has hand-synced a template's own venv — `core_templates`' staging `ignore`
    exists for exactly that folder. The app itself never creates one here
    (`projectenv._use_home_store` forces the home store for anything inside the
    package), so hashing it buys nothing and costs a walk over thousands of
    files on every ~16s re-hash of a live session.
    """
    before = selffix.tree_digest()
    venv = install / "templates" / "notebook" / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "python").write_bytes(b"\x7fELF")
    assert selffix.tree_digest() == before


def test_the_state_dir_does_not_modify_the_installation_it_describes(install):
    """The incident file a fix session reads lives inside the install tree —
    so writing it must not be a change the same run then reports."""
    before = selffix.tree_digest()
    selffix.record_incident({"title": "boom", "message": "Traceback…"})
    assert selffix.tree_digest() == before


def test_an_edit_moves_the_digest(install):
    before = selffix.tree_digest()
    (install / "jobs.py").write_text("RUNNING = 'running'  # patched\n")
    assert selffix.tree_digest() != before


def test_a_rename_is_as_visible_as_an_edit(install):
    before = selffix.tree_digest()
    os.rename(install / "jobs.py", install / "jobs2.py")
    assert selffix.tree_digest() != before


# ----------------------------------------------------------------- the mark


def test_settle_marks_only_when_the_tree_actually_moved(install):
    _pristine()
    assert selffix.settle(before=BEFORE[0], run_id="r1") is False
    assert selffix.status() is None

    (install / "jobs.py").write_text("RUNNING = 'running'  # patched\n")
    assert selffix.settle(before=BEFORE[0], run_id="r1", report=str(install / ".x" / "r.md")) is True
    state = selffix.status()
    assert state is not None
    assert state["modified"] is True
    assert state["version"] == __version__
    assert len(state["fixes"]) == 1


def test_repeated_stamps_from_one_session_stay_one_fix(install):
    """The watcher re-checks every few ticks so the badge appears while the user
    watches — appending per check would show one conversation as a column."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    for _ in range(4):
        selffix.settle(before=BEFORE[0], run_id="r1", title="download failed")
    state = selffix.status()
    assert [f["run_id"] for f in state["fixes"]] == ["r1"]

    selffix.settle(before=BEFORE[0], run_id="r2")
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1", "r2"]


def test_report_paths_survive_the_installation_being_moved(install, tmp_path,
                                                           monkeypatch):
    """Stored relative to the state dir, absolutised on read — a bundle dragged
    from the DMG to /Applications must not lose its own report."""
    _pristine()
    incident, report = selffix.record_incident({"title": "boom"})
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1", report=report, incident=incident)

    moved = tmp_path / "Applications" / "fused_render"
    moved.parent.mkdir(parents=True, exist_ok=True)
    os.rename(install, moved)
    monkeypatch.setattr(selffix, "install_root", lambda: str(moved))

    latest = selffix.status()["latest_report"]
    assert latest is not None
    assert latest.startswith(str(moved))
    assert os.path.exists(latest)


def test_a_session_that_changed_nothing_is_not_recorded_as_a_fix(install):
    """`settle` measures against the tree as THIS session found it, not against
    the release. On an install an earlier session already changed, the two are
    different before the new session has done anything — so measuring against
    the release would record a do-nothing session as a fix, and make its own
    empty report the one the badge points at."""
    _pristine()
    (install / "jobs.py").write_text("patched by the first session\n")
    selffix.settle(before=BEFORE[0], run_id="r1", title="first")
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1"]

    # A second session opens on the ALREADY-MODIFIED tree and edits nothing.
    _, before2 = selffix.begin_session()
    assert selffix.settle(before=before2, run_id="r2", title="second") is False
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1"]


def test_a_no_op_session_does_not_re_light_a_dismissed_badge(install):
    """Dismissing is a decision the user made about this machine. A later
    session that changed nothing must not overturn it."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.clear() is True

    _, before2 = selffix.begin_session()
    assert selffix.settle(before=before2, run_id="r2") is False
    assert selffix.status() is None


def test_a_second_session_that_does_change_something_is_recorded(install):
    """The other half — the guard above must not swallow a real second fix."""
    _pristine()
    (install / "jobs.py").write_text("patched once\n")
    selffix.settle(before=BEFORE[0], run_id="r1")

    _, before2 = selffix.begin_session()
    (install / "server" / "app.py").write_text("patched twice\n")
    assert selffix.settle(before=before2, run_id="r2") is True
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1", "r2"]


def test_the_pristine_baseline_survives_a_session_on_a_modified_tree(install):
    """`begin_session` must not re-baseline against an already-patched tree, or
    `reconcile` would lose its only picture of what the release shipped."""
    pristine_file = (install / "jobs.py").read_text()
    baseline, _ = selffix.begin_session()
    (install / "jobs.py").write_text("patched\n")

    baseline2, before2 = selffix.begin_session()
    assert baseline2 == baseline          # still the release
    assert before2 != baseline            # ...and this session knows it differs


# ------------------------------------------------- ...and the three ways out


def test_an_upgrade_clears_the_mark_on_sight(install):
    """`pip uninstall` only removes what its RECORD lists, so a marker can
    outlive the install it described. The version stamp is what catches that,
    and it has to be caught on the READ path — the badge must be gone the
    moment the new version serves a request, not after the next restart."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is not None

    marker = json.loads(open(selffix.marker_path()).read())
    marker["version"] = "0.0.1"
    with open(selffix.marker_path(), "w") as f:
        json.dump(marker, f)

    assert selffix.status() is None
    # ...and it is gone, not merely hidden: a later downgrade must not find it.
    assert not os.path.exists(selffix.marker_path())


def test_a_same_version_reinstall_clears_the_mark(install):
    """The case the version stamp cannot see — and the obvious thing a user
    does when the app misbehaves. `reconcile` is the only thing that catches
    it, which is why it hashes the tree at every start where a marker exists."""
    pristine = (install / "jobs.py").read_text()
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is not None

    (install / "jobs.py").write_text(pristine)  # the reinstall
    selffix.reconcile()
    assert selffix.status() is None


def test_reconcile_leaves_a_still_modified_install_alone(install):
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    selffix.reconcile()
    assert selffix.status() is not None


def test_reconcile_refreshes_a_digest_that_drifted_further(install):
    """A resumed conversation can change more files after the watcher gave up.
    The record has to follow, or the 'restored' test above compares against a
    tree that no longer exists anywhere."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    first = json.loads(open(selffix.marker_path()).read())["digest"]

    (install / "jobs.py").write_text("patched twice\n")
    selffix.reconcile()
    assert json.loads(open(selffix.marker_path()).read())["digest"] != first
    assert selffix.status() is not None


def test_clear_forgets_the_mark_but_keeps_the_report(install):
    """The user's own override. The badge is a claim about this machine and the
    person at it may have settled it by hand — but the record of what was
    changed is not theirs to lose by dismissing a badge."""
    _pristine()
    incident, report = selffix.record_incident({"title": "boom"})
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1", report=report, incident=incident)

    assert selffix.clear() is True
    assert selffix.status() is None
    assert os.path.exists(report)
    assert selffix.clear() is False


def test_dismissing_mid_session_is_not_undone_by_the_next_stamp(install):
    """The likeliest moment to dismiss is while watching the session that raised
    the badge — and the watcher re-stamps every few ticks and once more when the
    turn ends. Without remembering WHAT was dismissed, the user's click was
    undone seconds later by the next stamp of the very same change."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is not None

    assert selffix.clear() is True
    # ...the watcher keeps going, and the tree still differs from `before`.
    selffix.settle(before=BEFORE[0], run_id="r1")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is None


def test_a_dismissal_covers_only_the_state_it_was_made_for(install):
    """"I have seen this and do not want a badge for it" — not "never badge me
    again". A session that goes on to change something ELSE moves the digest
    past the dismissed one, and the badge legitimately comes back."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    selffix.clear()
    assert selffix.status() is None

    (install / "server" / "app.py").write_text("and this too\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is not None
    # ...and the spent dismissal is retired, not left to silence a later one.
    assert not os.path.exists(selffix.dismissed_path())


def test_a_dismissal_expires_with_the_version_it_was_made_on(install, monkeypatch):
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    selffix.clear()

    stale = json.loads(open(selffix.dismissed_path()).read())
    stale["version"] = "0.0.1"
    with open(selffix.dismissed_path(), "w") as f:
        json.dump(stale, f)
    assert selffix.dismissed_digest() == ""


def test_a_dismiss_during_settles_own_walk_still_wins(install, monkeypatch):
    """`settle` tests the dismissal BEFORE a tree walk that takes long enough
    for the user to click Dismiss in between — and `mark_modified` would then
    delete a dismissal it never saw. The same window SF-16 closed for
    `reconcile`, one function over: the authoritative check is under the lock."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is not None

    real_digest = selffix.tree_digest

    def digest_then_dismiss(*args, **kwargs):
        out = real_digest(*args, **kwargs)
        monkeypatch.setattr(selffix, "tree_digest", real_digest)
        selffix.clear()  # the user dismisses while settle is mid-walk
        return out

    monkeypatch.setattr(selffix, "tree_digest", digest_then_dismiss)
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is None


def test_reconcile_does_not_clear_a_fix_that_landed_during_its_own_walk(
        install, monkeypatch):
    """The half re-reading the marker does not cover.

    `reconcile` re-reads the marker under the lock so it never writes back a
    stale OBJECT. But it still DECIDES from `current`, measured before that
    read, and the two come apart exactly when a fix session lands mid-walk: the
    walk sees a tree that still looks pristine, the marker it then reads is the
    new one that session just wrote, and "restored — clear the mark" throws away
    the record of a fix that had just been made. Losing that is the one thing
    the feature must not do, since the report it points at is the whole
    deliverable.
    """
    pristine = (install / "jobs.py").read_text()
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    (install / "jobs.py").write_text(pristine)  # the walk will see it restored

    real_digest = selffix.tree_digest

    def digest_then_a_fix_lands(*args, **kwargs):
        out = real_digest(*args, **kwargs)  # pristine: "nothing to mark"
        monkeypatch.setattr(selffix, "tree_digest", real_digest)
        before = selffix.begin_session()[1]
        (install / "jobs.py").write_text("patched again\n")
        selffix.settle(before=before, run_id="r2")  # a second fix, mid-walk
        return out

    monkeypatch.setattr(selffix, "tree_digest", digest_then_a_fix_lands)
    selffix.reconcile()

    assert selffix.status() is not None, (
        "reconcile cleared a marker written after the walk it decided from")
    assert json.loads(open(selffix.marker_path()).read())["digest"] == real_digest(), (
        "the marker survived but was overwritten with the stale walk's digest")


def test_a_source_checkout_does_not_count_its_own_frontend_rebuild(install):
    """`scripts/dev.sh` runs `vite build --watch` beside the server, so on a
    checkout the build output is rewritten whenever the developer touches a
    frontend file — concurrently with a fix session, which would then be blamed
    for it."""
    (install.parent / ".git").mkdir()  # now a source checkout
    dist = install / "static" / "shell-dist" / "assets"
    dist.mkdir(parents=True)
    (dist / "index-abc123.js").write_text("built\n")

    before = selffix.tree_digest()
    (dist / "index-abc123.js").unlink()
    (dist / "index-def456.js").write_text("rebuilt\n")  # a vite rebuild
    assert selffix.tree_digest() == before

    # ...and real source under it is still covered.
    (install / "jobs.py").write_text("patched\n")
    assert selffix.tree_digest() != before


def test_a_linked_worktree_counts_as_a_source_checkout(install):
    """In a linked worktree `.git` is a FILE holding a `gitdir:` pointer, not a
    directory — and this repo's own dev setup uses worktrees, which is exactly
    where the build output churns under `vite build --watch`. An `isdir` test
    called those shipped installs and handed them the false badge SF-15a
    exists to prevent."""
    (install.parent / ".git").write_text("gitdir: /repo/.git/worktrees/wt\n")
    assert selffix.is_source_checkout() is True
    assert selffix.install_method() == "source"


def test_a_shipped_install_still_hashes_its_build_output(install):
    """The other half: nothing rewrites shell-dist on a real install, and a
    session that hand-patched the bundle really has modified the app."""
    assert not selffix.is_source_checkout()  # no .git above the package
    dist = install / "static" / "shell-dist" / "assets"
    dist.mkdir(parents=True)
    (dist / "index-abc123.js").write_text("built\n")

    before = selffix.tree_digest()
    (dist / "index-abc123.js").write_text("hand-patched\n")
    assert selffix.tree_digest() != before


def test_reconcile_does_not_resurrect_a_marker_cleared_while_it_walked(install,
                                                                      monkeypatch):
    """`reconcile` hashes the whole tree before it writes, which is long enough
    for the user to dismiss the badge. Writing back the object read BEFORE the
    walk would silently undo that."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")

    real_digest = selffix.tree_digest

    def digest_then_dismiss(*args, **kwargs):
        # The user clicks Dismiss while the walk is in flight.
        out = real_digest(*args, **kwargs)
        selffix.clear()
        monkeypatch.setattr(selffix, "tree_digest", real_digest)
        return out

    monkeypatch.setattr(selffix, "tree_digest", digest_then_dismiss)
    selffix.reconcile()
    assert selffix.status() is None


def test_reconcile_does_not_drop_a_fix_recorded_while_it_walked(install,
                                                               monkeypatch):
    """The other side of the same race: a watcher's stamp landing mid-walk must
    not be replaced by the pre-walk snapshot, losing its report."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")

    real_digest = selffix.tree_digest

    def digest_then_stamp(*args, **kwargs):
        out = real_digest(*args, **kwargs)
        monkeypatch.setattr(selffix, "tree_digest", real_digest)
        # A second session's watcher records its own fix while we walk.
        selffix.mark_modified(run_id="r2", title="second", digest="drifted")
        return out

    monkeypatch.setattr(selffix, "tree_digest", digest_then_stamp)
    selffix.reconcile()
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1", "r2"]


def test_a_lost_baseline_is_recovered_from_the_marker_not_from_the_patched_tree(
        install):
    """The badge must never be cleared by re-baselining the patch itself.

    `ensure_baseline` takes the tree in front of it as pristine, which is only
    honest on the first session of a version. The baseline file can go missing
    under a LIVE marker — the fix session is an agent editing this installation
    and can delete its state dir, and the very first write can have failed with
    `OSError` — and re-taking it from the patched tree makes `reconcile` find
    current == "pristine" on the next start and clear a badge for a change that
    is still on disk.

    The marker already carries the pristine digest, so the repair is exact
    rather than defensive.
    """
    pristine, _ = selffix.begin_session()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=pristine, run_id="r1")
    assert selffix.status() is not None
    assert selffix.status()["fixes"][0]["run_id"] == "r1"

    os.unlink(selffix.baseline_path())          # the file goes missing
    recovered, before = selffix.begin_session()  # a second session opens
    assert recovered == pristine                 # NOT the patched tree
    assert before != pristine

    # ...and the point of all that: reconcile still knows this tree is patched.
    selffix.reconcile()
    assert selffix.status() is not None, "the badge was cleared over a live patch"


def test_with_no_pristine_digest_anywhere_the_badge_is_kept_not_cleared(install):
    """The other half: a marker whose own `baseline_digest` is empty.

    That is the marker a session stamps when its baseline write failed, so
    nothing on disk knows what the release shipped. Guessing from the patched
    tree would clear the badge; the honest answer is to write no baseline at all
    and leave the badge standing. A badge that outstays its modification is
    cosmetic — one that vanishes over a live patch is the failure this feature
    exists to prevent.
    """
    (install / "jobs.py").write_text("patched\n")
    selffix.mark_modified(run_id="r1", digest=selffix.tree_digest(),
                          baseline_digest="")
    assert not os.path.exists(selffix.baseline_path())
    assert (selffix.status() or {}).get("modified") is True

    baseline, before = selffix.begin_session()
    assert baseline == ""                                  # no claim made
    assert before                                          # ...but this session
    assert not os.path.exists(selffix.baseline_path())     # ...and none written
    selffix.reconcile()
    assert selffix.status() is not None, "the badge was cleared with no baseline"


def test_a_new_version_starts_a_fresh_baseline(install, monkeypatch):
    """An upgrade legitimately replaced the tree the old baseline described;
    trusting it would report every upgrade as a modification."""
    first, _ = selffix.begin_session()
    (install / "jobs.py").write_text("the next release\n")
    # ensure_baseline compares against the module's own __version__ binding.
    stale = json.loads(open(selffix.baseline_path()).read())
    stale["version"] = "0.0.1"
    with open(selffix.baseline_path(), "w") as f:
        json.dump(stale, f)
    second, _ = selffix.begin_session()
    assert second != first


# ------------------------------------------------------ incidents & reports


def test_the_report_exists_before_the_session_writes_a_word(install):
    """A version chip that promises a report must always have a file to open —
    and what a developer most needs is known now, not after a model has
    summarised it."""
    incident, report = selffix.record_incident({
        "title": "FLUX.2-klein-4B", "message": "Traceback: OSError(28)",
        "page": "/models.html", "job_id": "sys:ai-model:x", "state": "error",
    })
    assert os.path.exists(incident) and os.path.exists(report)
    text = open(report, encoding="utf-8").read()
    assert "Not written yet" in text
    assert "Traceback: OSError(28)" in text
    assert "FLUX.2-klein-4B" in text
    assert __version__ in text


# ------------------------------------- ...when nothing actually went wrong


def test_a_described_problem_needs_no_error_and_says_so(install):
    """The Preferences way in (SF-14). A great deal of what is wrong with an app
    never raises anything, and a session told to trace a failure that does not
    exist will guess — which is the one thing a patch to somebody's install must
    not be."""
    incident, report = selffix.record_incident({
        "note": "Opening a big folder takes ten seconds and the window freezes.",
        "source": "preferences",
    })
    text = open(incident, encoding="utf-8").read()
    # The user's own words outrank the machinery: with no traceback the
    # description is the whole of what is known, so it leads the body rather
    # than sitting under the surface that sent it. (The five-line preamble —
    # when, version, platform — still comes first; that is context, not burial.)
    assert text.index("What the user asked for") < text.index("What the app was doing")
    assert "takes ten seconds" in text
    assert "No error was raised" in text
    assert "Not written yet" in open(report, encoding="utf-8").read()


def test_a_described_problem_gets_the_reproduce_first_brief(install):
    described = selffix.fix_prompt("/i.md", "/r.md", reported_error=False)
    assert "NOTHING CRASHED" in described
    assert "REPRODUCE WHAT THEY DESCRIBE" in described
    # ...and the failure brief keeps its own opening.
    failed = selffix.fix_prompt("/i.md", "/r.md", reported_error=True)
    assert "NOTHING CRASHED" not in failed
    assert "trace the failure" in failed
    # Both still fence the agent into the install.
    for prompt in (described, failed):
        assert "Only edit files under" in prompt


def test_the_incident_carries_the_app_log_and_names_the_call_log(install, tmp_path,
                                                                 monkeypatch):
    """With no traceback the log is frequently the only evidence there is, and a
    path the session has to go and find is a step it may not take."""
    log = tmp_path / "fused-render-1.log"
    log.write_text("ERROR listing /big took 9.8s\n" * 3)
    monkeypatch.setenv("FUSED_RENDER_LOG_DIR", str(tmp_path))
    monkeypatch.setattr("fused_render.logs.log_path", lambda: str(log))

    incident, _ = selffix.record_incident({"note": "slow"})
    text = open(incident, encoding="utf-8").read()
    assert "Recent app log" in text
    assert "listing /big took 9.8s" in text
    assert "Call log" in text


def test_the_log_tail_is_bounded(install, tmp_path, monkeypatch):
    """An incident file is meant to be READ. A multi-megabyte log pasted whole
    buries the description it is supposed to support."""
    log = tmp_path / "big.log"
    log.write_text("x" * 500_000 + "\nTHE LAST LINE\n")
    monkeypatch.setattr("fused_render.logs.log_path", lambda: str(log))

    incident, _ = selffix.record_incident({"note": "slow"})
    text = open(incident, encoding="utf-8").read()
    assert len(text) < selffix.LOG_TAIL_BYTES + 8_000
    assert "THE LAST LINE" in text  # the TAIL, not the head


def test_a_missing_log_is_not_worth_a_word(install, monkeypatch):
    monkeypatch.setattr("fused_render.logs.log_path", lambda: "/nope/absent.log")
    incident, _ = selffix.record_incident({"note": "slow"})
    assert "Recent app log" not in open(incident, encoding="utf-8").read()


def test_a_failed_row_with_no_message_still_gets_the_failure_brief(client,
                                                                   monkeypatch):
    """A job row may be `state: error` with an empty `message` (jobs.py leaves
    it "" and the manager renders a bare "Failed"). Keying the brief off the
    error TEXT handed that row the Preferences one — "nothing crashed, the user
    opened Preferences and described something" — which is false twice over and
    steers the session away from a failure that really happened."""
    seen = {}
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda t, p, m: seen.update(prompt=p) or {"run_id": "r"})
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready", lambda *a, **k: None)

    res = post(client, "/api/selffix/start",
               {"job_id": "sys:ai-model:x", "title": "FLUX.2-klein-4B",
                "state": "error", "kind": "download", "message": "",
                "source": "download manager"})
    assert res.status_code == 200
    assert "NOTHING CRASHED" not in seen["prompt"]
    assert "trace the failure" in seen["prompt"]


def test_start_refuses_a_session_with_nothing_to_look_at(client, monkeypatch):
    """Not validation for its own sake: a session handed no failure, no
    description and no name would read code at random and then report on having
    done so — which costs the user minutes to discover."""
    called = []
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda *a, **k: called.append(a) or {"run_id": "r"})
    res = post(client, "/api/selffix/start", {"source": "preferences", "note": "   "})
    assert res.status_code == 400
    assert "say what is wrong" in res.json()["error"]
    assert called == []


def test_a_described_problem_starts_a_session_and_is_labelled_by_its_first_line(
        client, install, monkeypatch):
    seen = {}

    def fake_spawn(target, prompt, mode):
        seen.update(prompt=prompt)
        return {"run_id": "run-9"}

    monkeypatch.setattr(selffix_routes, "_spawn_helper", fake_spawn)
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready", lambda *a, **k: None)

    res = post(client, "/api/selffix/start",
               {"note": "Dates are wrong in the parquet preview.\nOff by a day.",
                "source": "preferences"})
    assert res.status_code == 200
    # No error was reported, so the session gets the reproduce-first brief.
    assert "NOTHING CRASHED" in seen["prompt"]
    assert "Dates are wrong" in open(res.json()["incident"], encoding="utf-8").read()

    # The marker labels the fix by the description's first line — "a problem the
    # user described" over every row in the panel would say nothing.
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="run-9",
                   title="Dates are wrong in the parquet preview.")
    assert selffix.status()["fixes"][0]["title"] == (
        "Dates are wrong in the parquet preview.")


def test_list_reports_is_newest_first(install):
    _, first = selffix.record_incident({"title": "a"}, now=1000.0)
    _, second = selffix.record_incident({"title": "b"}, now=2000.0)
    os.utime(first, (1000.0, 1000.0))
    os.utime(second, (2000.0, 2000.0))
    assert [r["path"] for r in selffix.list_reports()] == [second, first]


@pytest.mark.parametrize("method", ["brew", "dmg", "windows", "linux", "source", "pip"])
def test_every_install_method_can_say_how_to_reinstall(install, method, monkeypatch):
    """The badge's other half. A panel that says "this app has been modified"
    and cannot tell you how to get an unmodified one is only half an answer, so
    every branch has to carry a headline, a note and a working link."""
    monkeypatch.setattr(selffix, "install_method", lambda: method)
    advice = selffix.reinstall_advice()
    assert advice["method"] == method
    assert advice["headline"] and advice["note"]
    assert advice["url"].startswith("https://")
    # The panel promotes the link to the section's ACTION when there is no
    # command to type, and words it from here — a raw URL as the only call to
    # action reads as a citation. So a label is never optional.
    assert advice["url_label"]


def test_a_dmg_install_has_nothing_to_type(install, monkeypatch):
    """The contract the panel's styling reads: empty `command` means the link
    IS the instruction. The DMG is dragged, not run — and it is the most common
    end-user install, so this is the branch that decides whether the reinstall
    section has a visible call to action at all."""
    for method in ("dmg", "windows", "linux"):
        monkeypatch.setattr(selffix, "install_method", lambda m=method: m)
        assert selffix.reinstall_advice()["command"] == ""
    for method in ("brew", "pip", "source"):
        monkeypatch.setattr(selffix, "install_method", lambda m=method: m)
        assert selffix.reinstall_advice()["command"]


def test_the_prompt_names_the_two_files_and_fences_the_agent_in(install):
    prompt = selffix.fix_prompt("/i/incident.md", "/r/report.md")
    assert "/i/incident.md" in prompt
    assert "/r/report.md" in prompt
    assert str(install) in prompt
    # The three rules a wandering agent breaks first.
    assert "static/shell-dist" in prompt
    assert "restarted" in prompt
    assert "Only edit files under" in prompt


# ------------------------------------------------------------------ the API


def test_start_requires_the_write_guard(client):
    assert client.post("/api/selffix/start", json={}).status_code == 403


def test_a_read_only_installation_gets_a_DIAGNOSTIC_session(client, install,
                                                            monkeypatch):
    """Not refused — told.

    This was a 409, on the argument that a session which cannot write spends
    minutes reading and then reports a fix that was never applied. That argument
    is about a session which does not KNOW it cannot write. Told up front, the
    same session is the most useful thing available on a machine nobody can
    patch, and an admin-installed copy is exactly where the user is least able
    to help themselves.

    Four things have to be true together, and each one alone would make the
    session worthless: it starts, the prompt says the tree is read-only, the
    report lands somewhere writable, and nothing tries to stamp an installation
    that cannot have changed.
    """
    monkeypatch.setattr(selffix, "writable", lambda: False)
    seen = {}

    def fake_spawn(target, prompt, mode):
        seen.update(target=target, prompt=prompt, mode=mode)
        return {"run_id": "run-ro"}

    monkeypatch.setattr(selffix_routes, "_spawn_helper", fake_spawn)
    watched = []
    monkeypatch.setattr(selffix_routes, "_watch_fix",
                        lambda *a, **k: watched.append(a))

    res = post(client, "/api/selffix/start", {"title": "boom"})
    assert res.status_code == 200
    body = res.json()
    assert body["diagnostic"] is True
    assert body["target"] == str(install)

    # The agent is told, in the prompt, before it wastes a turn discovering it.
    assert "READ-ONLY" in seen["prompt"]
    assert "DO NOT ATTEMPT A FIX" in seen["prompt"]
    assert "CHANGE NOTHING" in seen["prompt"]

    # ...and the report is somewhere it can actually write: NOT under the
    # installation, which is the whole reason it cannot fix anything.
    assert not body["report"].startswith(str(install))
    assert not body["incident"].startswith(str(install))
    assert os.path.exists(body["incident"])
    assert os.path.exists(body["report"])

    # Nothing to watch and nothing to stamp: no marker, no baseline, and no
    # watcher thread that would poll a digest which cannot move.
    assert watched == []
    assert selffix.status() is None
    assert not os.path.exists(selffix.baseline_path())


def test_a_write_that_fails_after_os_access_said_yes_becomes_diagnostic(
        client, install, monkeypatch):
    """`writable()` predicts; the write decides.

    `os.access` answers for the real uid's permission bits and knows nothing
    about an ACL that denies, a volume remounted read-only under a running app,
    or a full disk. So an installation can predict "writable" and refuse the
    very next write — and that used to surface as a 500 on precisely the
    installation SF-13 exists to help. The session now runs in the mode that can
    actually finish, and the incident lands where it can be written.
    """
    real_makedirs = os.makedirs
    state = selffix.state_dir()

    def no_writes_in_the_tree(path, *a, **k):
        if str(path).startswith(state):
            raise OSError(13, "Permission denied")
        return real_makedirs(path, *a, **k)

    monkeypatch.setattr(os, "makedirs", no_writes_in_the_tree)
    assert selffix.writable() is True          # the prediction still says yes

    seen = {}
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda target, prompt, mode: seen.update(prompt=prompt)
                        or {"run_id": "run-acl"})
    monkeypatch.setattr(selffix_routes, "_watch_fix", lambda *a, **k: None)

    res = post(client, "/api/selffix/start", {"title": "boom"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["diagnostic"] is True
    assert not selffix.in_state_dir(body["report"])
    assert os.path.exists(body["report"])
    # ...and the session is TOLD, which is the whole point of the mode.
    assert "READ-ONLY" in seen["prompt"]


def test_reports_written_out_of_tree_survive_the_install_becoming_writable(
        install, monkeypatch):
    """A report is a document about a machine's problem and outlives the
    installation (SF-13b) — so the panel has to keep listing one written while
    the install was read-only, after a chmod (or a move, or an ownership change)
    makes it look writable again. Reading only `records_dir()` dropped them from
    the list while the files sat on disk."""
    monkeypatch.setattr(selffix, "writable", lambda: False)
    _, diagnostic_report = selffix.record_incident({"title": "while locked"},
                                                   now=1000.0)
    assert not selffix.in_state_dir(diagnostic_report)

    monkeypatch.setattr(selffix, "writable", lambda: True)
    _, fix_report = selffix.record_incident({"title": "after chmod"}, now=2000.0)
    assert selffix.in_state_dir(fix_report)

    listed = [r["path"] for r in selffix.list_reports()]
    assert diagnostic_report in listed, "the out-of-tree report stopped being listed"
    assert fix_report in listed
    # Newest first ACROSS both homes — which dir a report landed in is an
    # accident of the day's permissions, not something to sort by.
    os.utime(diagnostic_report, (1000.0, 1000.0))
    os.utime(fix_report, (2000.0, 2000.0))
    assert [r["path"] for r in selffix.list_reports()][:2] == [fix_report,
                                                              diagnostic_report]


def test_in_tree_records_survive_the_install_becoming_READ_ONLY(install, monkeypatch):
    """The mirror of the writable-flip case, and the half a shared list hid.

    A writer must not be offered the state dir once the tree is known
    unwritable — that is a guaranteed exception per record. A READER has no such
    excuse: nothing about a home's writability bears on whether it can be read.
    Sharing one list meant an install that WAS writable and is now not — a
    `chmod`, a remount, an ownership change — dropped its in-tree reports and,
    worse, its session pointer out of view. That pointer is half the
    one-at-a-time guard, so a live fix session would stop excluding a second one
    the moment the tree it is editing turned read-only underneath it.
    """
    monkeypatch.setattr(selffix, "writable", lambda: True)
    _, in_tree = selffix.record_incident({"title": "while writable"}, now=1000.0)
    selffix.note_session("run-in-tree")
    assert selffix.in_state_dir(in_tree)

    monkeypatch.setattr(selffix, "writable", lambda: False)
    # The writer's list correctly stops offering a home that cannot take a write.
    assert selffix.record_homes() == [selffix.out_of_tree_dir()]
    # The reader's list never drops one, so neither record went out of view.
    assert selffix.state_dir() in selffix.reader_homes()
    assert in_tree in [r["path"] for r in selffix.list_reports()]
    assert selffix.active_run() == "run-in-tree", (
        "the guard lost a live session's pointer when the tree turned read-only")


def test_the_session_pointer_is_found_after_the_install_becomes_writable(
        install, monkeypatch):
    """Same rule for the guard's pointer: written in one home, still found from
    the other. Losing it would let a second agent start on a tree the first is
    still editing — the one thing SF-13a exists to prevent."""
    monkeypatch.setattr(selffix, "writable", lambda: False)
    selffix.note_session("run-locked")
    monkeypatch.setattr(selffix, "writable", lambda: True)
    assert selffix.active_run() == "run-locked"


def test_a_writable_installation_is_not_told_it_is_read_only(client, install,
                                                             monkeypatch):
    """The mirror, because a prompt that hedges on both is worse than either.
    An agent told "you may not be able to write" will check before it acts and
    report the check instead of the cause."""
    seen = {}
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda target, prompt, mode: seen.update(prompt=prompt)
                        or {"run_id": "run-rw"})
    monkeypatch.setattr(selffix_routes, "_watch_fix", lambda *a, **k: None)
    res = post(client, "/api/selffix/start", {"title": "boom"})
    assert res.status_code == 200
    assert res.json().get("diagnostic") is None
    assert "READ-ONLY" not in seen["prompt"]
    assert "DO NOT ATTEMPT A FIX" not in seen["prompt"]


def test_start_spawns_on_the_install_root_and_hands_back_the_run(client, install,
                                                                 monkeypatch):
    seen = {}

    def fake_spawn(target, prompt, mode):
        seen.update(target=target, prompt=prompt, mode=mode)
        return {"run_id": "run-7"}

    monkeypatch.setattr(selffix_routes, "_spawn_helper", fake_spawn)
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready",
                        lambda *a, **k: None)

    res = post(client, "/api/selffix/start",
               {"title": "download failed", "message": "OSError(28)"})
    assert res.status_code == 200
    body = res.json()
    assert body["run_id"] == "run-7"
    assert body["target"] == str(install)
    assert seen["target"] == str(install)
    # Not "auto" (the app scaffolder's mode): this session edits the
    # application itself, in front of a user who is watching it.
    assert seen["mode"] == selffix.FIX_PERMISSION_MODE == "prompt"
    assert os.path.exists(body["incident"])
    assert "OSError(28)" in open(body["incident"], encoding="utf-8").read()


def test_a_failed_spawn_says_why(client, monkeypatch):
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda *a, **k: {"error": "claude CLI not found"})
    res = post(client, "/api/selffix/start", {"title": "boom"})
    assert res.status_code == 502
    assert "claude CLI not found" in res.json()["error"]


def test_a_failed_spawn_does_not_wedge_the_one_session_slot(client, monkeypatch):
    """Every early return has to release it, or one missing CLI locks the
    feature out for the TTL's whole hour."""
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda *a, **k: {"error": "claude CLI not found"})
    assert post(client, "/api/selffix/start", {"title": "a"}).status_code == 502

    monkeypatch.setattr(selffix_routes, "_spawn_helper", lambda *a, **k: {"run_id": "r"})
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready",
                        lambda *a, **k: None)
    assert post(client, "/api/selffix/start", {"title": "b"}).status_code == 200


class _FakeAgent:
    """Stands in for the claude template's agent module.

    Two things the guard asks it, mirroring the real one: `_live_run` is the
    BOUNDED scan of the runs directory, and `_alive` answers for a run named
    directly. `alive` is the set of run ids whose process is still going;
    `scanned` is what the (limited) scan would find, which a test can leave
    empty to model a run that has scrolled out of the window.
    """

    RUNS = "/runs"

    def __init__(self, live: str = "", alive: set[str] | None = None):
        self.live = live
        self.alive = set(alive or ([live] if live else []))

    def _live_run(self, target: str, session_id: str = "") -> dict:
        return {"run_id": self.live}

    def _alive(self, run_dir: str) -> bool:
        return os.path.basename(run_dir) in self.alive


def test_only_one_fix_session_runs_at_a_time(client, monkeypatch):
    """Two agents rewriting one installation is not concurrency, it is a
    conflict — and a user with two failed rows clicking Fix on both is the
    ordinary way to get there."""
    agent = _FakeAgent()
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: agent)
    # Starting a session is what makes the runs directory report one.
    def spawn(*a, **k):
        agent.live = "r1"
        return {"run_id": "r1"}

    monkeypatch.setattr(selffix_routes, "_spawn_helper", spawn)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready",
                        lambda *a, **k: threading.Event().wait(5))

    assert post(client, "/api/selffix/start", {"title": "a"}).status_code == 200
    second = post(client, "/api/selffix/start", {"title": "b"})
    assert second.status_code == 409
    assert "already working" in second.json()["error"]
    assert "r1" in second.json()["error"]


def test_the_guard_survives_a_restart_because_it_is_asked_not_remembered(
        client, monkeypatch):
    """The claude process is detached and outlives this server, so a guard held
    in memory is cleared by exactly the restart a fix session CAUSES: it edits
    .py files under a dev server watching them. Nothing here is carried across —
    a fresh module state still refuses, because the answer is on disk."""
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: _FakeAgent("r-old"))
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda *a, **k: pytest.fail("must not spawn beside a live run"))

    refused = post(client, "/api/selffix/start", {"title": "after a restart"})
    assert refused.status_code == 409
    assert "r-old" in refused.json()["error"]


def test_a_lookup_that_cannot_answer_fails_open(client, monkeypatch):
    """Failing open is not a coin toss: everything this can fail on fails the
    spawn a moment later too, with a message that says what actually went wrong
    rather than "already running"."""
    class Boom:
        def _live_run(self, *a, **k):
            raise RuntimeError("runs dir unreadable")

    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: Boom())
    monkeypatch.setattr(selffix_routes, "_spawn_helper", lambda *a, **k: {"run_id": "r9"})
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready",
                        lambda *a, **k: None)

    assert post(client, "/api/selffix/start", {"title": "a"}).status_code == 200


def test_the_watcher_stamps_when_the_session_changed_something(install, monkeypatch):
    """The stamp is the app's decision, not the model's — a session asked to
    mark its own work is a session that can forget to."""
    _pristine()

    def fake_record(agent, run_id, on_tick=None):
        (install / "jobs.py").write_text("patched by the session\n")
        on_tick({"done": True, "session_id": "sess-1"})

    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready", fake_record)
    REAL_WATCH_FIX("run-7", "/i.md", "/r.md", "download failed",
                              BEFORE[0])

    state = selffix.status()
    assert state is not None
    assert state["fixes"][0]["session_id"] == "sess-1"
    assert state["fixes"][0]["title"] == "download failed"


def test_the_watcher_leaves_an_untouched_installation_alone(install, monkeypatch):
    _pristine()
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready",
                        lambda agent, run_id, on_tick=None: on_tick({"done": True}))
    REAL_WATCH_FIX("run-7", "/i.md", "/r.md", "", BEFORE[0])
    assert selffix.status() is None


def test_two_fixes_in_the_same_second_do_not_overwrite_each_other(install):
    """The collision would not be a duplicate file — it would be the second
    session clobbering the first session's report while it was being written."""
    incident_a, report_a = selffix.record_incident({"title": "a"}, now=1000.0)
    incident_b, report_b = selffix.record_incident({"title": "b"}, now=1000.0)
    assert report_a != report_b and incident_a != incident_b
    assert "# Incident — a" in open(report_a, encoding="utf-8").read()
    assert "# Incident — b" in open(report_b, encoding="utf-8").read()


def test_snapshot_carries_the_panel_and_config_carries_only_the_flag(client, install):
    """The split that keeps /api/config cheap: the chip's PRESENCE rides the
    config poll, its CONTENTS (a directory walk and, on a mac, a brew probe)
    are fetched once when the panel opens."""
    _pristine()
    assert "modified_install" not in client.get("/api/config").json()

    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")

    config = client.get("/api/config").json()
    assert config["modified_install"]["modified"] is True

    snapshot = client.get("/api/selffix").json()
    assert snapshot["modified"] is True
    assert snapshot["install_root"] == str(install)
    assert snapshot["reinstall"]["headline"]
    assert snapshot["reinstall"]["url"]
    assert "issues_url" in snapshot


def test_config_says_read_only_so_a_row_can_word_its_button(client, install,
                                                            monkeypatch):
    """The one bit the download manager needs BEFORE anyone clicks (SF-13d).

    Preferences learns this from GET /api/selffix, which costs a directory walk
    and a brew probe — far too much to word a label with. A failed download row
    has to say either "Fix this" or "Diagnose this" the moment it appears, so
    the flag rides the config poll the shell already makes. It is one
    `os.access` call.

    PRESENT ONLY WHEN READ-ONLY, like `modified_install`: a `read_only: False`
    on every writable install is a field that invites a truthiness check which
    passes for the wrong reason.
    """
    assert "read_only" not in client.get("/api/config").json()

    monkeypatch.setattr(selffix, "writable", lambda: False)
    assert client.get("/api/config").json()["read_only"] is True


def test_a_machine_with_no_claude_is_refused_BEFORE_the_body_is_validated(
        client, install, monkeypatch):
    """The machine's answer outranks the request's (SF-13f).

    Preferences offers "Set up Claude Code" with the description box empty —
    there is nothing to describe yet, the CLI is the problem — so a route that
    validated the body first answered that click with "say what is wrong", and
    the user never saw the install card the button exists to show. Asking
    someone to describe a problem for a session that cannot start on this
    computer is a form to fill in for nothing.

    Same message and status as the post-hoc path, because it is the same fact
    found a moment earlier.
    """
    monkeypatch.setattr(selffix_routes, "_claude_found", lambda: False)

    res = post(client, "/api/selffix/start", {})       # the empty describe click
    assert res.status_code == 502
    assert res.json()["error"] == claude_spawn.CLAUDE_MISSING_ERROR

    # ...and a described one gets the same answer rather than starting.
    res = post(client, "/api/selffix/start", {"note": "the dates render wrong"})
    assert res.status_code == 502
    assert res.json()["error"] == claude_spawn.CLAUDE_MISSING_ERROR


def test_an_empty_start_still_asks_what_is_wrong_when_claude_IS_installed(
        client, install):
    """The body check is not gone, only outranked: on a machine that can run a
    session, a request naming no failure and no description is still the one
    thing a session cannot work from."""
    res = post(client, "/api/selffix/start", {})
    assert res.status_code == 400
    assert "say what is wrong" in res.json()["error"]


def test_the_gate_asks_claude_health_and_nothing_else(client, install, monkeypatch):
    """ONE resolver, which is the whole point of `claude_health` (#621).

    That module exists because four independent copies of the candidate list let
    a CLI in `~/.bun/bin` give a working Claude-config tab and an
    `ai_unavailable` on the same machine in the same second. A self-fix gate with
    its own resolution would be that bug again in a new place: the button
    refusing while the health strip beside it says the install is fine.
    """
    from fused_render import claude_health

    # BOTH reads are pinned, because a miss re-measures (see below) and an
    # unpinned re-measure would resolve THIS container's real CLI and answer
    # the opposite of what the case is about.
    def pin(found):
        for name in ("summary", "summary_refreshed"):
            monkeypatch.setattr(claude_health, name, lambda: dict(found))

    # The REAL resolver (the module fixture pins a stand-in for every other
    # test), reading nothing but claude_health.
    pin({"found": False})
    assert REAL_CLAUDE_FOUND() is False

    pin({"found": True})
    assert REAL_CLAUDE_FOUND() is True

    # ...and a snapshot that could not tell reads as "no CLI", which is the
    # refusal direction: claude_health never raises, it degrades, so an absent
    # `found` must not be mistaken for a usable install.
    pin({})
    assert REAL_CLAUDE_FOUND() is False


def test_a_cached_MISS_is_re_measured_before_it_may_refuse(client, install,
                                                           monkeypatch):
    """THE BUTTON'S OWN INSTRUCTION, OBEYED. It says "Set up Claude Code"; the
    user goes and installs it and clicks again — and the snapshot behind the
    gate is up to 60s old and says no.

    The cache cannot notice that install by itself. It invalidates on the
    resolved binary's mtime, and a snapshot that resolved nothing has no path to
    stat: an install into a directory already on PATH moves no path, changes no
    PATH string, and touches no file the snapshot knows about, so the
    fingerprint still matches. Age is the only thing that clears it, which makes
    the minute after the install exactly the window this button is clicked in.

    So a miss re-measures, and only a miss: the cost lands on the click that
    would otherwise be refused.
    """
    from fused_render import claude_health

    reads = []

    def stale_summary():
        reads.append("cached")
        return {"found": False}            # the snapshot from before the install

    def fresh_summary():
        reads.append("measured")
        return {"found": True}             # what a probe would see now

    monkeypatch.setattr(claude_health, "summary", stale_summary)
    monkeypatch.setattr(claude_health, "summary_refreshed", fresh_summary)

    assert REAL_CLAUDE_FOUND() is True
    assert reads == ["cached", "measured"]


def test_a_cached_HIT_costs_no_probe(client, install, monkeypatch):
    """The other direction, which is why the re-measure can be afforded at all.

    A cached yes is taken as read. Being wrong about it costs one spawn that
    fails and says so in its own words — the answer this route gave before the
    gate existed — whereas re-probing every start would pay seconds on every
    machine to catch the rare disappearance of a binary that reports itself.
    """
    from fused_render import claude_health

    def refuse_to_probe():
        raise AssertionError("a cached yes must not spend a probe")

    monkeypatch.setattr(claude_health, "summary", lambda: {"found": True})
    monkeypatch.setattr(claude_health, "summary_refreshed", refuse_to_probe)

    assert REAL_CLAUDE_FOUND() is True


def test_clear_endpoint(client, install):
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert client.post("/api/selffix/clear").status_code == 403
    assert post(client, "/api/selffix/clear").json() == {"cleared": True}
    assert "modified_install" not in client.get("/api/config").json()


def test_a_session_whose_watcher_died_still_excludes_a_second_one(client, monkeypatch):
    """The thread that follows a session is bookkeeping, not the guard.

    It used to be both, and that forced a choice between two harms when the
    thread failed to start: free the guard with an agent live, or hold it on a
    timer. Neither is needed once the guard is a question about the runs
    directory — the session excludes the next one because its process is alive,
    watched or not. The unstamped badge is the one cost left, and no lock could
    have saved it.
    """
    agent = _FakeAgent()
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: agent)

    def spawn(*a, **k):
        agent.live = "r1"
        return {"run_id": "r1"}

    monkeypatch.setattr(selffix_routes, "_spawn_helper", spawn)

    class NoThreads:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    real_thread = selffix_routes.threading.Thread
    monkeypatch.setattr(selffix_routes.threading, "Thread", NoThreads)

    # The start still succeeds — the session really is running, and the user is
    # about to land in it.
    assert post(client, "/api/selffix/start", {"title": "a"}).status_code == 200

    # ...and the next one is refused anyway. Only Thread is put back: the spawn
    # stub stays, so a refusal that did NOT happen would be a test failure
    # rather than a real `claude` process.
    monkeypatch.setattr(selffix_routes.threading, "Thread", real_thread)
    second = post(client, "/api/selffix/start", {"title": "b"})
    assert second.status_code == 409
    assert "r1" in second.json()["error"]


def test_a_long_fix_still_excludes_a_second_one_after_the_scan_forgets_it(
        client, monkeypatch, install):
    """The scan is BOUNDED — it reads the newest runs on the machine before
    filtering by target — and a fix session is the long-running case by
    construction. A machine running scheduled tasks can start enough runs beside
    it that the scan stops seeing the very session it is guarding, so the run we
    started is also named directly and asked about by pid."""
    selffix.note_session("r-long")
    # The scan finds nothing: this run scrolled out of its window.
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive={"r-long"}))
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda *a, **k: pytest.fail("must not spawn beside a live fix"))

    refused = post(client, "/api/selffix/start", {"title": "second"})
    assert refused.status_code == 409
    assert "r-long" in refused.json()["error"]


def test_a_recorded_run_that_has_finished_does_not_block_anything(
        client, monkeypatch, install):
    """The pointer is not a lease: nothing expires it, because liveness is the
    process. A record left by a finished session — or by a machine that lost
    power mid-fix — reads as "not running" the moment its pid is gone."""
    selffix.note_session("r-dead")
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive=set()))
    monkeypatch.setattr(selffix_routes, "_spawn_helper", lambda *a, **k: {"run_id": "r2"})
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready", lambda *a, **k: None)

    assert post(client, "/api/selffix/start", {"title": "a"}).status_code == 200
    # ...and the new run took its place in the record.
    assert selffix.active_run() == "r2"


def test_the_snapshot_reports_a_registry_that_will_not_parse(client, monkeypatch, tmp_path):
    """A broken template registry is a fault with no symptom except the app
    being subtly wrong: the BUILT-IN registry still matches, so files still
    preview and only the user's own bindings stop applying. The server has
    reported it on every stat since PT-8 and nothing read it, so the Preferences
    tab — where "something is wrong with the app" lives — now carries it, with
    the whole error and a way to copy it (a toast clamps at four lines)."""
    from fused_render.server import templates as server_templates

    bad = tmp_path / "registry.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(bad))

    snapshot = client.get("/api/selffix").json()
    assert "cannot read registry.json" in snapshot["template_error"]


def test_a_healthy_registry_says_nothing_at_all(client, monkeypatch, tmp_path):
    """Absent rather than empty, like `modified_install`: a `template_error: ""`
    is a value every caller then has to truthiness-check, and one of them
    eventually will not."""
    from fused_render.server import templates as server_templates

    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(tmp_path / "absent.json"))
    assert "template_error" not in client.get("/api/selffix").json()


def test_a_missing_user_registry_is_not_an_error(tmp_path, monkeypatch):
    """Most installs have never had one. Reporting its absence would put a
    warning in front of every user who never wrote a custom template."""
    from fused_render.server import templates as server_templates

    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(tmp_path / "nope.json"))
    assert server_templates.registry_error() == ""


def test_the_user_registry_is_reported_before_the_builtin_one(tmp_path, monkeypatch):
    """It is the one a person edits, so it is the one they can fix."""
    from fused_render.server import templates as server_templates

    user = tmp_path / "user.json"
    user.write_text("nope", encoding="utf-8")
    builtin = tmp_path / "builtin.json"
    builtin.write_text("also nope", encoding="utf-8")
    monkeypatch.setattr(server_templates, "USER_REGISTRY", str(user))
    monkeypatch.setattr(server_templates, "BUILTIN_REGISTRY", str(builtin))

    error = server_templates.registry_error()
    assert "cannot read registry.json:" in error
    assert "built-in" not in error


def test_the_stale_marker_check_and_the_unlink_are_one_step(install, monkeypatch):
    """Read, decide, delete — under the lock, because otherwise they are about
    two different files.

    `status` runs on every `/api/config` poll, so it is the most frequent
    caller in the module, and the one that DELETES. Between its version check
    and its unlink, a fix session's watcher can call `mark_modified` and write
    a fresh marker for the version now installed; the unlink then takes that
    one away, and the badge for a fix that really happened never appears. The
    precondition is exactly the ordinary upgrade-then-fix sequence, not a
    contrived one.

    Asserted by probing the lock from inside `_discard` rather than by racing
    threads: a test that has to lose a race to fail is a test that passes by
    luck.
    """
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    marker = json.loads(open(selffix.marker_path()).read())
    marker["version"] = "0.0.1"
    with open(selffix.marker_path(), "w") as f:
        json.dump(marker, f)

    seen = {}
    real_discard = selffix._discard

    def probe(path):
        free = selffix._lock.acquire(blocking=False)
        seen["held"] = not free
        if free:
            selffix._lock.release()
        return real_discard(path)

    monkeypatch.setattr(selffix, "_discard", probe)
    assert selffix.status() is None
    assert seen["held"], "the version check and the unlink must not be splittable"


def test_the_session_pointer_survives_a_read_only_installation(install, monkeypatch):
    """The long-session half of the one-at-a-time guard, on the install where it
    is hardest to notice missing.

    `note_session` is best-effort by design — a pointer that could not be
    written costs that half of the guard, and refusing to start a fix over it
    would be the wrong trade. Which is exactly why it must not be pointed at a
    directory that CANNOT be written: it fails silently, on every start, on the
    one kind of installation where the user has no way to investigate. A
    diagnostic session holds the tree open for reading exactly as long as a
    fixing one does, and the bounded runs-directory scan scrolls past it just
    the same (SF-13c1).
    """
    monkeypatch.setattr(selffix, "writable", lambda: False)
    selffix.note_session("run-ro")
    assert selffix.active_run() == "run-ro"
    # ...and not inside the tree it could not write to.
    assert not selffix.session_path().startswith(str(install))


def test_a_session_on_ONE_installation_does_not_lock_ANOTHER(install, tmp_path,
                                                             monkeypatch):
    """The guard is per-INSTALL, and the out-of-tree home is per-USER.

    Those two facts collide. The state dir belongs to one installation, so a
    pointer in it can only ever be about that one; `~/.fused-render/selffix` is
    shared by every copy the user has, so a pointer named the same way there
    speaks for all of them. One live diagnostic session on an admin-installed
    copy then answered "busy" for the user's own second copy — and installing a
    second copy is the exact remedy the read-only UI names, so the refusal
    landed on precisely the user who had followed the advice.
    """
    other = tmp_path / "elsewhere" / "fused_render"
    other.mkdir(parents=True)

    # A diagnostic session on the read-only copy: its pointer has nowhere to go
    # but the shared home.
    monkeypatch.setattr(selffix, "writable", lambda: False)
    selffix.note_session("run-on-the-admin-copy")
    assert selffix.active_run() == "run-on-the-admin-copy"

    # The user's own copy, same machine, same user, same home dir.
    monkeypatch.setattr(selffix, "install_root", lambda: str(other))
    assert selffix.active_run() == "", (
        "a session on one installation was reported as live on another")


def test_two_installations_do_not_overwrite_each_other_s_pointer(install, tmp_path,
                                                                 monkeypatch):
    """The mirror of the same collision, and the quieter half.

    Sharing one name in the shared home does not only make a pointer visible to
    the wrong install — it makes the second start DESTROY the first. The copy
    whose pointer was overwritten keeps running with only the bounded scan
    behind it, which is the very half `note_session` exists to cover.
    """
    other = tmp_path / "elsewhere" / "fused_render"
    other.mkdir(parents=True)
    monkeypatch.setattr(selffix, "writable", lambda: False)

    selffix.note_session("run-on-the-admin-copy")
    monkeypatch.setattr(selffix, "install_root", lambda: str(other))
    selffix.note_session("run-on-my-own-copy")
    assert selffix.active_run() == "run-on-my-own-copy"

    monkeypatch.setattr(selffix, "install_root", lambda: str(install))
    assert selffix.active_run() == "run-on-the-admin-copy", (
        "the second installation's session pointer overwrote the first's")


def test_two_installations_do_not_mix_their_reports(install, tmp_path, monkeypatch):
    """The pointer beside them was namespaced and the records were not.

    The out-of-tree home is per-USER, so a diagnostic session on an admin copy
    and one on the user's own copy write into the same `selffix` dir — and a
    bare `reports/` there leaves the reader no way to tell whose is whose. Both
    panels then list both installations' problems, and the file `issueUrl`
    offers to open can belong to a copy the user was not even looking at. The
    read-only case is the common one here, which makes it the installs least
    able to write anywhere else that collide.
    """
    other = tmp_path / "elsewhere" / "fused_render"
    other.mkdir(parents=True)
    monkeypatch.setattr(selffix, "writable", lambda: False)  # both write out of tree

    _, mine = selffix.record_incident({"title": "on my copy"}, now=1000.0)
    assert not selffix.in_state_dir(mine)

    monkeypatch.setattr(selffix, "install_root", lambda: str(other))
    _, theirs = selffix.record_incident({"title": "on the admin copy"}, now=2000.0)
    assert not selffix.in_state_dir(theirs)
    assert [r["path"] for r in selffix.list_reports()] == [theirs], (
        "an installation's panel listed another installation's report")

    monkeypatch.setattr(selffix, "install_root", lambda: str(install))
    assert [r["path"] for r in selffix.list_reports()] == [mine], (
        "an installation's panel listed another installation's report")


def test_an_in_tree_pointer_survives_the_installation_being_MOVED(install, tmp_path,
                                                                  monkeypatch):
    """Why the two homes name the pointer differently rather than both carrying
    the install.

    Folding the root into the name everywhere would look more uniform and would
    be wrong in the one home that does not need it: the state dir travels WITH
    the tree, so a name derived from the old absolute path stops matching the
    moment the install is moved or renamed — and a fix session survives that
    exactly as a chat does. Inside the tree, the directory is the identity.
    """
    _, in_tree = selffix.record_incident({"title": "here"}, now=1000.0)
    selffix.note_session("run-before-the-move")
    assert selffix.in_state_dir(in_tree)

    moved = tmp_path / "moved" / "fused_render"
    moved.parent.mkdir(parents=True, exist_ok=True)
    os.rename(install, moved)
    monkeypatch.setattr(selffix, "install_root", lambda: str(moved))

    assert selffix.active_run() == "run-before-the-move", (
        "the pointer stopped being found once its installation moved")


def test_in_tree_records_survive_the_installation_being_MOVED(install, tmp_path,
                                                              monkeypatch):
    """The same argument as the pointer above, for the records beside it — and
    the reason `_records_subdir` is an ASYMMETRY rather than "always namespace".

    Folding the install into the directory name everywhere would look tidier and
    would break the one home that does not need it. The state dir travels WITH
    the tree, so a name derived from the old absolute path stops matching the
    moment the install is moved or renamed, and the panel would show nothing
    while the files sat there — the same silent disappearance reading only one
    home used to cause. Inside the tree the DIRECTORY is the identity; only the
    shared out-of-tree home needs the name to carry it.
    """
    _, in_tree = selffix.record_incident({"title": "here"}, now=1000.0)
    assert selffix.in_state_dir(in_tree)
    assert [r["path"] for r in selffix.list_reports()] == [in_tree]

    moved = tmp_path / "moved" / "fused_render"
    moved.parent.mkdir(parents=True, exist_ok=True)
    os.rename(install, moved)
    monkeypatch.setattr(selffix, "install_root", lambda: str(moved))

    listed = [r["path"] for r in selffix.list_reports()]
    assert len(listed) == 1 and listed[0].startswith(str(moved)), (
        "the in-tree report stopped being listed once its installation moved")


def test_a_dismiss_that_could_not_remove_the_marker_says_so(client, install,
                                                            monkeypatch):
    """The one failure in `clear` that must not be swallowed.

    Both dismiss buttons hide the badge on the server's 200 rather than waiting
    for the next poll — VersionChip and SelfFixPanel each say so in a comment,
    the second in as many words ("the marker is known-gone: say so now"). That
    inference is only sound if a 200 means the file is actually off disk. Let
    the unlink fail quietly and the user gets the badge removed, then handed
    back a minute later by the next `/api/config`, which reads as the dismiss
    having failed while the app told them it worked.
    """
    _pristine()
    selffix.mark_modified(run_id="r1", digest="d", now=1000.0)
    assert selffix.status() is not None

    def denied(path):
        raise PermissionError(13, "Permission denied", path)

    # A CONTEXT, not `monkeypatch.undo()`: undo drops every patch this test's
    # monkeypatch holds, `install`'s `install_root` among them, and the assert
    # below would then read the REAL package — where it happily found a marker
    # left by something else and passed for the wrong reason.
    with monkeypatch.context() as m:
        m.setattr(selffix.os, "unlink", denied)
        refused = post(client, "/api/selffix/clear")
    assert refused.status_code == 500, refused.text
    assert "could not clear the marker" in refused.json()["error"]

    # ...and the badge is still there, because the marker is.
    assert selffix.status() is not None, (
        "the marker was reported cleared but is still on disk")


def test_a_marker_another_process_already_removed_is_a_clean_dismiss(
        install, monkeypatch):
    """`FileNotFoundError` is the one OSError that means it WORKED.

    The existence check and the unlink are not atomic across processes — a
    reconcile in a second server can drop the marker in between — and gone is
    gone, so that race is a successful dismiss rather than something to report.
    """
    _pristine()
    selffix.mark_modified(run_id="r1", digest="d", now=1000.0)

    real_unlink = selffix.os.unlink

    def vanished(path):
        real_unlink(path)
        raise FileNotFoundError(2, "No such file or directory", path)

    with monkeypatch.context() as m:
        m.setattr(selffix.os, "unlink", vanished)
        assert selffix.clear() is True
    assert selffix.status() is None


# QUOTING IS UNIT-TESTED ON `_shell_path`, WIRING IS TESTED ON THE HOST.
#
# The split is forced, and the first version of these tests learned it the hard
# way on the windows leg. `_shell_path` is pure string work, so every platform
# and path shape can be exercised anywhere. `reinstall_advice` around it calls
# `os.path.dirname`, which is ALSO platform-specific — on Linux it splits only
# on "/", so a `C:\...` literal comes back as "" — and `platform.system()`
# cannot be faked away from the flavour of `os.path` in the same process. Faking
# the platform while taking the path from the runner asserts about two platforms
# at once; faking it while writing a literal for the other one asserts about a
# path `os.path` cannot parse. So the wiring test uses the host's own shape and
# does not fake the platform at all.
_POSIX_PLAIN = "/home/me/src/fused_render"
_POSIX_SPACES = "/Users/me/My Checkouts/fused render"
_WIN_PLAIN = r"C:\Users\me\src"
_WIN_SPACES = r"C:\Program Files\fused render"


@pytest.mark.parametrize("system,path,expected", [
    # A space is what breaks the paste, and it is ordinary on both.
    ("Darwin", _POSIX_SPACES, "'/Users/me/My Checkouts/fused render'"),
    ("Linux", _POSIX_SPACES, "'/Users/me/My Checkouts/fused render'"),
    ("Windows", _WIN_SPACES, r'"C:\Program Files\fused render"'),
    # ...and quoting only where it is needed: the command is read at least as
    # often as it is run, and quotes a path never needed are noise.
    ("Darwin", _POSIX_PLAIN, _POSIX_PLAIN),
    ("Linux", _POSIX_PLAIN, _POSIX_PLAIN),
    ("Windows", _WIN_PLAIN, _WIN_PLAIN),
])
def test_shell_path_quotes_for_the_shell_it_names(system, path, expected,
                                                 monkeypatch):
    """Every branch of the platform switch, both with and without spaces.

    Windows gets DOUBLE quotes rather than `shlex.quote`'s single ones: `"` is
    not a legal character in a Windows path so wrapping is always safe there,
    while a single quote is literal to cmd.exe and would be pasted straight
    through into the path.
    """
    monkeypatch.setattr(selffix.platform, "system", lambda: system)
    assert selffix._shell_path(path) == expected


def test_the_platform_switch_is_about_the_SHELL_not_the_path_shape(monkeypatch):
    """Why the wiring test below cannot fake the platform.

    `shlex.quote` quotes a Windows-shaped path even with no spaces in it,
    because a backslash is shell-special on POSIX. "Does this path need
    quoting?" therefore has two different answers depending on which shell is
    asking — which is what makes a faked platform plus a runner-shaped path a
    test about neither.
    """
    monkeypatch.setattr(selffix.platform, "system", lambda: "Darwin")
    assert selffix._shell_path(_WIN_PLAIN) != _WIN_PLAIN
    assert selffix._shell_path(_POSIX_PLAIN) == _POSIX_PLAIN


def test_the_source_reinstall_command_survives_a_path_with_spaces(tmp_path,
                                                                 monkeypatch):
    """The wiring, on the host's own path flavour and real platform.

    The command renders in a `<code>` block with a copy button beside it, so the
    panel is inviting the user to run it — and `git -C /Users/me/My Checkouts/…`
    unquoted is three arguments git cannot make sense of. What this pins is that
    the parent directory arrives as ONE argument whatever the host is.
    """
    root = tmp_path / "My Checkouts" / "fused render" / "fused_render"
    root.mkdir(parents=True)
    monkeypatch.setattr(selffix, "install_root", lambda: str(root))
    monkeypatch.setattr(selffix, "install_method", lambda: "source")

    command = selffix.reinstall_advice()["command"]
    assert command.startswith("git -C ") and command.endswith(" status"), command
    quoted = command[len("git -C "):-len(" status")]
    if platform.system() == "Windows":
        assert quoted == f'"{root.parent}"', command
    else:
        assert shlex.split(quoted) == [str(root.parent)], command


# ------------------------------------------- the watcher outliving its server


def _join_watchers(timeout: float = 5.0) -> None:
    """Wait out any watcher `resume` launched. It is a daemon thread, so an
    assertion made straight after the call would race it."""
    for thread in threading.enumerate():
        if thread.name == "fused-render-selffix-watch":
            thread.join(timeout=timeout)



def test_a_fix_whose_watcher_died_with_the_server_is_still_stamped(
        install, monkeypatch):
    """SF-7d. The watcher is a thread on the server that spawned the session;
    the claude process is detached and outlives it. The restart most likely to
    happen is the one the session CAUSES — it edits .py files under a dev server
    watching them — and before this, that restart ended the measuring while the
    editing carried on: no `settle` against this session's `before`, and
    `reconcile` cannot stand in because it returns early with no marker to find.
    The install ended up patched with no badge, the one outcome this feature
    exists to prevent."""
    before = _pristine()
    selffix.note_session("r-orphan", before=before, report=str(install / ".x" / "r.md"),
                         incident="", title="download failed")
    # The session edits the tree; the server that was watching is gone.
    (install / "jobs.py").write_text("RUNNING = 'running'  # patched by the session\n")
    # A new process starts. Its run is over, so nothing is re-attached.
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive=set()))

    selffix_routes.resume()

    state = selffix.status()
    assert state is not None, "the fix was never stamped — the badge would not appear"
    assert state["modified"] is True
    assert [f["run_id"] for f in state["fixes"]] == ["r-orphan"]
    assert state["fixes"][0]["title"] == "download failed", (
        "the marker's strings ride with the pointer, or the panel lists the fix "
        "under a blank label")


def test_resume_re_attaches_the_watcher_while_the_session_is_still_running(
        install, monkeypatch):
    """Stamping once is only half of it. A session still editing after the
    restart needs something measuring it from here on, and liveness is asked of
    the pid — the same question the guard asks, never remembered."""
    before = _pristine()
    selffix.note_session("r-live", before=before)
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive={"r-live"}))
    # The autouse `_no_detached_watcher` already stubs the watcher; this records
    # what it was handed, which is the contract under test — that a watcher is
    # launched for the live run, carrying the digest it must measure against.
    watched = []
    monkeypatch.setattr(
        selffix_routes, "_watch_fix",
        lambda run_id, incident, report, title, seen: watched.append((run_id, seen)))

    selffix_routes.resume()
    _join_watchers()

    assert watched == [("r-live", before)], (
        "a live session was left with nothing watching it")


def test_resume_does_not_re_attach_to_a_session_that_has_finished(
        install, monkeypatch):
    """The dead run has already had its final answer taken by the settle above.
    Re-attaching would poll a run that will never tick until the watcher's own
    hour is up."""
    before = _pristine()
    selffix.note_session("r-done", before=before)
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive=set()))
    monkeypatch.setattr(selffix_routes, "_watch_fix",
                        lambda *a, **k: pytest.fail("must not follow a dead run"))

    selffix_routes.resume()
    _join_watchers()


def test_resume_stamps_nothing_when_the_tree_never_moved(install, monkeypatch):
    """`resume` finishes a stamp; it does not invent one. A session that changed
    nothing must not raise a badge pointing at a do-nothing report — the same
    rule `settle` already holds, reached by a different door."""
    before = _pristine()
    selffix.note_session("r-quiet", before=before)
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive=set()))

    selffix_routes.resume()

    assert selffix.status() is None


def test_a_pointer_written_before_this_existed_resumes_nothing(install, monkeypatch):
    """A schema-1 record survives an upgrade and carries no `before`. There is
    nothing to measure against, and measuring against the BASELINE instead would
    turn the badge into an integrity claim about the bytes — which this feature
    refuses to make. So it does exactly what the old version did: nothing."""
    _pristine()
    for home in selffix.record_homes():
        path = selffix._session_file(home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump({"schema": 1, "run_id": "r-old"}, handle)
        break
    (install / "jobs.py").write_text("RUNNING = 'running'  # patched\n")
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive=set()))

    selffix_routes.resume()

    assert selffix.status() is None
    assert selffix.active_run() == "r-old", "the pointer still names its run"


def test_a_diagnostic_session_records_no_before_so_nothing_resumes_it(
        client, monkeypatch, install):
    """A read-only install has no baseline, no watcher and nothing to stamp — a
    digest that cannot move. The pointer is still written (the guard needs it
    just as much) but it carries no `before`, so `resume` skips it for the same
    reason the watcher was never started."""
    monkeypatch.setattr(selffix, "writable", lambda: False)
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: _FakeAgent())
    monkeypatch.setattr(selffix_routes, "_spawn_helper", lambda *a, **k: {"run_id": "r-diag"})
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready", lambda *a, **k: None)

    started = post(client, "/api/selffix/start", {"note": "dates render wrong"})
    assert started.status_code == 200
    assert started.json()["diagnostic"] is True

    assert selffix.active_run() == "r-diag"
    assert selffix.session_record().get("before") == "", (
        "a diagnostic session must not leave a digest for resume to act on")


def test_a_session_that_puts_the_tree_BACK_is_not_a_modification(install):
    """The badge claims "this copy is not the one we released". A tree that is
    byte-identical to what this version shipped makes that claim false, however
    it got there — so a session that UNDOES an earlier patch must not be marked
    for the act of restoring the installation."""
    release = _pristine()
    (install / "jobs.py").write_text("RUNNING = 'running'  # patched\n")
    patched = selffix.tree_digest()
    assert patched != release

    # A second session opens on the PATCHED tree and puts it back: the file is
    # restored on disk BEFORE the stamp, or `current == before` short-circuits
    # and this proves nothing.
    (install / "jobs.py").write_text("RUNNING = 'running'\n")
    assert selffix.tree_digest() == release
    assert selffix.settle(before=patched, run_id="r-restore") is True, (
        "the tree really did move away from `before` — that is what it answers")
    assert selffix.status() is None, "restoring the release is not a modification"

    # ...and a tree that is genuinely somewhere else still marks.
    (install / "jobs.py").write_text("RUNNING = 'running'  # patched again\n")
    assert selffix.settle(before=patched, run_id="r-restore") is True
    assert selffix.status() is not None, "a real patch still marks"


def test_a_same_version_reinstall_under_a_leftover_pointer_stays_clean(
        install, monkeypatch):
    """SF-7d's own hazard, and the reason the veto lives in `settle` rather than
    in `resume`. The session pointer sits in the state dir, which a same-version
    reinstall does NOT remove — pip's RECORD never listed it, which is the whole
    reason `reconcile` exists. So a pointer can outlive the tree it described,
    with a `before` describing a PATCHED tree, and the reinstall restores the
    release underneath it. Settling against that `before` would light an amber
    badge on a copy byte-identical to what we shipped, and `reconcile` could not
    rescue it: it is a sibling startup thread that stands down when the marker
    moves under its walk."""
    release = _pristine()
    (install / "jobs.py").write_text("RUNNING = 'running'  # patched\n")
    patched = selffix.tree_digest()
    # A later session opened on the patched tree and recorded it.
    selffix.note_session("r-before-the-reinstall", before=patched)

    # The user reinstalls the same version: the tree is the release again, the
    # state dir (marker, baseline, pointer) survives.
    (install / "jobs.py").write_text("RUNNING = 'running'\n")
    assert selffix.tree_digest() == release
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive=set()))

    selffix_routes.resume()

    assert selffix.status() is None, (
        "a clean reinstall was stamped as modified — the badge would point at a "
        "report describing changes that are no longer on disk")


def test_a_finished_session_stops_costing_a_tree_walk_on_every_start(
        install, monkeypatch):
    """`resume` is a startup handler, and hashing the whole package on every
    boot for the rest of the installation's life is not a price this module
    pays (`reconcile` takes one `stat` on an ordinary start). Once the run is
    gone and its stamp is made, the pointer keeps naming the run for the guard
    but stops carrying a digest to measure against."""
    before = _pristine()
    selffix.note_session("r-finished", before=before)
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive=set()))

    selffix_routes.resume()

    assert selffix.session_record().get("before") == ""
    assert selffix.active_run() == "r-finished", (
        "the guard still needs the run named; only the digest is retired")

    walks = []
    real_digest = selffix.tree_digest
    monkeypatch.setattr(selffix, "tree_digest",
                        lambda *a, **k: walks.append(1) or real_digest(*a, **k))
    selffix_routes.resume()
    assert walks == [], "a later start re-walked the tree for a finished session"


def test_a_live_session_keeps_its_digest_across_the_resume(install, monkeypatch):
    """The retirement above is for FINISHED runs only. A session still editing
    needs its `before` on the next restart too — servers can bounce twice."""
    before = _pristine()
    selffix.note_session("r-still-going", before=before)
    monkeypatch.setattr(selffix_routes, "_load_agent",
                        lambda: _FakeAgent(live="", alive={"r-still-going"}))
    monkeypatch.setattr(selffix_routes, "_watch_fix", lambda *a, **k: None)

    selffix_routes.resume()
    _join_watchers()

    assert selffix.session_record().get("before") == before
