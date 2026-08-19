"""Tests for the Claude-file-history reader
(fused_render/templates/shared/file_history.py). SPEC §34.

Like appenv.py next to it this is a stdlib-only TEMPLATE module, not a package
module — a template child under the fused engine has no PYTHONPATH, so it can
never be `import fused_render...`ed (see annotate.py's docstring). These load it
by path the way test_annotate_revert.py loads annotate.py.

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


def test_declining_the_deltas_skips_difflib_and_says_so(claude_home, tmp_path,
                                                        monkeypatch):
    """`deltas=False` is the same degradation the byte cap already produces, asked
    for by the caller instead of forced by the content.

    It exists because this call is the ENTIRE cost of a timeline: measured at
    290 ms of a 292 ms read on a 453 KB file with 12 checkpoints, against 0.2 ms
    to enumerate the store. A panel that wants to paint a list on sight cannot pay
    a third of a second for two numbers a row.
    """
    fh = _load()
    f = _target(tmp_path, "keep\ngone1\ngone2\n")
    write_version(claude_home, "s", f, "keep\nnew1\nnew2\nnew3\n")

    calls = []
    real = fh.difflib.SequenceMatcher
    monkeypatch.setattr(fh.difflib, "SequenceMatcher",
                        lambda *a, **k: calls.append(1) or real(*a, **k))

    v = fh.list_versions(f, deltas=False)[0]
    assert not calls, "difflib ran for a caller that declined the deltas"
    assert v["exact"] is False
    assert v["differs"] is True          # still a byte comparison
    assert (v["added"], v["removed"]) == (1, 0)   # 4 lines vs 3, net only

    assert fh.list_versions(f, deltas=True)[0]["exact"] is True
    assert calls, "difflib must still run for the exact pair"


def test_the_deltas_flag_cannot_buy_precision_the_content_forbids(claude_home,
                                                                  tmp_path,
                                                                  monkeypatch):
    """The flag NARROWS only. Above the byte cap the answer is inexact whatever
    the caller asked for — otherwise `deltas=True` would be a way to reintroduce
    the quadratic diff the cap exists to prevent."""
    fh = _load()
    monkeypatch.setattr(fh, "DIFF_BYTE_CAP", 8)
    f = _target(tmp_path, "aaaa\nbbbb\ncccc\n")
    write_version(claude_home, "s", f, "aaaa\n")
    assert fh.list_versions(f, deltas=True)[0]["exact"] is False


def test_the_selection_is_the_same_whether_the_deltas_were_computed(claude_home,
                                                                    tmp_path):
    """The positional walk is built on `differs`, a byte comparison — so nothing
    the deltas flag touches can move where a revert would land. This is the
    guarantee that lets the panel render a cheap timeline and act on it."""
    fh = _load()
    f = _target(tmp_path, "a\nb\nc\n")
    write_version(claude_home, "s", f, "a\nB\nc\n", mtime=1000)
    write_version(claude_home, "s", f, "a\nb\nc\n", mtime=2000)
    write_version(claude_home, "s", f, "a\nb\nc\nd\n", mtime=3000)
    exact = fh.timeline(f)
    cheap = fh.timeline(f, deltas=False)
    for key in ("position", "revert", "offer", "at_earliest", "unconfirmed",
                "unique_current", "note"):
        assert exact[key] == cheap[key], key
    assert [v["id"] for v in exact["versions"]] == [v["id"] for v in cheap["versions"]]


# ------------------------------------------------- the null backup (new file)

def test_a_null_backup_becomes_a_file_did_not_exist_entry(claude_home, tmp_path):
    """Semantic 2. Only the transcript carries this — the filesystem cannot
    represent "no content" — so it arrives through opt-in enrichment."""
    fh = _load()
    f = _target(tmp_path, "written by claude\n")
    write_version(claude_home, "s", f, "written by claude\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
        delta_record(os.path.basename(f), path_hash(f) + "@v1", 1,
                     "2026-07-31T06:36:28.180Z",
                     real_parent_dir=str(tmp_path)),
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
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
        delta_record(os.path.basename(f), path_hash(f) + "@v1", 1,
                     "2026-07-31T06:36:28.180Z",
                     real_parent_dir=str(tmp_path)),
    ])
    ghost = [v for v in fh.list_versions(f, enrich=True) if not v["existed"]][0]

    plan = fh.revert_plan(f, ghost["id"])
    assert plan["ok"] is True
    assert plan["action"] == "delete"  # not an empty restore
    assert plan["removed"] == 1        # the one line currently on disk
    assert plan["added"] == 0

    fh.apply_revert(f, ghost["id"])
    assert not os.path.exists(f)


def test_a_null_backup_entry_differs_only_while_the_file_exists(claude_home,
                                                                tmp_path):
    fh = _load()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
        delta_record(os.path.basename(f), path_hash(f) + "@v1", 1,
                     "2026-07-31T06:36:28.180Z",
                     real_parent_dir=str(tmp_path)),
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
    chmod -w file, which is why the target's own writability is probed."""
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


