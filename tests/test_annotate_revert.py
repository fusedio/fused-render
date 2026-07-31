"""Tests for annotate.py's revert actions — the seam between the annotate view
and the Claude-file-history reader (SPEC §33, D193).

`file_history.py` owns the store and the write; annotate.py owns two things it
cannot: turning every failure into an `{"error": ...}` DICT (a raised exception
would become the red traceback overlay, which is not an acceptable answer to
"this file has no history"), and stashing the pre-restore content into the
`<file>.json` sidecar it already read-merge-writes.

The stash is the answer to the sharpest hazard in the feature: current on-disk
content is frequently in NO checkpoint, so a restore can vaporize work that
exists nowhere else. The UI's confirm step is the first line of defence; this is
the second.
"""
import importlib.util
import json
import os

import pytest

from _claude_history import (  # noqa: F401  (claude_home is a fixture)
    claude_home, delta_record, path_hash, write_transcript, write_version)

skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root")


def _load_annotate():
    path = os.path.join("fused_render", "templates", "annotate", "annotate.py")
    spec = importlib.util.spec_from_file_location("annotate_target", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _target(tmp_path, content="a\nb\nc\n", name="page.html"):
    f = tmp_path / name
    f.write_text(content)
    return str(f)


def _sidecar(target):
    return target + ".json"


# ------------------------------------------------------------- history action

def test_history_returns_the_timeline(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "old\n", mtime=1000)
    out = ann.main(action="history", file=f)
    assert out["available"] is True
    assert [v["id"] for v in out["versions"]] == ["s@v1"]
    assert out["revert"] == "s@v1"
    assert out["writable"] is True


def test_history_with_no_store_is_an_empty_state_not_an_error(claude_home,
                                                             tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    out = ann.main(action="history", file=f)
    assert out["available"] is False
    assert out["versions"] == []
    assert out["revert"] is None
    assert out["note"]
    assert "error" not in out


def test_history_needs_a_file(claude_home):
    assert "error" in _load_annotate().main(action="history", file="")


def test_history_enrich_is_opt_in(claude_home, tmp_path):
    """The transcripts are the expensive half, so the boot-time call must not
    read them; only an expanded History panel asks."""
    ann = _load_annotate()
    f = _target(tmp_path, "made by claude\n")
    write_version(claude_home, "s", f, "made by claude\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z"),
    ])
    assert all(v["existed"] for v in ann.main(action="history", file=f)["versions"])
    rich = ann.main(action="history", file=f, enrich=True)["versions"]
    assert any(not v["existed"] for v in rich)


# ------------------------------------------------------------- plan action

def test_revert_plan_action_describes_the_write(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "unsaved\nwork\n")
    write_version(claude_home, "s", f, "old\n")
    plan = ann.main(action="revert_plan", file=f)
    assert plan["ok"] is True
    assert plan["action"] == "restore"
    assert plan["unique_current"] is True  # the confirm step must gate on this
    assert plan["removed"] == 2 and plan["added"] == 1


def test_revert_plan_with_nothing_to_revert(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path)
    plan = ann.main(action="revert_plan", file=f)
    assert plan["ok"] is False and plan["error"]


# ------------------------------------------------------------- revert action

def test_revert_restores_and_stashes_the_previous_content(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "unsaved work\n")
    write_version(claude_home, "s", f, "wanted\n")

    out = ann.main(action="revert", file=f, version_id="s@v1")
    assert out["ok"] is True and out["action"] == "restore"
    assert out["stashed"] is True
    with open(f, encoding="utf-8") as h:
        assert h.read() == "wanted\n"

    stash = json.loads(open(_sidecar(f), encoding="utf-8").read())["revertStash"]
    assert len(stash) == 1
    assert stash[0]["content"] == "unsaved work\n"
    assert stash[0]["version_id"] == "s@v1"
    assert stash[0]["size"] == len("unsaved work\n")
    assert stash[0]["at"] > 0


