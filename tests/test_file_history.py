"""Tests for the Claude-file-history reader
(fused_render/templates/shared/file_history.py). SPEC §33.

Like appenv.py next to it this is a stdlib-only TEMPLATE module, not a package
module — a template child under the fused engine has no PYTHONPATH, so it can
never be `import fused_render...`ed (see annotate.py's docstring). These load it
by path the way test_annotate_comments.py loads annotate.py.

Every test runs against a synthetic store under CLAUDE_CONFIG_DIR (the
`claude_home` fixture); nothing here may read the real ~/.claude, which is the
user's live edit history.

The three semantics that are easy to get wrong and are each pinned below:
  1. versions are CHECKPOINTS, not per-edit pre-images — the newest `@vN` often
     is NOT what is on disk, so "revert last change" means the newest version
     whose content DIFFERS from disk;
  2. `backupFileName: null` means the file did not exist yet — reverting across
     that boundary is a DELETE;
  3. chains are PER-SESSION, so version numbers collide across sessions and a
     global timeline must merge on time, never on N.
"""
import importlib.util
import os

import pytest

from _claude_history import (  # noqa: F401  (claude_home is a fixture)
    claude_home, delta_record, path_hash, write_transcript, write_version)

skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _load():
    path = os.path.join(os.path.dirname(__file__), "..", "fused_render",
                        "templates", "shared", "file_history.py")
    spec = importlib.util.spec_from_file_location("file_history_target", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _target(tmp_path, content="a\nb\nc\n", name="page.html"):
    f = tmp_path / name
    f.write_text(content)
    return str(f)


# ------------------------------------------------------------- config dir

def test_config_dir_honors_the_env_var(claude_home):
    assert _load().config_dir() == str(claude_home)


def test_config_dir_defaults_under_the_home_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    fh = _load()
    # expanduser, not a hardcoded POSIX join — this ships on Windows too.
    assert fh.config_dir() == os.path.expanduser(os.path.join("~", ".claude"))


def test_history_root_sits_under_the_config_dir(claude_home):
    fh = _load()
    assert fh.history_root() == os.path.join(str(claude_home), "file-history")


# ------------------------------------------------------------- hash derivation

def test_the_hash_is_sha256_of_the_absolute_path_truncated_to_16(claude_home):
    import hashlib
    fh = _load()
    p = os.path.join(os.sep + "abs", "some", "file.py")
    want = hashlib.sha256(p.encode()).hexdigest()[:16]
    assert fh.path_hash(p) == want
    assert len(fh.path_hash(p)) == 16


def test_the_hash_is_taken_of_the_ABSOLUTE_path(claude_home, tmp_path,
                                                monkeypatch):
    """A relative path must hash identically to its abspath, or the store lookup
    silently finds nothing whenever the caller passes a relative path."""
    fh = _load()
    monkeypatch.chdir(tmp_path)
    assert fh.path_hash("page.html") == fh.path_hash(str(tmp_path / "page.html"))


# ------------------------------------------------------------- enumeration

def test_versions_are_enumerated_from_the_filesystem_alone(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path)
    write_version(claude_home, "sess-a", f, "v1\n", mtime=1000)
    write_version(claude_home, "sess-a", f, "v2\n", mtime=2000)

    vs = fh.list_versions(f)
    assert [v["version"] for v in vs] == [2, 1]  # newest-first
    assert all(v["session"] == "sess-a" for v in vs)
    assert all(v["existed"] for v in vs)
    # No transcript exists at all, and enumeration did not need one.
    assert not os.path.isdir(os.path.join(str(claude_home), "projects"))


def test_another_files_versions_are_not_claimed(claude_home, tmp_path):
    fh = _load()
    mine = _target(tmp_path, name="mine.html")
    theirs = _target(tmp_path, name="theirs.html")
    write_version(claude_home, "s", theirs, "not mine\n")
    assert fh.list_versions(mine) == []


def test_cross_session_merge_orders_by_time_not_by_version_number(claude_home,
                                                                  tmp_path):
    """Two sessions each holding a `@v2` for the same path is the normal case.
    Ordering by N would interleave them wrongly and make "the last change"
    resolve to whichever session happened to count higher."""
    fh = _load()
    f = _target(tmp_path)
    # sess-old wrote v1,v2 yesterday; sess-new wrote v1,v2 today. The correct
    # timeline is new-v2, new-v1, old-v2, old-v1 — which sorting on `version`
    # could never produce.
    write_version(claude_home, "sess-old", f, "old1\n", mtime=1000)
    write_version(claude_home, "sess-old", f, "old2\n", mtime=2000)
    write_version(claude_home, "sess-new", f, "new1\n", mtime=3000)
    write_version(claude_home, "sess-new", f, "new2\n", mtime=4000)

    vs = fh.list_versions(f)
    assert [(v["session"], v["version"]) for v in vs] == [
        ("sess-new", 2), ("sess-new", 1), ("sess-old", 2), ("sess-old", 1)]
    # The colliding numbers are both present — a version is identified by the
    # PAIR, never by N alone.
    assert sum(1 for v in vs if v["version"] == 2) == 2


def test_a_version_carries_its_size_lines_and_mtime(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path)
    write_version(claude_home, "s", f, "one\ntwo\nthree\n", mtime=4242)
    v = fh.list_versions(f)[0]
    assert v["lines"] == 3
    assert v["size"] == len("one\ntwo\nthree\n")
    assert v["mtime"] == 4242


# ---------------------------------------------- differs-from-disk selection

def test_the_newest_version_is_often_not_what_is_on_disk(claude_home, tmp_path):
    """Semantic 1. Verified empirically on a real session: 6 of 13 files matched
    their highest @vN, 7 did not. So `differs` is per-version content
    comparison, not "is this the highest N"."""
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "old\n", mtime=1000)
    write_version(claude_home, "s", f, "disk\n", mtime=2000)   # == disk
    write_version(claude_home, "s", f, "newer\n", mtime=3000)  # != disk

    vs = fh.list_versions(f)
    assert [v["differs"] for v in vs] == [True, False, True]