# ============================================================ review findings
# Each of the following pins a defect a reviewer reproduced against this module.
# They are grouped here rather than merged above so the reason each rule exists
# stays legible.

# --- C1: ghost attribution ------------------------------------------------
# A did-not-exist row used to be accepted on nothing but a repo-relative
# path-boundary SUFFIX match, with no project attribution at all. `src/main.py`,
# `README.md` and `index.ts` recur across repositories, so an unrelated project
# that created its own `src/main.py` injected a ghost into THIS file's timeline —
# and since it sorts by that transcript's timestamp it was typically newest, so
# it became the revert target and turned "Revert last change" into a DELETE of a
# file Claude never created. The record already carries `realParentDir`, an
# ABSOLUTE directory, so the rule is now an identity test.

def test_a_ghost_from_another_project_with_the_same_relative_path_is_ignored(
        claude_home, tmp_path):
    fh = _load()
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    target = _target(mine, "edited by the human\n", name="main.py")
    write_version(claude_home, "s-mine", target, "my content\n", mtime=1785479788)
    # A DIFFERENT project created its own src/main.py. Same relative path, same
    # basename, different tree — and its record is NEWER, which is what made it
    # win the revert selection.
    write_transcript(claude_home, "s-theirs", str(theirs), [
        delta_record("main.py", None, 1, "2026-07-31T23:00:00.000Z",
                     real_parent_dir=str(theirs)),
    ])
    # ...and its session is in the store too, which is how the scan reached it.
    write_version(claude_home, "s-theirs", str(theirs / "main.py"), "theirs\n")

    vs = fh.list_versions(target, enrich=True)
    assert all(v["existed"] for v in vs), "a foreign project's ghost leaked in"
    plan = fh.revert_plan(target)
    assert plan["action"] == "restore"  # NOT delete


def test_a_ghost_is_accepted_on_an_exact_real_parent_dir_identity(claude_home,
                                                                  tmp_path):
    fh = _load()
    f = _target(tmp_path, "made by claude\n")
    write_version(claude_home, "s", f, "made by claude\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 1, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
    ])
    ghosts = [v for v in fh.list_versions(f, enrich=True) if not v["existed"]]
    assert len(ghosts) == 1


def test_a_ghost_with_a_matching_dir_but_another_basename_is_ignored(claude_home,
                                                                     tmp_path):
    fh = _load()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record("sibling.md", None, 1, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
    ])
    assert all(v["existed"] for v in fh.list_versions(f, enrich=True))


def test_a_ghost_without_a_real_parent_dir_is_REFUSED_not_guessed(claude_home,
                                                                  tmp_path):
    """The fallback refuses rather than falling back to the suffix heuristic:
    guessing here means offering a delete of the wrong file."""
    fh = _load()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 1, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=""),
    ])
    assert all(v["existed"] for v in fh.list_versions(f, enrich=True))


def test_the_transcript_is_found_without_iterating_every_project_slug(claude_home,
                                                                     tmp_path):
    """A glob on `projects/*/<session>.jsonl` replaces the slug cross-product —
    the identity test makes the slug irrelevant, so a transcript filed under a
    slug that does not match the target's cwd still enriches correctly."""
    fh = _load()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n", mtime=1785479788)
    write_transcript(claude_home, "s", "/some/other/-slug", [
        delta_record(os.path.basename(f), None, 1, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
    ])
    assert any(not v["existed"] for v in fh.list_versions(f, enrich=True))


# --- I1: a skipped version must not silently retarget the revert ----------

def test_an_unreadable_version_is_reported_not_swallowed(claude_home, tmp_path,
                                                          monkeypatch):
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "older\n", mtime=1000)
    write_version(claude_home, "s", f, "newest\n", mtime=2000)
    real_open = open

    def flaky(path, *a, **kw):
        if "@v2" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    tl = fh.timeline(f)
    assert len(tl["skipped"]) == 1
    assert tl["skipped"][0]["version"] == 2
    assert "13" in tl["note"] or "Permission" in tl["note"]