def test_revert_defaults_to_the_last_change(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "older\n", mtime=1000)
    write_version(claude_home, "s", f, "disk\n", mtime=2000)  # a no-op version
    out = ann.main(action="revert", file=f)
    assert out["ok"] is True and out["id"] == "s@v1"
    with open(f, encoding="utf-8") as h:
        assert h.read() == "older\n"


def test_the_stash_preserves_every_key_it_does_not_own(claude_home, tmp_path):
    """Same read-merge-write contract as `_record`: the sidecar is shared with
    the claude chat template and the bookmark mirror, so a revert must round-trip
    their keys instead of clobbering them off disk."""
    ann = _load_annotate()
    f = _target(tmp_path, "before\n")
    write_version(claude_home, "s", f, "after\n")
    sess = [{"id": "s1", "preview": "hi", "created_at": 1, "last_used": 1,
             "cwd": "/x"}]
    with open(_sidecar(f), "w", encoding="utf-8") as h:
        json.dump({"claudeSessions": sess, "lastSession": "s1",
                   "comments": [{"id": "c1", "content": "keep me"}]}, h)

    ann.main(action="revert", file=f, version_id="s@v1")
    data = json.loads(open(_sidecar(f), encoding="utf-8").read())
    assert data["claudeSessions"] == sess
    assert data["lastSession"] == "s1"
    assert data["comments"][0]["content"] == "keep me"
    assert len(data["revertStash"]) == 1


def test_the_stash_is_bounded(claude_home, tmp_path):
    """A sidecar is a small JSON file the claude template rewrites constantly;
    an unbounded stash of file copies would grow it without limit."""
    ann = _load_annotate()
    f = _target(tmp_path, "gen0\n")
    for i in range(1, 7):
        write_version(claude_home, "s", f, "gen%d\n" % i)
    for i in range(1, 7):
        ann.main(action="revert", file=f, version_id="s@v%d" % i)
    stash = json.loads(open(_sidecar(f), encoding="utf-8").read())["revertStash"]
    assert len(stash) == ann.STASH_KEEP
    # Newest last: the entries kept are the most recent ones.
    assert stash[-1]["content"] == "gen5\n"


def test_a_large_file_is_reverted_without_being_copied_into_the_sidecar(
        claude_home, tmp_path, monkeypatch):
    """Better a revert with no stash than a multi-megabyte sidecar — and the
    caller is TOLD, so the UI can make the confirm step firmer."""
    ann = _load_annotate()
    monkeypatch.setattr(ann, "STASH_BYTE_CAP", 8)
    f = _target(tmp_path, "far too much content to stash\n")
    write_version(claude_home, "s", f, "small\n")
    out = ann.main(action="revert", file=f, version_id="s@v1")
    assert out["ok"] is True
    assert out["stashed"] is False
    assert out["stash_note"]
    assert not os.path.exists(_sidecar(f))  # nothing written at all
    with open(f, encoding="utf-8") as h:
        assert h.read() == "small\n"


def test_binary_content_is_not_stashed_but_is_still_reverted(claude_home,
                                                             tmp_path):
    ann = _load_annotate()
    f = str(tmp_path / "blob.bin")
    with open(f, "wb") as h:
        h.write(b"\xff\xfe\x00current")
    d = os.path.join(str(claude_home), "file-history", "s")
    os.makedirs(d)
    with open(os.path.join(d, path_hash(f) + "@v1"), "wb") as h:
        h.write(b"\xff\xfe\x00wanted")

    out = ann.main(action="revert", file=f, version_id="s@v1")
    assert out["ok"] is True and out["stashed"] is False
    assert out["stash_note"]
    with open(f, "rb") as h:
        assert h.read() == b"\xff\xfe\x00wanted"