def test_revert_plan_picks_the_newest_version_that_differs_from_disk(
        claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "older\n", mtime=1000)
    write_version(claude_home, "s", f, "disk\n", mtime=2000)  # newest, identical

    plan = fh.revert_plan(f)
    assert plan["ok"] is True
    assert plan["action"] == "restore"
    # NOT v2 (which is a no-op) — v1, the newest version that would change
    # anything.
    assert (plan["session"], plan["version"]) == ("s", 1)
    assert plan["id"] == "s@v1"  # what the confirm step sends back verbatim


def test_revert_plan_says_so_when_nothing_differs(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "disk\n")
    plan = fh.revert_plan(f)
    assert plan["ok"] is False
    assert "error" in plan  # informative state, never an exception


def test_the_line_delta_describes_what_the_restore_would_do(claude_home,
                                                            tmp_path):
    """+N/−M is stated in terms of the CHANGE THE RESTORE MAKES — lines it
    introduces and lines it takes away — because that is the number the confirm
    step has to show. The reverse framing reads the same on symmetric edits and
    lies on every asymmetric one."""
    fh = _load()
    f = _target(tmp_path, "keep\ngone1\ngone2\n")
    write_version(claude_home, "s", f, "keep\nnew1\nnew2\nnew3\n")
    v = fh.list_versions(f)[0]
    assert v["added"] == 3
    assert v["removed"] == 2
    assert v["exact"] is True


def test_an_identical_version_has_a_zero_delta(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "same\n")
    write_version(claude_home, "s", f, "same\n")
    v = fh.list_versions(f)[0]
    assert (v["added"], v["removed"], v["differs"]) == (0, 0, False)


def test_a_huge_file_reports_an_inexact_delta_instead_of_diffing(claude_home,
                                                                 tmp_path,
                                                                 monkeypatch):
    """difflib is quadratic in the worst case, so above a byte cap the delta
    degrades to a line-count difference and SAYS it is inexact — a render must
    not be able to hang on a big file."""
    fh = _load()
    monkeypatch.setattr(fh, "DIFF_BYTE_CAP", 8)
    f = _target(tmp_path, "aaaa\nbbbb\ncccc\n")
    write_version(claude_home, "s", f, "aaaa\n")
    v = fh.list_versions(f)[0]
    assert v["exact"] is False
    assert v["differs"] is True
    assert (v["added"], v["removed"]) == (0, 2)  # 1 line vs 3, net only


# ------------------------------------------------- the null backup (new file)