def test_revert_refuses_rather_than_retargeting_past_a_skipped_version(
        claude_home, tmp_path, monkeypatch):
    """The user presses "Revert last change" and would otherwise get a
    possibly-much-older checkpoint, presented by the confirm sheet as the
    newest."""
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "much older\n", mtime=1000)
    write_version(claude_home, "s", f, "the real newest\n", mtime=2000)
    real_open = open

    def flaky(path, *a, **kw):
        if "@v2" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    plan = fh.revert_plan(f)
    assert plan["ok"] is False
    assert plan["skipped"]
    assert "could not be read" in plan["error"]


def test_an_older_skipped_version_does_not_block_the_revert(claude_home,
                                                            tmp_path,
                                                            monkeypatch):
    """Refusing on ANY skip would make one unreadable ancient checkpoint disable
    the feature. Only a skip that could have been the target matters."""
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "ancient\n", mtime=1000)
    write_version(claude_home, "s", f, "newest\n", mtime=2000)
    real_open = open

    def flaky(path, *a, **kw):
        if "@v1" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    plan = fh.revert_plan(f)
    assert plan["ok"] is True
    assert plan["version"] == 2


def test_an_explicit_version_id_is_not_blocked_by_a_skip(claude_home, tmp_path,
                                                          monkeypatch):
    """The refusal is about the AUTOMATIC choice. A user who clicked a specific
    row named the version themselves; there is nothing to guess."""
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "wanted\n", mtime=1000)
    write_version(claude_home, "s", f, "unreadable\n", mtime=2000)
    real_open = open

    def flaky(path, *a, **kw):
        if "@v2" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    assert fh.revert_plan(f, "s@v1")["ok"] is True


# --- I2: symlinked targets -------------------------------------------------

def test_a_symlinked_target_is_refused(claude_home, tmp_path):
    """`os.replace` swaps the LINK for a regular file instead of writing through
    it, so the real file keeps its pre-revert content while the call reports
    success — and the stash captured a file that was never overwritten."""
    fh = _load()
    real = tmp_path / "real.txt"
    real.write_text("real content\n")
    link = tmp_path / "link.txt"
    os.symlink(str(real), str(link))
    write_version(claude_home, "s", str(link), "wanted\n")

    with pytest.raises(ValueError):
        fh.apply_revert(str(link), "s@v1")
    assert os.path.islink(str(link))
    with open(str(real), encoding="utf-8") as h:
        assert h.read() == "real content\n"


# --- I3: enrichment must not change what a revert DOES --------------------

def test_the_plan_and_the_write_always_see_the_did_not_exist_rows(claude_home,
                                                                  tmp_path):
    """`enrich` used to be a parameter of revert_plan/apply_revert, and the view
    passed its disclosure-widget state — so the SAME button performed a restore
    before History was expanded and a delete after. One click, two different
    destructive outcomes, decided by whether a panel was open. There is now no
    parameter to get wrong."""
    import inspect

    fh = _load()
    assert "enrich" not in inspect.signature(fh.revert_plan).parameters
    assert "enrich" not in inspect.signature(fh.apply_revert).parameters

    f = _target(tmp_path, "made by claude\n")
    write_version(claude_home, "s", f, "made by claude\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 1, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
    ])
    # Nobody asked for enrichment, and the answer is still the delete.
    plan = fh.revert_plan(f)
    assert plan["action"] == "delete"
    fh.apply_revert(f, plan["id"])
    assert not os.path.exists(f)


# --- I6: the read-only-mount probe must fail CLOSED ----------------------