def test_a_delete_revert_stashes_the_whole_file_first(claude_home, tmp_path):
    """Reverting across a did-not-exist boundary removes the file outright, so
    the stash is the ONLY copy of what was there."""
    ann = _load_annotate()
    f = _target(tmp_path, "claude wrote this\n")
    write_version(claude_home, "s", f, "claude wrote this\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z"),
    ])
    out = ann.main(action="revert", file=f, version_id="s@none0", enrich=True)
    assert out["ok"] is True and out["action"] == "delete"
    assert out["stashed"] is True
    assert not os.path.exists(f)
    stash = json.loads(open(_sidecar(f), encoding="utf-8").read())["revertStash"]
    assert stash[0]["content"] == "claude wrote this\n"


# ------------------------------------------------------- failures stay data

def test_an_unknown_version_is_an_error_dict_not_a_traceback(claude_home,
                                                             tmp_path):
    """Anything raised out of `main` becomes the red traceback overlay, which is
    never the right answer here — so every failure crosses the bridge as data."""
    ann = _load_annotate()
    f = _target(tmp_path, "keep\n")
    write_version(claude_home, "s", f, "v1\n")
    out = ann.main(action="revert", file=f, version_id="nope@v9")
    assert "error" in out and out.get("ok") is not True
    with open(f, encoding="utf-8") as h:
        assert h.read() == "keep\n"


def test_revert_needs_a_file(claude_home):
    assert "error" in _load_annotate().main(action="revert", file="")


def test_a_target_the_store_never_recorded_is_refused(claude_home, tmp_path):
    ann = _load_annotate()
    victim = _target(tmp_path, "precious\n", name="victim.txt")
    known = _target(tmp_path, "known\n", name="known.txt")
    write_version(claude_home, "s", known, "payload\n")
    out = ann.main(action="revert", file=victim, version_id="s@v1")
    assert "error" in out
    with open(victim, encoding="utf-8") as h:
        assert h.read() == "precious\n"


@skip_root
def test_a_read_only_target_is_refused_as_data(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "keep\n")
    write_version(claude_home, "s", f, "v1\n")
    os.chmod(f, 0o444)
    try:
        out = ann.main(action="revert", file=f, version_id="s@v1")
        assert "error" in out
        with open(f, encoding="utf-8") as h:
            assert h.read() == "keep\n"
    finally:
        os.chmod(f, 0o644)


@pytest.fixture
def ro_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    import fused_render.shell.mounts as mounts

    m = mounts.add_mount("pub", "pub-remote:bucket", read_only=True)
    mp = mounts.mountpoint(m)
    os.makedirs(mp)
    f = os.path.join(mp, "page.html")
    with open(f, "w") as fh:
        fh.write("keep\n")
    return f


def test_a_read_only_mount_refuses_the_revert(claude_home, ro_mount):
    ann = _load_annotate()
    write_version(claude_home, "s", ro_mount, "wanted\n")
    assert os.access(os.path.dirname(ro_mount), os.W_OK)  # the lie
    out = ann.main(action="revert", file=ro_mount, version_id="s@v1")
    assert "error" in out
    with open(ro_mount, encoding="utf-8") as h:
        assert h.read() == "keep\n"
    assert ann.main(action="history", file=ro_mount)["writable"] is False


def test_the_store_is_never_written_by_any_action(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "wanted\n")

    def snap():
        out = {}
        for root, _dirs, names in os.walk(str(claude_home)):
            for n in names:
                p = os.path.join(root, n)
                out[p] = open(p, "rb").read()
        return out

    before = snap()
    ann.main(action="history", file=f, enrich=True)
    ann.main(action="revert_plan", file=f)
    ann.main(action="revert", file=f, version_id="s@v1")
    assert snap() == before


def test_history_degrades_when_the_shared_helper_is_missing(claude_home,
                                                            tmp_path):
    """A copy of the annotate folder taken without its `shared/` sibling has no
    file_history at all — the same degradation appenv already has. Revert simply
    is not offered; nothing else in the view breaks."""
    import builtins
    import sys

    ann = _load_annotate()
    f = _target(tmp_path)
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "file_history":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    saved = sys.modules.pop("file_history", None)
    builtins.__import__ = blocked
    try:
        assert "error" in ann.main(action="history", file=f)
        assert "error" in ann.main(action="revert", file=f, version_id="s@v1")
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["file_history"] = saved