def test_a_null_backup_becomes_a_file_did_not_exist_entry(claude_home, tmp_path):
    """Semantic 2. Only the transcript carries this — the filesystem cannot
    represent "no content" — so it arrives through opt-in enrichment."""
    fh = _load()
    f = _target(tmp_path, "written by claude\n")
    write_version(claude_home, "s", f, "written by claude\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z"),
        delta_record(os.path.basename(f), path_hash(f) + "@v1", 1,
                     "2026-07-31T06:36:28.180Z"),
    ])

    plain = fh.list_versions(f)
    assert all(v["existed"] for v in plain)  # not without asking

    vs = fh.list_versions(f, enrich=True)
    ghost = [v for v in vs if not v["existed"]]
    assert len(ghost) == 1
    assert ghost[0]["path"] is None
    assert ghost[0]["lines"] == 0 and ghost[0]["size"] == 0
    # It is the OLDEST entry: the file did not exist before v1 created it.
    assert vs[-1] is ghost[0]


def test_reverting_across_a_null_backup_is_a_delete(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "written by claude\n")
    write_version(claude_home, "s", f, "written by claude\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z"),
        delta_record(os.path.basename(f), path_hash(f) + "@v1", 1,
                     "2026-07-31T06:36:28.180Z"),
    ])
    ghost = [v for v in fh.list_versions(f, enrich=True) if not v["existed"]][0]

    plan = fh.revert_plan(f, ghost["id"], enrich=True)
    assert plan["ok"] is True
    assert plan["action"] == "delete"  # not an empty restore
    assert plan["removed"] == 1        # the one line currently on disk
    assert plan["added"] == 0

    fh.apply_revert(f, ghost["id"], enrich=True)
    assert not os.path.exists(f)


def test_a_null_backup_entry_differs_only_while_the_file_exists(claude_home,
                                                                tmp_path):
    fh = _load()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z"),
        delta_record(os.path.basename(f), path_hash(f) + "@v1", 1,
                     "2026-07-31T06:36:28.180Z"),
    ])
    ghost = [v for v in fh.list_versions(f, enrich=True) if not v["existed"]][0]
    assert ghost["differs"] is True
    os.unlink(f)
    ghost = [v for v in fh.list_versions(f, enrich=True) if not v["existed"]][0]
    assert ghost["differs"] is False  # already absent — deleting is a no-op


# ------------------------------------------------------------- restore

def test_apply_revert_writes_the_version_content(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "current\n")
    write_version(claude_home, "s", f, "wanted\n")
    res = fh.apply_revert(f, "s@v1")
    assert res["ok"] is True and res["action"] == "restore"
    with open(f, encoding="utf-8") as fh_:
        assert fh_.read() == "wanted\n"


def test_apply_revert_replaces_atomically_and_leaves_no_temp(claude_home,
                                                             tmp_path):
    """mkstemp + os.replace in the target's OWN directory (same rule as
    _save_sidecar): a reader never sees a half-written file, and a cross-device
    rename can't happen."""
    fh = _load()
    f = _target(tmp_path, "current\n")
    write_version(claude_home, "s", f, "wanted\n")
    before = set(os.listdir(tmp_path))
    fh.apply_revert(f, "s@v1")
    assert set(os.listdir(tmp_path)) == before  # no .tmp left behind


def test_apply_revert_preserves_bytes_exactly(claude_home, tmp_path):
    fh = _load()
    body = "line\r\nwith crlf\n\tand a tab\nnö trailing newline"
    f = _target(tmp_path, "current\n")
    write_version(claude_home, "s", f, body)
    fh.apply_revert(f, "s@v1")
    with open(f, encoding="utf-8", newline="") as fh_:
        assert fh_.read() == body


def test_the_store_is_never_written(claude_home, tmp_path):
    """The feature is a strictly READ-ONLY consumer of the Claude config dir.
    Asserted as a whole-tree snapshot rather than per-call, so a future write
    anywhere under there trips this."""
    fh = _load()
    f = _target(tmp_path, "current\n")
    write_version(claude_home, "s", f, "wanted\n")

    def snap():
        out = {}
        for root, _dirs, names in os.walk(str(claude_home)):
            for n in names:
                p = os.path.join(root, n)
                out[p] = (os.path.getsize(p), open(p, "rb").read())
        return out

    before = snap()
    fh.list_versions(f)
    fh.timeline(f)
    fh.revert_plan(f)
    fh.apply_revert(f, "s@v1")
    assert snap() == before


# ------------------------------------------------------------- confinement