def test_a_failing_mount_probe_is_treated_as_not_writable(claude_home, tmp_path,
                                                           monkeypatch):
    """The blanket `except Exception` used to wrap the probe CALL as well as the
    import, so any failure inside it fell through to os.access — which lies under
    a read-only mount with CacheMode=full. A doomed revert then reported ok:True
    and the 403 arrived later at the async upload, never reaching this UI."""
    fh = _load()
    f = _target(tmp_path)
    import appenv

    def boom(_path):
        raise TypeError("malformed FUSED_RENDER_RO_MOUNTS")

    monkeypatch.setattr(appenv, "mount_read_only", boom)
    assert fh.file_writable(f) is False


# --- M2: an unreadable store is not a fact about the file ----------------

@skip_root
def test_an_unlistable_history_root_is_named_not_reported_as_no_versions(
        claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path)
    root = os.path.join(str(claude_home), "file-history")
    os.makedirs(root)
    os.chmod(root, 0o000)
    try:
        tl = fh.timeline(f)
        assert tl["available"] is True      # it EXISTS; we just cannot read it
        assert "13" in tl["note"] or "Permission" in tl["note"]
        assert root in tl["note"]
        assert "no recorded versions" not in tl["note"]
    finally:
        os.chmod(root, 0o755)


@skip_root
def test_an_unlistable_session_dir_names_the_path_and_errno(claude_home,
                                                             tmp_path):
    fh = _load()
    f = _target(tmp_path)
    write_version(claude_home, "good", f, "readable\n", mtime=1000)
    bad = os.path.join(str(claude_home), "file-history", "bad")
    os.makedirs(bad)
    os.chmod(bad, 0o000)
    try:
        tl = fh.timeline(f)
        assert [v["session"] for v in tl["versions"]] == ["good"]
        assert len(tl["skipped"]) == 1
        note = tl["skipped"][0]["reason"]
        assert bad in note
        assert "13" in note or "Permission" in note
    finally:
        os.chmod(bad, 0o755)


# --- the revert rule is POSITIONAL, not "newest that differs" --------------
# The user pressed the button twice and it oscillated. "Newest version whose
# content differs from disk" answers "which checkpoint is most recent and isn't
# what I have", which is not what undo means:
#
#   disk == v3 -> v3 differs=False, v2 differs=True -> target v2
#   disk == v2 -> v3 differs=True  (newest in list) -> target v3
#
# ...forever, with v1 unreachable at any point. Undo is positional: find where
# disk sits in the chain, then step BACKWARDS. The differs-check survives inside
# the backward walk so a duplicate-content version cannot be chosen as a no-op.