# ------------------------------------------------- template source contracts
# The same idiom as test_annotate_template.py: these pin the handful of rules in
# template.html whose breakage is SILENT — no error anywhere, just a slow render,
# a destroyed file, or a panel that never appears.

TEMPLATE = os.path.join(
    os.path.dirname(__file__), "..", "fused_render", "templates", "annotate",
    "template.html")


@pytest.fixture(scope="module")
def source():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


def test_the_boot_history_call_does_not_enrich(source):
    """Enrichment reads the session transcripts, which reach 5 MB+. Paying that
    on every annotate boot is invisible — the panel still works, it just makes
    opening any annotated file slower — so the boot call is pinned to
    `loadHistory(false)` and enrichment is only ever reached from the toggle."""
    assert "loadHistory(false);" in source
    body = source[source.index("histToggle.addEventListener"):]
    body = body[:body.index("let pending")]
    assert "loadHistory(true)" in body
    assert "!enriched" in body  # ...and only once


def test_a_revert_can_only_be_issued_from_the_confirm_sheet(source):
    """The one destructive action in this view. If a `revert` call ever grows a
    second call site, the confirm sheet stops being a gate and becomes
    decoration — which is exactly the failure the user would only discover by
    losing a file."""
    sites = source.count('action: "revert"')
    assert sites == 1
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    assert 'action: "revert"' in handler[:handler.index("});")]


def test_the_confirm_sheet_states_what_the_write_costs(source):
    """`unique_current` means the bytes on disk are in no checkpoint at all, so
    the restore destroys the only copy. The warning must be driven by that flag
    rather than shown unconditionally, or it becomes noise people click past."""
    body = source[source.index("async function askRevert"):]
    body = body[:body.index('getElementById("confirmgo").addEventListener')]
    assert "plan.unique_current" in body
    assert "revertStash" in body           # says where the backstop lives
    assert "current.size" in body and "target.size" in body  # byte counts
    assert "plan.added" in body            # and the line delta


def test_a_failed_revert_says_so(source):
    """Caught by clicking Revert in the running app: `renderHistory()` rewrites
    histNote from the timeline's own note, so setting the error text and THEN
    re-rendering wiped it — every failed revert became a silent no-op, the worst
    possible outcome for a destructive action's error path."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    branch = handler[handler.index("if (out.error"):handler.index("// Reload the framed")]
    assert branch.index("renderHistory()") < branch.index('histNote.textContent')


def test_the_framed_view_is_reloaded_after_a_revert(source):
    """A code editor framed here is holding a buffer that the revert just made
    stale; its next save would write the pre-revert content straight back."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    assert "location.reload()" in handler


def test_the_history_block_is_not_inside_the_claude_footer(source):
    """#sidefoot is display:none'd wherever the file has no claude mode (Send to
    Claude would go nowhere). Reverting has nothing to do with that, so the
    block is its own footer — nesting it would make revert vanish on every file
    type without a chat view."""
    assert 'id="histfoot"' in source
    foot = source[source.index('<div id="sidefoot">'):]
    assert 'id="histfoot"' not in foot[:foot.index("</div>")]


def test_the_row_count_is_the_rows_actually_rendered(source):
    """Found on screen: the collapsed label said "History (2)" while the expanded
    list drew 3 rows. Both numbers were right for their moment — the
    did-not-exist checkpoint only exists after enrichment — but a count that
    contradicts the list it labels reads as a bug. So the label is derived from
    the row loop's own counter, and the collapsed state claims no number at all.
    """
    body = source[source.index("function renderHistory"):]
    body = body[:body.index("async function callHistory")]
    assert "rows++" in body
    assert '"▾ History (" + rows + ")"' in body
    assert '"▸ History"' in body
    assert "versions.length + \")\"" not in body  # the old, jumping form