@pytest.mark.parametrize("entry_id", [
    "..@v1", ".@v1", "@v1", "../..@v1", "a/b@v1", "a" + os.sep + "b@v1",
    os.sep + "etc@v1", "..\\..\\x@v1",
    "../../../../etc/passwd", "s@v1/../../x", "s",
])
def test_a_crafted_entry_id_cannot_reach_anything(claude_home, tmp_path,
                                                   entry_id):
    """The selector the client sends is an OPAQUE id, matched against the
    enumerated timeline — never joined into a path. So traversal has nothing to
    traverse: an id that no enumerated entry claims simply does not resolve, and
    the only paths this module ever opens are ones it built itself from
    (history_root, session dir it listed, hash it derived)."""
    fh = _load()
    f = _target(tmp_path)
    write_version(claude_home, "s", f, "v\n")
    with pytest.raises(ValueError):
        fh.apply_revert(f, entry_id)


@pytest.mark.parametrize("entry_id", [
    "s@v0", "s@v-1", "s@v1; rm", None, 1, "s@v1.5", "s@vv1", "s@v01",
])
def test_a_bad_selector_is_refused(claude_home, tmp_path, entry_id):
    fh = _load()
    f = _target(tmp_path)
    write_version(claude_home, "s", f, "v\n")
    with pytest.raises(ValueError):
        fh.apply_revert(f, entry_id)


def test_a_target_with_no_versions_at_all_cannot_be_written(claude_home,
                                                            tmp_path):
    """The sharpest path guard available to a module that cannot see the view:
    a restore may only touch a path the STORE already knows, so a crafted
    `file` param reaches nothing Claude never edited."""
    fh = _load()
    victim = _target(tmp_path, "precious\n", name="victim.txt")
    known = _target(tmp_path, "known\n", name="known.txt")
    write_version(claude_home, "s", known, "payload\n")

    with pytest.raises(ValueError):
        fh.apply_revert(victim, "s@v1")
    with open(victim, encoding="utf-8") as h:
        assert h.read() == "precious\n"


def test_an_unknown_version_of_a_known_file_is_refused(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "current\n")
    write_version(claude_home, "s", f, "v1\n")
    with pytest.raises(ValueError):
        fh.apply_revert(f, "s@v9")
    with pytest.raises(ValueError):
        fh.apply_revert(f, "other-session@v1")
    with open(f, encoding="utf-8") as h:
        assert h.read() == "current\n"


def test_a_directory_target_is_refused(claude_home, tmp_path):
    fh = _load()
    d = tmp_path / "adir"
    d.mkdir()
    write_version(claude_home, "s", str(d), "payload\n")
    with pytest.raises(ValueError):
        fh.apply_revert(str(d), "s@v1")
    assert os.path.isdir(str(d))


# ------------------------------------------------------------- writability

@skip_root
def test_a_read_only_file_is_not_reverted(claude_home, tmp_path):
    """os.replace goes through the DIRECTORY, so it would happily overwrite a
    chmod -w file — the same trap _sidecar_writable documents."""
    fh = _load()
    f = _target(tmp_path, "current\n")
    write_version(claude_home, "s", f, "wanted\n")
    os.chmod(f, 0o444)
    try:
        assert fh.file_writable(f) is False
        with pytest.raises(PermissionError):
            fh.apply_revert(f, "s@v1")
        with open(f, encoding="utf-8") as h:
            assert h.read() == "current\n"
    finally:
        os.chmod(f, 0o644)


@skip_root
def test_a_read_only_directory_is_not_reverted(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "current\n")
    write_version(claude_home, "s", f, "wanted\n")
    os.chmod(tmp_path, 0o555)
    try:
        assert fh.file_writable(f) is False
        with pytest.raises(PermissionError):
            fh.apply_revert(f, "s@v1")
    finally:
        os.chmod(tmp_path, 0o755)


# ------------------------------------------------- read-only remote mounts
# os.access(W_OK) LIES under a read-only mount with CacheMode=full: the write
# lands in the local VFS cache and only 403s at the async upload (the
# sidecar-write incident). Only the shell's persisted read_only flag can answer
# this, and it arrives via shared/appenv's env contract — never by importing
# fused_render, which a template child can never do.

@pytest.fixture
def ro_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    import fused_render.shell.mounts as mounts

    m = mounts.add_mount("pub", "pub-remote:bucket", read_only=True)
    mp = mounts.mountpoint(m)
    os.makedirs(mp)
    f = os.path.join(mp, "page.html")
    with open(f, "w") as fh:
        fh.write("current\n")
    return f