def _chain(claude_home, tmp_path):
    f = _target(tmp_path, "v3\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "v2\n", mtime=2000)
    write_version(claude_home, "s", f, "v3\n", mtime=3000)
    return f


def test_revert_steps_backwards_from_the_current_position(claude_home, tmp_path):
    fh = _load()
    f = _chain(claude_home, tmp_path)
    plan = fh.revert_plan(f)
    assert (plan["version"], plan["action"]) == (2, "restore")


def test_pressing_revert_again_keeps_going_back_instead_of_oscillating(
        claude_home, tmp_path):
    """The whole bug, as a sequence. Each step must be strictly older than the
    last — the old rule went v3 -> v2 -> v3 -> v2 for ever."""
    fh = _load()
    f = _chain(claude_home, tmp_path)
    seen = []
    for _ in range(2):
        plan = fh.revert_plan(f)
        assert plan["ok"] is True
        seen.append(plan["version"])
        fh.apply_revert(f, plan["id"])
    assert seen == [2, 1]
    with open(f, encoding="utf-8") as h:
        assert h.read() == "v1\n"


def test_the_earliest_checkpoint_is_a_distinct_terminal_state(claude_home,
                                                              tmp_path):
    """...and at the bottom of the chain it STOPS, rather than falling back to
    something newer (which is what made it a two-state toggle)."""
    fh = _load()
    f = _chain(claude_home, tmp_path)
    for _ in range(2):
        fh.apply_revert(f, fh.revert_plan(f)["id"])
    plan = fh.revert_plan(f)
    assert plan["ok"] is False
    assert plan["at_earliest"] is True
    assert "earliest" in plan["error"]
    # Only an ENRICHED timeline may claim the terminal state — an unenriched one
    # cannot see a did-not-exist boundary and would claim it a step early.
    tl = fh.timeline(f, enrich=True)
    assert tl["revert"] is None
    assert tl["at_earliest"] is True
    assert "earliest" in tl["note"]


def test_the_target_is_never_newer_than_the_current_position(claude_home,
                                                              tmp_path):
    fh = _load()
    f = _chain(claude_home, tmp_path)
    for _ in range(2):
        tl = fh.timeline(f)
        pos = next(v for v in tl["versions"] if v["id"] == tl["position"])
        target = next(v for v in tl["versions"] if v["id"] == tl["revert"])
        assert target["mtime"] < pos["mtime"]
        fh.apply_revert(f, tl["revert"])


def test_a_duplicate_content_version_is_skipped_by_the_backward_walk(
        claude_home, tmp_path):
    """v3 and v2 hold identical bytes, so disk matches both; position resolves to
    v3 and the target must step PAST v2 (restoring it would write the same bytes
    back) to v1. This is why step 2 keeps a differs-check instead of taking the
    entry at i+1."""
    fh = _load()
    f = _target(tmp_path, "same\n")
    write_version(claude_home, "s", f, "older\n", mtime=1000)
    write_version(claude_home, "s", f, "same\n", mtime=2000)
    write_version(claude_home, "s", f, "same\n", mtime=3000)
    plan = fh.revert_plan(f)
    assert plan["version"] == 1
    assert plan["ok"] is True


def test_content_in_no_checkpoint_steps_back_to_the_newest_one(claude_home,
                                                               tmp_path):
    """Position is nowhere in the chain, so the first step back is "discard to
    the most recent checkpoint" — and that is the same condition as
    `unique_current`, derived once so the two cannot disagree."""
    fh = _load()
    f = _target(tmp_path, "typed by the human, in no checkpoint\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "v2\n", mtime=2000)
    plan = fh.revert_plan(f)
    assert plan["version"] == 2          # the newest, not the oldest
    assert plan["unique_current"] is True
    assert fh.timeline(f)["position"] is None


def test_position_is_the_newest_matching_entry(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "dup\n")
    write_version(claude_home, "s", f, "dup\n", mtime=1000)
    write_version(claude_home, "s", f, "other\n", mtime=2000)
    write_version(claude_home, "s", f, "dup\n", mtime=3000)
    assert fh.timeline(f)["position"] == "s@v3"
    # ...so the step back is v2, not v1 — walking from the OLDEST match would
    # have skipped a real checkpoint.
    assert fh.revert_plan(f)["version"] == 2


def test_the_walk_can_terminate_in_the_creation_boundary(claude_home, tmp_path):
    """With enrichment forced on the plan (I3), a did-not-exist boundary is
    always present, so the chain genuinely ends in a delete rather than in a
    refusal one step early."""
    fh = _load()
    f = _target(tmp_path, "v1\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 1, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
    ])
    plan = fh.revert_plan(f)
    assert plan["action"] == "delete"
    fh.apply_revert(f, plan["id"])
    assert not os.path.exists(f)
    # And once the file is gone the boundary IS the position, so there is
    # nothing older to step to.
    assert fh.revert_plan(f)["at_earliest"] is True


def test_a_skip_that_could_have_established_position_refuses(claude_home,
                                                             tmp_path,
                                                             monkeypatch):
    """A dropped version now corrupts POSITION, not just the target — so a skip
    anywhere newer than the chosen target makes the whole walk unsafe."""
    fh = _load()
    f = _target(tmp_path, "v3\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "v2\n", mtime=2000)
    write_version(claude_home, "s", f, "v3\n", mtime=3000)
    real_open = open

    def flaky(path, *a, **kw):
        if "@v3" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    plan = fh.revert_plan(f)
    assert plan["ok"] is False
    assert "could not be read" in plan["error"]


def test_at_earliest_is_only_claimed_from_an_enriched_scan(claude_home,
                                                            tmp_path):
    """Found by pressing the button four times in the running app. The boot
    timeline skips the transcripts, so it cannot see the did-not-exist boundary
    and would report at_earliest one step early — and the view believed it and
    disabled the button on a file whose remaining step back was a delete that
    `revert_plan` (which always enriches) offers happily."""
    fh = _load()
    f = _target(tmp_path, "made by claude\n")
    write_version(claude_home, "s", f, "made by claude\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 1, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
    ])
    boot = fh.timeline(f)
    assert boot["enriched"] is False
    assert "earliest" not in boot["note"]   # must not claim it
    rich = fh.timeline(f, enrich=True)
    assert rich["enriched"] is True
    assert rich["revert"].endswith("@none1")  # the delete IS available
    # ...and the plan agrees with the enriched view, never with the boot one.
    assert fh.revert_plan(f)["action"] == "delete"


def test_at_earliest_is_still_claimed_once_enriched(claude_home, tmp_path):
    fh = _load()
    f = _target(tmp_path, "v1\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    tl = fh.timeline(f, enrich=True)
    assert tl["at_earliest"] is True and tl["enriched"] is True
    assert "earliest" in tl["note"]


# ============================================================= the plan's diff
# Aggregates ("+2 / −1", "1.2 kB after") answer how MUCH changes, never WHAT, and
# on a destructive confirm those are different questions. The diff rides on the
# plan rather than on a fourth action for the reason `revert_plan`'s own docstring
# gives: a plan built against one disk state and applied against another is how a
# user confirms one diff and gets a different one, and the plan's `id` is already
# the freshness token the write demands back.

def _diff_body(plan):
    """The diff's content lines — no `---`/`+++` names, no `@@` hunk headers."""
    return [ln for ln in plan["diff"]["lines"][2:] if not ln.startswith("@@")]


def test_the_plan_carries_the_diff_the_confirm_sheet_renders(claude_home,
                                                            tmp_path):
    fh = _load()
    f = _target(tmp_path, "a\nb\nc\n")
    write_version(claude_home, "s", f, "a\nB\nc\n")
    diff = fh.revert_plan(f)["diff"]
    assert diff["reason"] == "" and diff["truncated"] is False
    # One line replaced: the restore takes `b` away and puts `B` back.
    assert "-b" in diff["lines"] and "+B" in diff["lines"]
    assert diff["changed"] == 2
    # No trailing newlines — the view builds one node per line.
    assert all(not ln.endswith("\n") for ln in diff["lines"])


def test_the_diff_is_framed_as_what_the_restore_does(claude_home, tmp_path):
    """The same framing (and the same reason for it) as `_delta`: disk is the
    "from" side and the version the "to" side. The reverse reads identically on a
    symmetric edit and lies on every asymmetric one."""
    fh = _load()
    f = _target(tmp_path, "keep\ngone\n")
    write_version(claude_home, "s", f, "keep\n")
    body = _diff_body(fh.revert_plan(f))
    # Restoring v1 REMOVES `gone`; the reverse framing would call it an addition.
    assert "-gone" in body and "+gone" not in body


def test_the_diff_headers_name_the_checkpoint_not_the_stores_path(claude_home,
                                                                 tmp_path):
    """The version's path inside Claude Code's history store is a hashed filename
    the user cannot open or act on, so it has no business in a header. The session
    is there because it is the only thing that tells two rows both called v2 apart
    (semantic 4)."""
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s0mesess", f, "old\n")
    lines = fh.revert_plan(f)["diff"]["lines"]
    assert lines[0] == "--- on disk now"
    assert lines[1].startswith("+++ v1 (session s0mesess")
    assert path_hash(f) not in "\n".join(lines)
    assert str(claude_home) not in "\n".join(lines)


def test_a_delete_diffs_as_every_current_line_removed(claude_home, tmp_path):
    """Honest and useful: the sheet says "DELETES it", and this is what that
    costs, line by line."""
    fh = _load()
    f = _target(tmp_path, "one\ntwo\n")
    write_version(claude_home, "s", f, "one\ntwo\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 1, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
    ])
    plan = fh.revert_plan(f)
    assert plan["action"] == "delete"
    assert _diff_body(plan) == ["-one", "-two"]
    assert plan["diff"]["changed"] == 2
    assert "did not exist" in plan["diff"]["lines"][1]


def test_an_absent_target_diffs_as_every_line_added(claude_home, tmp_path):
    """The mirror of the delete, and it falls out of `_current` modelling absence
    as `[]` lines — "Claude deleted my file" gets a diff, not a blank panel."""
    fh = _load()
    f = str(tmp_path / "gone.txt")
    write_version(claude_home, "s", f, "back\nagain\n")
    plan = fh.revert_plan(f)
    assert plan["current"]["exists"] is False
    assert _diff_body(plan) == ["+back", "+again"]
    assert plan["diff"]["changed"] == 2


def test_content_over_the_byte_cap_is_not_diffed_at_all(claude_home, tmp_path,
                                                        monkeypatch):
    """The same guard `_delta` degrades over, and for the same reason (difflib is
    quadratic). A PARTIAL diff presented as complete would be the worse answer:
    the sheet's whole job is to show what the click does."""
    fh = _load()
    monkeypatch.setattr(fh, "DIFF_BYTE_CAP", 8)
    f = _target(tmp_path, "aaaa\nbbbb\ncccc\n")
    write_version(claude_home, "s", f, "aaaa\n")
    diff = fh.revert_plan(f)["diff"]
    assert diff["lines"] == [] and diff["changed"] == 0
    assert "too large" in diff["reason"]


def test_binary_content_on_either_side_says_so_instead_of_diffing(claude_home,
                                                                 tmp_path):
    """Binary checkpoints are ordinary and perfectly restorable byte-for-byte —
    they just have no lines, exactly as `_lines` reports for the delta."""
    fh = _load()
    f = tmp_path / "img.bin"
    f.write_bytes(b"\xff\xd8\xff\x00")
    write_version(claude_home, "s", str(f), "text\n")
    diff = fh.revert_plan(str(f))["diff"]
    assert diff["lines"] == [] and diff["changed"] == 0
    assert "not UTF-8 text" in diff["reason"]


def test_an_unreadable_version_reports_the_path_and_the_errno(claude_home,
                                                             tmp_path,
                                                             monkeypatch):
    """Through `_why`, like every other caller of `_read`: a permissions problem
    is a fact about the machine and `chmod` is actionable, where "no diff" is not.
    A version unreadable at SCAN time is skipped and the plan refuses outright, so
    the only way here is the race — readable when enumerated, gone by the time the
    diff re-reads it — and the plan must still stand, because `apply_revert` reads
    the version for itself and would say so then."""
    fh = _load()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "old\n")
    real_open = open
    reads = []

    def flaky(path, *a, **kw):
        if "@v1" in str(path):
            reads.append(str(path))
            if len(reads) > 1:   # the scan's read succeeds; the diff's does not
                raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    plan = fh.revert_plan(f)
    assert plan["ok"] is True and plan["skipped"] == []
    assert plan["diff"]["lines"] == []
    assert "Permission denied" in plan["diff"]["reason"]
    assert "errno 13" in plan["diff"]["reason"]
    assert reads[0] in plan["diff"]["reason"]