def test_expanding_enriches_before_it_paints(source):
    """...and the same reason the count must not jump: the enriched list has to
    land before the open state is drawn, not replace it a moment later."""
    body = source[source.index("histToggle.addEventListener"):]
    body = body[:body.index("let pending")]
    assert body.index("loadHistory(true)") < body.index("renderHistory()")


def test_a_did_not_exist_row_wears_its_own_version_number(source):
    """It is version 1 of the chain in a real store — the creation boundary — so
    it is numbered like every row below it, with "did not exist" as the annotation
    that explains what restoring it does. The dash survives only for a record
    that carried no usable number, since "v0" would invent one the store never
    wrote."""
    body = source[source.index("function renderHistory"):]
    body = body[:body.index("async function callHistory")]
    assert 'v.version >= 1 ? "v" + v.version : "—"' in body


def test_a_did_not_exist_row_keeps_the_version_from_its_record(claude_home,
                                                               tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        # Real shape: the creation boundary is version 1, and the content
        # checkpoint that follows it is version 2.
        delta_record(os.path.basename(f), None, 1, "2026-07-31T07:14:04.850Z"),
    ])
    ghost = [v for v in ann.main(action="history", file=f, enrich=True)["versions"]
             if not v["existed"]][0]
    assert ghost["version"] == 1
    assert ghost["id"] == "s@none1"  # still distinct from a content `s@v1`


def test_an_unknown_timestamp_is_not_rendered_as_1970(source):
    """`_epoch` returns 0 for a stamp it cannot parse, and `new Date(0)` is a
    confident lie about when a file was created."""
    body = source[source.index("function renderHistory"):]
    body = body[:body.index("async function callHistory")]
    assert 'v.mtime ? ago(v.mtime) : "time unknown"' in body
    assert "v.mtime\n" in body or "(v.mtime" in body  # the title is gated too


def test_the_did_not_exist_row_takes_its_time_from_its_own_record(claude_home,
                                                                  tmp_path):
    """Not from a neighbouring version — the two happen to coincide in a real
    session (both come from the same turn), which is exactly what would hide a
    fallback that copied the adjacent row's mtime."""
    ann = _load_annotate()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z"),
    ])
    ghost = [v for v in ann.main(action="history", file=f, enrich=True)["versions"]
             if not v["existed"]][0]
    assert ghost["mtime"] != 1785479788          # not the neighbour's
    assert abs(ghost["mtime"] - 1785477600) < 2  # its own record's 06:00:00Z


def test_an_unparseable_timestamp_becomes_zero_not_a_guess(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "x\n")
    write_version(claude_home, "s", f, "x\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 0, "not a timestamp"),
    ])
    ghost = [v for v in ann.main(action="history", file=f, enrich=True)["versions"]
             if not v["existed"]][0]
    assert ghost["mtime"] == 0.0  # the view renders "time unknown" for this


def test_a_version_identical_to_disk_is_not_clickable(source):
    """Restoring it would write the same bytes back — an action that looks like
    it did nothing, which reads as a broken button."""
    body = source[source.index("function renderHistory"):]
    body = body[:body.index("async function callHistory")]
    assert "if (v.differs) row.onclick" in body


def test_the_comments_log_and_the_stash_coexist(claude_home, tmp_path):
    """Both writers go through the same read-merge-write, so recording a comment
    after a revert must not drop the stash, and vice versa."""
    ann = _load_annotate()
    f = _target(tmp_path, "before\n")
    write_version(claude_home, "s", f, "after\n")
    ann._record(f, [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    ann.main(action="revert", file=f, version_id="s@v1")
    ann._record(f, [{"id": "c2", "content": "there", "createdAt": 2}], [])

    data = json.loads(open(_sidecar(f), encoding="utf-8").read())
    assert sorted(c["id"] for c in data["comments"]) == ["c1", "c2"]
    assert len(data["revertStash"]) == 1