def test_a_read_only_mount_refuses_the_revert(claude_home, ro_mount):
    fh = _load()
    write_version(claude_home, "s", ro_mount, "wanted\n")
    assert os.access(os.path.dirname(ro_mount), os.W_OK)  # the lie
    assert fh.file_writable(ro_mount) is False
    with pytest.raises(PermissionError):
        fh.apply_revert(ro_mount, "s@v1")
    with open(ro_mount, encoding="utf-8") as h:
        assert h.read() == "current\n"


def test_the_timeline_reports_writability_so_the_ui_can_disable_revert(
        claude_home, ro_mount):
    fh = _load()
    write_version(claude_home, "s", ro_mount, "wanted\n")
    assert fh.timeline(ro_mount)["writable"] is False


def test_writability_degrades_to_os_access_without_appenv(claude_home, ro_mount):
    """A copy of this folder taken without its `shared/` sibling has no appenv.
    The guard keeps the pure os.access rule rather than raising — the timeline
    still renders, it just cannot see mount read-only-ness."""
    import builtins
    import sys

    fh = _load()
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "appenv":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    saved = sys.modules.pop("appenv", None)
    builtins.__import__ = blocked
    try:
        assert fh.file_writable(ro_mount) is True
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["appenv"] = saved


# --------------------------------------------------------- the unique-content
# hazard: current disk content is frequently in NO checkpoint, so a naive
# restore vaporizes work with no undo. The plan has to say so, loudly enough for
# the UI to gate on it.

def test_the_plan_flags_current_content_that_no_version_holds(claude_home,
                                                              tmp_path):
    fh = _load()
    f = _target(tmp_path, "unsaved work\n")
    write_version(claude_home, "s", f, "old\n")
    plan = fh.revert_plan(f)
    assert plan["unique_current"] is True
    assert plan["current"]["lines"] == 1
    assert plan["current"]["size"] == len("unsaved work\n")


def test_the_plan_does_not_flag_content_some_version_still_holds(claude_home,
                                                                 tmp_path):
    fh = _load()
    f = _target(tmp_path, "checkpointed\n")
    write_version(claude_home, "s", f, "checkpointed\n", mtime=2000)
    write_version(claude_home, "s", f, "older\n", mtime=1000)
    plan = fh.revert_plan(f)
    assert plan["unique_current"] is False


def test_the_plan_carries_the_byte_and_line_counts_the_confirm_step_shows(
        claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "a\nb\nc\n")
    write_version(claude_home, "s", f, "a\n")
    plan = fh.revert_plan(f)
    assert plan["removed"] == 2 and plan["added"] == 0
    assert plan["version"] == 1
    assert plan["target"]["size"] == 2 and plan["target"]["lines"] == 1


# ------------------------------------------------------------- degradation
# Each of these is an EMPTY/INFORMATIVE state, never an exception and never a
# traceback overlay in the view.

def test_no_file_history_dir_at_all(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path)
    assert fh.list_versions(f) == []
    tl = fh.timeline(f)
    assert tl["available"] is False
    assert tl["versions"] == []
    assert tl["revert"] is None
    assert tl["note"]
    assert fh.revert_plan(f)["ok"] is False


def test_no_config_dir_at_all(tmp_path, monkeypatch):
    fh = _load()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nope"))
    f = _target(tmp_path)
    assert fh.timeline(f)["available"] is False


def test_a_file_with_no_versions_but_a_live_store(claude_home, tmp_path):
    fh = _load()
    other = _target(tmp_path, name="other.txt")
    write_version(claude_home, "s", other, "x\n")
    f = _target(tmp_path)
    tl = fh.timeline(f)
    assert tl["available"] is True   # the store exists...
    assert tl["versions"] == []      # ...it just has nothing for this file
    assert tl["note"]


@skip_root
def test_an_unreadable_session_dir_is_skipped_not_fatal(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path)
    write_version(claude_home, "good", f, "readable\n", mtime=1000)
    bad = os.path.join(str(claude_home), "file-history", "bad")
    os.makedirs(bad)
    write_version(claude_home, "bad", f, "hidden\n", mtime=2000)
    os.chmod(bad, 0o000)
    try:
        vs = fh.list_versions(f)
        assert [v["session"] for v in vs] == ["good"]
    finally:
        os.chmod(bad, 0o755)


def test_a_stray_non_directory_in_the_history_root_is_ignored(claude_home,
                                                              tmp_path):
    fh = _load()
    f = _target(tmp_path)
    write_version(claude_home, "s", f, "v\n")
    with open(os.path.join(str(claude_home), "file-history", ".DS_Store"),
              "w") as h:
        h.write("junk")
    assert len(fh.list_versions(f)) == 1