def test_a_long_diff_is_cut_but_still_counts_the_whole_change(claude_home,
                                                             tmp_path,
                                                             monkeypatch):
    """`changed` is the FULL diff's count, so the trailing "view was cut" line can
    say how much the sheet is not showing. A truncated count would make the
    disclosure label understate the change it is hiding."""
    fh = _load()
    monkeypatch.setattr(fh, "DIFF_LINE_CAP", 10)
    f = _target(tmp_path, "".join("new-%d\n" % i for i in range(30)))
    write_version(claude_home, "s", f, "".join("old-%d\n" % i for i in range(30)))
    diff = fh.revert_plan(f)["diff"]
    assert diff["truncated"] is True
    assert len(diff["lines"]) == 10
    assert diff["changed"] == 60      # 30 removed + 30 added, uncut


def test_identical_content_says_there_is_nothing_to_show(claude_home, tmp_path):
    """An explicitly clicked version may hold exactly what is on disk (the
    automatic walk skips those). An empty `<pre>` would read as a broken diff, so
    the "why" channel carries it like every other no-diff case."""
    fh = _load()
    f = _target(tmp_path, "same\n")
    write_version(claude_home, "s", f, "same\n")
    diff = fh.revert_plan(f, "s@v1")["diff"]
    assert diff["lines"] == [] and diff["changed"] == 0
    assert diff["reason"]


def test_a_refused_plan_carries_no_diff_at_all(claude_home, tmp_path):
    """`ok: False` is "there is nothing to revert to", so there is nothing to
    diff either — and a `diff` key on a refusal is a thing the view would have to
    remember not to render."""
    fh = _load()
    plan = fh.revert_plan(_target(tmp_path))
    assert plan["ok"] is False
    assert "diff" not in plan