def test_malformed_version_filenames_are_ignored(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path)
    write_version(claude_home, "s", f, "real\n")
    d = os.path.join(str(claude_home), "file-history", "s")
    h = path_hash(f)
    for name in (h + "@v", h + "@vx", h + "@v1.2", h, h + "@v-1", h + "@v01"):
        with open(os.path.join(d, name), "w") as fp:
            fp.write("junk\n")
    vs = fh.list_versions(f)
    # `@v01` is a legitimate zero-padded 1... it is not: only the exact decimal
    # form the store writes is accepted, so anything else stays invisible rather
    # than becoming a second, ambiguous "version 1".
    assert [(v["session"], v["version"]) for v in vs] == [("s", 1)]
    assert fh.list_versions(f)[0]["lines"] == 1


def test_a_corrupt_transcript_does_not_break_enrichment(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n")
    p = write_transcript(claude_home, "s", str(tmp_path), [])
    with open(p, "w") as h:
        h.write("{not json\n\x00\x00\n[]\n")
    vs = fh.list_versions(f, enrich=True)
    assert [v["version"] for v in vs] == [1]  # the FS truth survives


def test_a_missing_transcript_does_not_break_enrichment(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n")
    assert len(fh.list_versions(f, enrich=True)) == 1


def test_an_oversized_transcript_is_not_parsed(claude_home, tmp_path,
                                               monkeypatch):
    """Real transcripts reach 5 MB+. Enrichment is already opt-in; above a cap
    it also refuses outright, so no view render can be held hostage by one."""
    fh = _load()
    monkeypatch.setattr(fh, "TRANSCRIPT_BYTE_CAP", 16)
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z"),
    ] * 50)
    vs = fh.list_versions(f, enrich=True)
    assert all(v["existed"] for v in vs)  # the ghost entry never appears


def test_a_version_file_that_vanishes_mid_scan_is_skipped(claude_home, tmp_path,
                                                          monkeypatch):
    fh = _load()
    f = _target(tmp_path)
    write_version(claude_home, "s", f, "v1\n")
    real_open = open

    def flaky(path, *a, **kw):
        if "@v1" in str(path):
            raise FileNotFoundError(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    assert fh.list_versions(f) == []


def test_a_binary_version_is_surfaced_without_a_line_delta(claude_home,
                                                            tmp_path):
    """Not every checkpointed file is text. A version that will not decode still
    belongs on the timeline (it is restorable byte-for-byte); it just has no
    honest line count."""
    fh = _load()
    f = str(tmp_path / "blob.bin")
    with open(f, "wb") as h:
        h.write(b"\xff\xfe\x00current")
    d = os.path.join(str(claude_home), "file-history", "s")
    os.makedirs(d)
    with open(os.path.join(d, path_hash(f) + "@v1"), "wb") as h:
        h.write(b"\xff\xfe\x00wanted")

    v = fh.list_versions(f)[0]
    assert v["differs"] is True
    assert v["exact"] is False
    fh.apply_revert(f, "s@v1")
    with open(f, "rb") as h:
        assert h.read() == b"\xff\xfe\x00wanted"


def test_a_target_that_does_not_exist_on_disk_can_still_be_restored(claude_home,
                                                                    tmp_path):
    """The undo for "Claude deleted my file": every version differs from an
    absent file, so the newest one is the plan."""
    fh = _load()
    f = str(tmp_path / "deleted.txt")
    write_version(claude_home, "s", f, "back\n", mtime=1000)
    tl = fh.timeline(f)
    assert tl["current"]["exists"] is False
    plan = fh.revert_plan(f)
    assert plan["ok"] is True and plan["action"] == "restore"
    assert plan["added"] == 1 and plan["removed"] == 0
    fh.apply_revert(f, "s@v1")
    with open(f, encoding="utf-8") as h:
        assert h.read() == "back\n"


# ------------------------------------------------------------- timeline shape

def test_the_timeline_is_the_whole_payload_the_view_needs(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "v2\n", mtime=2000)

    tl = fh.timeline(f)
    assert tl["file"] == os.path.abspath(f)
    assert tl["hash"] == path_hash(f)
    assert tl["available"] is True
    assert tl["writable"] is True
    assert tl["current"] == {"exists": True, "size": 5, "lines": 1}
    assert [v["version"] for v in tl["versions"]] == [2, 1]
    assert tl["revert"] == "s@v2"  # the opaque selector, ready to send back
    assert tl["note"] == ""
