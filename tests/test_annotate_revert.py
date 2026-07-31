"""Tests for annotate.py's revert actions — the seam between the annotate view
and the Claude-file-history reader (SPEC §34, D194).

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
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
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

    out = ann.main(action="revert", file=f, version_id="s@v1",
                   confirm_unique=True)
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


def test_the_plan_chooses_and_the_revert_only_applies(claude_home, tmp_path):
    """The bridge deliberately no longer picks a target: the plan does, and its id
    is echoed back (I4). Two calls, one decision — so what the user confirmed and
    what lands cannot come apart."""
    ann = _load_annotate()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "older\n", mtime=1000)
    write_version(claude_home, "s", f, "disk\n", mtime=2000)  # the position
    plan = ann.main(action="revert_plan", file=f)
    assert plan["id"] == "s@v1"  # step BACK from the position, not to it
    out = ann.main(action="revert", file=f, version_id=plan["id"])
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

    ann.main(action="revert", file=f, version_id="s@v1", confirm_unique=True)
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
        ann.main(action="revert", file=f, version_id="s@v%d" % i,
                 confirm_unique=True)
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
    out = ann.main(action="revert", file=f, version_id="s@v1",
                   confirm_unique=True)
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

    out = ann.main(action="revert", file=f, version_id="s@v1",
                   confirm_unique=True)
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
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
    ])
    out = ann.main(action="revert", file=f, version_id="s@none0",
                   confirm_unique=True)
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
    # The boot call passes the RESTORED disclosure state, whose default is false,
    # so a fresh boot still never touches a transcript.
    boot = source[source.index("const saved = histState();"):]
    boot = boot[:boot.index("})();")]
    assert "histOpen = !!saved.open" in boot
    assert "loadHistory(histOpen)" in boot
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


def test_the_sheet_knows_whether_a_copy_will_be_kept(source):
    """The hedge was the bug: "a copy is kept unless too large or not text" made
    the one unrecoverable case — content in no checkpoint AND no stash — read
    exactly like the safe one, and the user found out in the past tense. The
    sheet now reads `plan.stash`, the same predicate the write runs."""
    body = source[source.index("async function askRevert"):]
    body = body[:body.index('getElementById("confirmgo").addEventListener')]
    assert "plan.stash === false" in body
    assert "plan.stash_note" in body
    assert "unless it is too large" not in source  # the old hedge is gone


def test_an_unknown_plan_timestamp_is_not_rendered_as_1970_in_the_sheet(source):
    body = source[source.index("async function askRevert"):]
    body = body[:body.index('getElementById("confirmgo").addEventListener')]
    assert "plan.mtime" in body
    assert "unknown time" in body


def test_the_page_does_not_pass_enrich_to_the_plan_or_the_write(source):
    """It used to pass the History panel's own state, so the same button did a
    restore before the panel was expanded and a delete after. Only the `history`
    action takes `enrich` now."""
    for marker in ('action: "revert_plan"', 'action: "revert"'):
        call = source[source.index(marker):]
        call = call[:call.index("}")]
        assert "enrich" not in call, marker
    hist = source[source.index('action: "history"'):]
    assert "enrich" in hist[:hist.index("}")]


def test_the_page_echoes_the_plan_id_and_the_confirm_token(source):
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    call = handler[handler.index('action: "revert"'):]
    call = call[:call.index("}")]
    assert "version_id: plan.id" in call
    assert "confirm_unique" in call


def test_an_unenriched_timeline_still_offers_the_click(claude_home, tmp_path):
    """The boot timeline cannot see the did-not-exist boundary, so its
    `at_earliest` is provisional; believing it disabled the button on a file whose
    remaining step back was a delete. Asserted on the PAYLOAD now rather than on
    the view's conditions — `revert` is the one field the button reads."""
    ann = _load_annotate()
    f = _target(tmp_path, "v1\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1785479788)
    write_transcript(claude_home, "s", str(tmp_path), [
        delta_record(os.path.basename(f), None, 1, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
    ])
    boot = ann.main(action="history", file=f)
    assert boot["enriched"] is False
    assert boot["offer"] is True           # the click must still be possible
    rich = ann.main(action="history", file=f, enrich=True)
    assert rich["revert"].endswith("@none1")


def test_the_button_reads_exactly_one_field(source):
    """Three findings came from the view keeping several conditions in step with
    the plan by hand. `revert` is published only when the action may actually be
    offered, so there is one thing to read and nothing to keep in step."""
    body = source[source.index("function renderHistory"):]
    body = body[:body.index("async function callHistory")]
    assert "revertBtn.disabled = busy || timeline.offer === false;" in body
    assert "timeline.unconfirmed" not in body   # no second condition survives
    assert "timeline.at_earliest" not in body


def test_the_position_is_marked_in_the_list(source):
    """The rule is positional, so a list that does not show where disk sits gives
    no clue why the target is the row it is."""
    body = source[source.index("function renderHistory"):]
    body = body[:body.index("async function callHistory")]
    assert "timeline.position" in body


def test_a_stale_panel_after_a_revert_is_not_reported_as_plain_success(source):
    """The same write-order bug as the failure branch, one path over: the reload
    can fail and write its error into histNote, and the success message then
    erased it. Fixed as a class — loadHistory returns its error to the caller."""
    assert "return out.error;" in source
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    assert "const reloadErr = await loadHistory" in handler
    assert "reportOutcome(outcome, false, reloadErr)" in handler
    # ...and reporting it must not write the composed message back into the carry
    # slot: a dying page would then resurrect a note a later boot had consumed.
    tail = handler[handler.index("reportOutcome(outcome"):]
    assert "carryOutcome" not in tail


def test_a_failed_revert_says_so(source):
    """Caught by clicking Revert in the running app: `renderHistory()` rewrites
    histNote from the timeline's own note, so setting the error text and THEN
    re-rendering wiped it — every failed revert became a silent no-op, the worst
    possible outcome for a destructive action's error path."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    branch = handler[handler.index("if (out.error"):handler.index("// Reload the framed")]
    assert branch.index("renderHistory()") < branch.index("setNote(")


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
        delta_record(os.path.basename(f), None, 1, "2026-07-31T07:14:04.850Z",
                     real_parent_dir=str(tmp_path)),
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
        delta_record(os.path.basename(f), None, 0, "2026-07-31T06:00:00.000Z",
                     real_parent_dir=str(tmp_path)),
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
        delta_record(os.path.basename(f), None, 0, "not a timestamp",
                     real_parent_dir=str(tmp_path)),
    ])
    ghost = [v for v in ann.main(action="history", file=f, enrich=True)["versions"]
             if not v["existed"]][0]
    assert ghost["mtime"] == 0.0  # the view renders "time unknown" for this


def test_a_version_identical_to_disk_is_not_clickable(source):
    """Restoring it would write the same bytes back — an action that looks like
    it did nothing, which reads as a broken button."""
    body = source[source.index("function renderHistory"):]
    body = body[:body.index("async function callHistory")]
    assert 'if (v.differs && timeline.writable !== false) {' in body


def test_the_comments_log_and_the_stash_coexist(claude_home, tmp_path):
    """Both writers go through the same read-merge-write, so recording a comment
    after a revert must not drop the stash, and vice versa."""
    ann = _load_annotate()
    f = _target(tmp_path, "before\n")
    write_version(claude_home, "s", f, "after\n")
    ann._record(f, [{"id": "c1", "content": "hi", "createdAt": 1}], [])
    ann.main(action="revert", file=f, version_id="s@v1", confirm_unique=True)
    ann._record(f, [{"id": "c2", "content": "there", "createdAt": 2}], [])

    data = json.loads(open(_sidecar(f), encoding="utf-8").read())
    assert sorted(c["id"] for c in data["comments"]) == ["c1", "c2"]
    assert len(data["revertStash"]) == 1


# ============================================================ review findings

# --- C2: the stash must be byte-faithful ----------------------------------
# It opened the target in TEXT mode, so universal-newline translation silently
# rewrote CRLF and lone-CR files: b"line one\r\nline two\r\n" stashed as
# 'line one\nline two\n' while the sibling `size` recorded 20 against 18 chars.
# The sidecar contradicted itself and a hand-recovery restored different bytes
# than were lost. This repo ships a `windows/` dir, so CRLF is a live case.

@pytest.mark.parametrize("raw", [
    b"line one\r\nline two\r\n",
    b"old mac\rstyle\r",
    b"mixed\r\nunix\nand\rcr\r\n",
])
def test_the_stash_is_byte_faithful(claude_home, tmp_path, raw):
    ann = _load_annotate()
    f = str(tmp_path / "crlf.txt")
    with open(f, "wb") as h:
        h.write(raw)
    write_version(claude_home, "s", f, "reverted\n")

    out = ann.main(action="revert", file=f, version_id="s@v1",
                   confirm_unique=True)
    assert out["stashed"] is True
    entry = json.loads(open(_sidecar(f), encoding="utf-8").read())["revertStash"][0]
    assert entry["content"].encode("utf-8") == raw
    assert entry["size"] == len(raw)          # and the two agree


def test_the_stashed_size_is_the_byte_count_not_the_character_count(claude_home,
                                                                    tmp_path):
    ann = _load_annotate()
    f = str(tmp_path / "utf8.txt")
    raw = "nö — ünïcode\r\n".encode("utf-8")
    with open(f, "wb") as h:
        h.write(raw)
    write_version(claude_home, "s", f, "x\n")
    ann.main(action="revert", file=f, version_id="s@v1", confirm_unique=True)
    entry = json.loads(open(_sidecar(f), encoding="utf-8").read())["revertStash"][0]
    assert entry["size"] == len(raw)
    assert entry["content"].encode("utf-8") == raw


# --- C3: the confirm sheet must know whether a stash will be kept ---------
# The skip decision used to be computed inside `_revert`, AFTER the click, and
# surfaced only post-hoc. So the sheet showed a permanent hedge ("a copy is kept
# ... unless too large or not text") and the WORST combination — unique_current
# plus a stash skip, i.e. genuinely unrecoverable — read exactly like the safe
# case. os.replace then destroyed the only copy and the user learned about it in
# the past tense, beside "Reverted to v3."

def test_the_plan_reports_whether_a_stash_will_be_kept(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "unsaved\n")
    write_version(claude_home, "s", f, "old\n")
    plan = ann.main(action="revert_plan", file=f)
    assert plan["stash"] is True
    assert plan["stash_note"] == ""


def test_the_plan_reports_a_stash_skip_for_a_large_file(claude_home, tmp_path,
                                                        monkeypatch):
    ann = _load_annotate()
    monkeypatch.setattr(ann, "STASH_BYTE_CAP", 8)
    f = _target(tmp_path, "far too much content to stash\n")
    write_version(claude_home, "s", f, "small\n")
    plan = ann.main(action="revert_plan", file=f)
    assert plan["stash"] is False
    assert "too large" in plan["stash_note"]
    # ...and this is the unrecoverable combination the sheet must escalate.
    assert plan["unique_current"] is True


def test_the_plan_reports_a_stash_skip_for_binary(claude_home, tmp_path):
    ann = _load_annotate()
    f = str(tmp_path / "blob.bin")
    with open(f, "wb") as h:
        h.write(b"\xff\xfe\x00current")
    d = os.path.join(str(claude_home), "file-history", "s")
    os.makedirs(d)
    with open(os.path.join(d, path_hash(f) + "@v1"), "wb") as h:
        h.write(b"\xff\xfe\x00wanted")
    plan = ann.main(action="revert_plan", file=f)
    assert plan["stash"] is False
    assert "UTF-8" in plan["stash_note"]


@skip_root
def test_the_plan_reports_a_stash_skip_for_an_unwritable_sidecar(claude_home,
                                                                 tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "unsaved\n")
    write_version(claude_home, "s", f, "old\n")
    sidecar = tmp_path / "page.html.json"
    sidecar.write_text("{}")
    os.chmod(sidecar, 0o444)
    try:
        plan = ann.main(action="revert_plan", file=f)
        assert plan["stash"] is False
        assert "read-only" in plan["stash_note"]
    finally:
        os.chmod(sidecar, 0o644)


def test_the_plan_predicate_agrees_with_what_the_revert_actually_does(
        claude_home, tmp_path, monkeypatch):
    """The two must be one computation, or the sheet promises something the write
    does not deliver."""
    ann = _load_annotate()
    monkeypatch.setattr(ann, "STASH_BYTE_CAP", 8)
    f = _target(tmp_path, "far too much content to stash\n")
    write_version(claude_home, "s", f, "small\n")
    plan = ann.main(action="revert_plan", file=f)
    out = ann.main(action="revert", file=f, version_id=plan["id"],
                   confirm_unique=True)
    assert out["stashed"] == plan["stash"]
    assert out["stash_note"] == plan["stash_note"]


# --- I4: the bridge must not write on an unconfirmed plan -----------------
# The confirm gate lived only in the page; `_revert` never consulted
# `unique_current`, so `action="revert"` with `version_id=None` performed the
# destructive write with no confirmation token and no plan echo. The only guard
# was a source grep over today's template, which pins the page and not the
# bridge — so any future second caller got an unguarded file-destroying entry
# point.

def test_the_bridge_requires_the_plans_version_id(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "keep\n")
    write_version(claude_home, "s", f, "other\n")
    out = ann.main(action="revert", file=f)
    assert "error" in out
    assert "version_id" in out["error"]
    with open(f, encoding="utf-8") as h:
        assert h.read() == "keep\n"


def test_the_bridge_refuses_an_unconfirmed_unique_current_revert(claude_home,
                                                                 tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "in no checkpoint\n")
    write_version(claude_home, "s", f, "old\n")
    plan = ann.main(action="revert_plan", file=f)
    assert plan["unique_current"] is True

    out = ann.main(action="revert", file=f, version_id=plan["id"])
    assert "error" in out
    assert "confirm_unique" in out["error"]
    with open(f, encoding="utf-8") as h:
        assert h.read() == "in no checkpoint\n"   # untouched

    out = ann.main(action="revert", file=f, version_id=plan["id"],
                   confirm_unique=True)
    assert out["ok"] is True
    with open(f, encoding="utf-8") as h:
        assert h.read() == "old\n"


def test_a_revert_that_loses_nothing_needs_no_extra_token(claude_home, tmp_path):
    """The token gates the DESTRUCTIVE case only. Stepping back from a position
    that is itself a checkpoint loses nothing unrecorded, so demanding a token
    there would train the user to always pass it."""
    ann = _load_annotate()
    f = _target(tmp_path, "v2\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "v2\n", mtime=2000)
    plan = ann.main(action="revert_plan", file=f)
    assert plan["unique_current"] is False
    out = ann.main(action="revert", file=f, version_id=plan["id"])
    assert out["ok"] is True


def test_a_stale_version_id_is_refused_after_the_file_moves_on(claude_home,
                                                               tmp_path):
    """The echo is also a freshness check: a plan built against one disk state
    and applied against another is exactly how a user confirms one diff and gets
    another."""
    ann = _load_annotate()
    f = _target(tmp_path, "v2\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "v2\n", mtime=2000)
    out = ann.main(action="revert", file=f, version_id="s@v99",
                   confirm_unique=True)
    assert "error" in out
    with open(f, encoding="utf-8") as h:
        assert h.read() == "v2\n"


# --- I5: a stash failure must not be reported as a false claim -----------

@skip_root
def test_an_unreadable_target_is_not_reported_as_not_being_text(claude_home,
                                                                tmp_path):
    """`except (OSError, UnicodeDecodeError)` reported an EACCES on the target as
    "previous content is not UTF-8 text", and a getsize failure as "nothing on
    disk to stash" — which reads as "the file is absent". Both are cases the user
    could actually fix, described as something else."""
    ann = _load_annotate()
    f = _target(tmp_path, "secret\n")
    write_version(claude_home, "s", f, "old\n")
    os.chmod(f, 0o000)
    try:
        ok, note = ann._stash_plan(f)[:2]
        assert ok is False
        assert "UTF-8" not in note
        assert "13" in note or "Permission" in note
    finally:
        os.chmod(f, 0o644)


def test_an_absent_target_says_absent(claude_home, tmp_path):
    ann = _load_annotate()
    f = str(tmp_path / "gone.txt")
    write_version(claude_home, "s", f, "old\n")
    ok, note = ann._stash_plan(f)[:2]
    assert ok is False
    assert "nothing on disk" in note


# --- M3: an import-time bug is not a missing sibling ---------------------

def test_a_broken_helper_reports_its_own_error_not_a_missing_folder(claude_home,
                                                                    tmp_path,
                                                                    monkeypatch):
    """`except Exception` turned a SyntaxError inside file_history.py into
    "helper is not available", which reads as "you copied the folder without
    shared/" and sends the reader to the wrong place entirely."""
    import builtins

    ann = _load_annotate()
    f = _target(tmp_path)
    real_import = builtins.__import__

    def broken(name, *a, **kw):
        if name == "file_history":
            raise ValueError("boom inside the module body")
        return real_import(name, *a, **kw)

    import sys
    saved = sys.modules.pop("file_history", None)
    monkeypatch.setattr(builtins, "__import__", broken)
    try:
        out = ann.main(action="history", file=f)
        assert "error" in out
        assert "ValueError" in out["error"]
        assert "not available" not in out["error"]
    finally:
        if saved is not None:
            sys.modules["file_history"] = saved


# ==================================================== the panel, driven for real
# `rowMarks` and the sessionStorage persistence are pure enough to run under node
# straight out of the shipping template — the `_js_block` approach
# test_log_studio_detail.py and test_graph_canvas.py use, for the same reason: a
# copy of the logic here would keep passing after the real code regressed.

def _js_block(src, header):
    """`header` plus its brace-balanced body, verbatim from the template."""
    start = src.index(header)
    open_brace = src.index("{", start)
    depth = 0
    for i in range(open_brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _run(body, tmp_path, prelude=""):
    import json as _json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the template's JS")
    harness = tmp_path / "harness.mjs"
    harness.write_text(prelude + body, encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return _json.loads(out.stdout)


# --- item 1: two dot states, target expressed as row treatment ------------

def _marks_prelude(source):
    return _js_block(source, "function rowMarks(v, positionId, targetId)") + "\n"


def test_the_dot_answers_one_question_and_has_two_states(source, tmp_path):
    """Three glyphs over three colours read as noise, and at 11px `◉` and `●` are
    barely distinguishable — while meaning unrelated things. The dot now says only
    "you are here"; the revert target is row treatment."""
    got = _run("""
      const rows = [
        { id: "s@v3", differs: false },
        { id: "s@v2", differs: true },
        { id: "s@v1", differs: true },
      ].map((v) => rowMarks(v, "s@v3", "s@v2"));
      console.log(JSON.stringify(rows));
    """, tmp_path, _marks_prelude(source))
    assert [r["dot"] for r in got] == ["●", "○", "○"]  # two states only
    # The target is NOT a third glyph...
    assert got[1]["dot"] == "○"
    # ...it is the row.
    assert "next" in got[1]["cls"]
    assert "next" not in got[0]["cls"] and "next" not in got[2]["cls"]
    assert "here" in got[0]["cls"] and "here" not in got[1]["cls"]


def test_no_third_dot_glyph_is_emitted(source):
    """Scoped to the code rather than the file: the comment above `rowMarks` names
    the rejected glyph on purpose, and that is the one place it should appear."""
    marks = _js_block(source, "function rowMarks(v, positionId, targetId)")
    assert "◉" not in marks
    # ...and `rowMarks` is the only source of a dot's text.
    assert source.count("dot.textContent") == 1
    assert "dot.textContent = marks.dot;" in source


def test_the_position_row_is_not_dimmed_as_an_inert_duplicate(source, tmp_path):
    """The current position always has `differs: false`, so the `.same` dimming
    would grey out the one row the marker exists to emphasise."""
    got = _run("""
      console.log(JSON.stringify(rowMarks({ id: "a", differs: false }, "a", "b")));
    """, tmp_path, _marks_prelude(source))
    assert "same" in got["cls"] and "here" in got["cls"]
    # ...which the stylesheet exempts rather than the JS special-casing it.
    assert ".hrow.same:not(.here) { opacity: 0.5; }" in source


def test_the_target_stripe_reserves_its_width_on_every_row(source):
    """Otherwise marking the target reflows the whole list by 2px."""
    assert "border-left: 2px solid transparent" in source
    assert ".hrow.next { border-left-color: var(--accent);" in source


def test_nothing_to_revert_to_is_simply_no_accent_row(source, tmp_path):
    """The at_earliest state needs no fourth marker: `revert` is null, so no row
    is striped, and "no accent anywhere" already reads as "nothing to go back
    to"."""
    got = _run("""
      const rows = [{ id: "s@v1", differs: false }]
        .map((v) => rowMarks(v, "s@v1", null));
      console.log(JSON.stringify(rows));
    """, tmp_path, _marks_prelude(source))
    assert "next" not in got[0]["cls"]
    assert "here" in got[0]["cls"]


# --- item 2: the acknowledgement checkbox is gone -------------------------

def test_the_confirm_button_is_never_disabled(source):
    """Removed as friction by the owner. This is a real reduction in gating, so
    the assertions below are about what MUST remain."""
    assert "confirmack" not in source
    assert "go.disabled" not in source
    assert "ack.checked" not in source


def test_the_irreversible_warning_is_what_remains_and_carries_weight(source):
    """It is now the only thing between a click and unrecoverable loss."""
    body = source[source.index("async function askRevert"):]
    body = body[:body.index('getElementById("confirmgo").addEventListener')]
    assert "cannot be recovered from anywhere" in body
    assert 'warn.classList.toggle("hard", irreversible)' in body
    assert "#confirmwarn.hard" in source
    # ...and the button says what it does, since its label is the last word.
    assert "Overwrite permanently" in body and "Delete permanently" in body


def test_the_bridge_token_is_unrelated_to_any_ui_gate(source):
    """`confirm_unique` is the BRIDGE's guard (I4) and is derived from the plan,
    never from a widget — removing the checkbox must not have touched it."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    call = handler[handler.index('action: "revert"'):]
    call = call[:call.index("}")]
    assert "confirm_unique: !!plan.unique_current" in call


# --- item 3: the panel survives the post-revert reload -------------------

def _persist_prelude(source):
    return (
        "const store = {};\n"
        "globalThis.sessionStorage = {\n"
        "  getItem: (k) => (k in store ? store[k] : null),\n"
        "  setItem: (k, v) => { store[k] = String(v); },\n"
        "};\n"
        "const file = '/tmp/target.md';\n"
        'const HIST_KEY = "fusedAnnotateHist:" + file;\n'
        "let histOpen = false;\n"
        + _js_block(source, "function histState()") + "\n"
        + _js_block(source, "function saveHistState(patch)") + "\n"
        + _js_block(source, "function carryOutcome(text)") + "\n"
        + _js_block(source, "function takeCarriedOutcome()") + "\n"
        + _js_block(source, "function dropCarriedOutcome()") + "\n"
    )


def test_the_disclosure_state_and_outcome_survive_a_reload(source, tmp_path):
    """The behaviour the user asked for: after a revert the shell reboots the
    whole preview off its fs-event watch, and the panel used to come back
    collapsed with the outcome gone — so a successful revert looked like nothing
    had happened."""
    got = _run("""
      // ...the user expands the panel and reverts
      saveHistState({ open: true });
      histOpen = true;
      carryOutcome("Reverted to v2.");
      // ...the shell tears the page down and boots it again: same file, fresh
      // module scope, only sessionStorage in common.
      const booted = histState();
      const carried = takeCarriedOutcome();
      console.log(JSON.stringify({
        open: !!booted.open,
        carried,
        again: takeCarriedOutcome(),   // read-and-clear
        stillOpen: !!histState().open, // ...but the disclosure state is sticky
      }));
    """, tmp_path, _persist_prelude(source))
    assert got["open"] is True
    assert got["carried"] == {"text": "Reverted to v2.", "error": False}
    assert got["again"] is None
    assert got["stillOpen"] is True


def test_a_failed_revert_is_not_carried_across_a_reload(source, tmp_path):
    """Only a SUCCESSFUL revert is carried. A failure changed nothing on disk, so
    there is no fs event, no reload to bridge, and no reason to greet a later
    visit to the file with a stale error — which is what persisting it did."""
    got = _run("""
      const out = {};
      out.writers = 1;                 // carryOutcome is the only one
      carryOutcome("Reverted to v2.");
      out.afterTake = takeCarriedOutcome();
      out.spent = takeCarriedOutcome();
      carryOutcome("Reverted to v3.");
      dropCarriedOutcome();            // displayed in-page => spent
      out.afterDrop = takeCarriedOutcome();
      console.log(JSON.stringify(out));
    """, tmp_path, _persist_prelude(source))
    assert got["afterTake"]["text"] == "Reverted to v2."
    assert got["spent"] is None          # read-and-clear
    assert got["afterDrop"] is None      # in-page display also spends it
    # ...and the failure branch does not write to the slot at all.
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    branch = handler[handler.index("if (out.error"):handler.index("// Reload the framed")]
    assert "carryOutcome" not in branch and "saveHistState" not in branch


def test_a_hostile_sessionStorage_never_breaks_the_panel(source, tmp_path):
    """Private mode throws on setItem, and a corrupt value must not take the
    panel down with it — persistence is a nicety, never a dependency."""
    got = _run("""
      const out = {};
      globalThis.sessionStorage = {
        getItem: () => "{not json",
        setItem: () => { throw new Error("quota"); },
      };
      out.corrupt = histState();
      saveHistState({ open: true });   // must not throw
      out.survived = true;
      console.log(JSON.stringify(out));
    """, tmp_path, _persist_prelude(source))
    assert got["corrupt"] == {}
    assert got["survived"] is True


def test_the_outcome_is_persisted_before_the_refresh_can_race_it(source):
    """The fs-event reload is already in flight by then: written first the outcome
    survives, written after it is lost exactly when the reload is fastest."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    body = handler[handler.index("const outcome ="):]
    assert body.index("carryOutcome(") < body.index("await loadHistory")


def test_an_expanded_restore_is_enriched(source):
    """An expanded panel must be enriched or its row count disagrees with the list
    it labels — so the boot call passes the restored disclosure state, not a
    literal false."""
    boot = source[source.index("const saved = histState();"):]
    boot = boot[:boot.index("})();")]
    assert "histOpen = !!saved.open" in boot
    assert "await loadHistory(histOpen)" in boot


def test_the_carried_outcome_is_applied_after_the_refresh(source):
    """loadHistory rewrites the note element from the timeline's own note, so a
    carried message applied first would be wiped — and it goes through the shared
    reporter, so a failed refresh is not hidden behind a carried success."""
    boot = source[source.index("const saved = histState();"):]
    boot = boot[:boot.index("})();")]
    assert boot.index("await loadHistory(histOpen)") < boot.index("if (carried)")
    assert "reportOutcome(carried.text, false, reloadErr)" in boot


def test_the_disclosure_state_is_not_written_to_the_url(source):
    """`comments` lives in the URL precisely so a review can be SHARED; whether a
    disclosure widget is open is a workspace habit (the D185 argument for pane
    geometry), and a transient outcome message in a bookmark would be a lie the
    moment it was opened."""
    for key in ("hist", "histOpen", "revertNote"):
        assert 'fused.params.set("' + key not in source


# ============================================== review round 3 (Bugbot findings)

# --- B1: a read-only target must not reach a destructive confirm ----------

def test_the_plan_refuses_an_unwritable_target_with_its_reason(claude_home,
                                                               ro_mount):
    """Strengthened: the plan does not hand back `ok: True` with a false flag for
    the caller to remember to check — it REFUSES, because a plan is an offer and
    this action cannot be offered. The bool alone was also a dead end: a read-only
    mount, a chmod'd file, a symlink and an unwritable directory are four
    different things for the user to do."""
    ann = _load_annotate()
    write_version(claude_home, "s", ro_mount, "wanted\n")
    plan = ann.main(action="revert_plan", file=ro_mount)
    assert plan["ok"] is False
    assert "read-only mount" in plan["error"]
    assert ann.main(action="history", file=ro_mount)["writable_reason"]


@skip_root
def test_the_reason_distinguishes_a_chmod_from_a_mount(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "keep\n")
    write_version(claude_home, "s", f, "other\n")
    os.chmod(f, 0o444)
    try:
        plan = ann.main(action="revert_plan", file=f)
        assert plan["ok"] is False
        assert "the file itself is read-only" in plan["error"]
        assert "mount" not in plan["error"]
        assert "the file itself is read-only" in ann.main(
            action="history", file=f)["writable_reason"]
    finally:
        os.chmod(f, 0o644)


def test_ask_revert_refuses_an_unwritable_target_before_the_sheet_opens(source):
    """`writable` is a FIELD on a SUCCESSFUL plan, not an error, so nothing in the
    old bail conditions caught it: the sheet opened, the user confirmed a
    destructive act, and it could only fail server-side. Checked at the plan level
    because that closes the class rather than one entry point — the same reasoning
    as making the bridge the authority for the confirm token."""
    body = source[source.index("async function askRevert"):]
    body = body[:body.index('getElementById("confirmgo").addEventListener')]
    assert "plan.writable === false" in body
    assert "plan.writable_reason" in body
    # ...and it happens BEFORE the sheet is ever shown.
    assert body.index("plan.writable === false") < body.index('classList.add("open")')


def test_rows_are_inert_on_an_unwritable_target(source, tmp_path):
    """The other layer. The main button already went dead on `writable === false`
    while every differing row stayed clickable."""
    body = source[source.index("function renderHistory"):]
    body = body[:body.index("async function callHistory")]
    assert "timeline.writable !== false" in body
    assert 'row.style.cursor = "default"' in body


def test_the_note_says_why_the_button_is_dead(source):
    """`offer_reason` is the same sentence the plan would refuse with, so no press
    is needed to discover why a disabled button is disabled."""
    body = source[source.index("function renderHistory"):]
    body = body[:body.index("async function callHistory")]
    assert "timeline.offer_reason" in body


# --- B2: the toggle label follows the fetch's outcome ---------------------

def test_the_toggle_flips_only_after_a_successful_fetch(source):
    """Flipping first and awaiting second left an open "▾" label above an empty
    list whenever the enrich failed. Third instance of the optimistic-then-await
    trap in this panel."""
    body = source[source.index("histToggle.addEventListener"):]
    body = body[:body.index("let pending")]
    assert body.index("await loadHistory(true)") < body.index("histOpen = !histOpen")
    assert "if (await loadHistory(true)) return;" in body


def test_a_failed_fetch_repaints_from_the_last_good_timeline(source):
    """...which is what makes the label resync rather than needing the toggle
    handler to undo itself."""
    body = source[source.index("async function loadHistory(enrich)"):]
    body = body[:body.index("histToggle.addEventListener")]
    assert "if (timeline) renderHistory();" in body
    assert body.index("renderHistory()") < body.index("setNote(")


# --- B3: unconfirmed terminality is a STATE, not a click-refuse loop -----

def test_an_unreadable_version_with_no_target_is_reported_as_unconfirmed(
        claude_home, tmp_path, monkeypatch):
    """The refusal itself is CORRECT and stays: with no target, a version we
    failed to read is exactly a candidate for the older differing entry we did not
    find, so claiming "you are at the earliest checkpoint" would assert
    terminality from a scan with a hole in it. What was wrong was the wording (it
    blamed an unidentifiable last change, when the problem is unprovable
    terminality) and the fact that the only way to discover it was to click."""
    ann = _load_annotate()
    f = _target(tmp_path, "v1\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "unreadable\n", mtime=500)
    real_open = open

    def flaky(path, *a, **kw):
        if "@v2" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    tl = ann.main(action="history", file=f, enrich=True)
    assert tl["unconfirmed"] is True
    assert tl["at_earliest"] is False   # cannot be claimed
    assert tl["revert"] is None
    assert "cannot be confirmed as the earliest" in tl["note"]

    plan = ann.main(action="revert_plan", file=f)
    assert plan["ok"] is False
    assert plan["unconfirmed"] is True
    assert "earliest checkpoint" in plan["error"]
    # ...and NOT the wrong sentence it used to give.
    assert "last change cannot be identified" not in plan["error"]


def test_a_skip_with_a_target_keeps_the_other_wording(claude_home, tmp_path,
                                                      monkeypatch):
    """The distinction the docstring describes survives: with a target, the
    problem really is that the last change cannot be identified."""
    ann = _load_annotate()
    f = _target(tmp_path, "disk\n")
    write_version(claude_home, "s", f, "older\n", mtime=1000)
    write_version(claude_home, "s", f, "newest\n", mtime=2000)
    real_open = open

    def flaky(path, *a, **kw):
        if "@v2" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    plan = ann.main(action="revert_plan", file=f)
    assert plan["ok"] is False
    assert plan["unconfirmed"] is False
    assert "last change cannot be identified" in plan["error"]


def test_the_unconfirmed_state_withholds_the_target(claude_home, tmp_path,
                                                    monkeypatch):
    """A stable refusal must not be a loop the user discovers by pressing — so it
    is withheld from the payload the button reads, not merely special-cased in the
    view."""
    ann = _load_annotate()
    f = _target(tmp_path, "v1\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "unreadable\n", mtime=500)
    real_open = open

    def flaky(path, *a, **kw):
        if "@v2" in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    tl = ann.main(action="history", file=f, enrich=True)
    assert tl["unconfirmed"] is True
    assert tl["revert"] is None                      # no row to stripe
    assert tl["offer"] is False                      # nothing to press
    assert "earliest" in tl["offer_reason"]          # ...and it says why


def test_a_refusal_settles_the_panel_instead_of_looping(source):
    """...and any refusal that arrives anyway refreshes the panel into its real
    state, so the second press cannot produce the same dead end."""
    body = source[source.index("async function askRevert"):]
    body = body[:body.index("// `writable` is a FIELD")]
    assert "await loadHistory(true)" in body
    assert "reportOutcome(plan.error" in body


def test_the_two_terminal_states_are_never_both_claimed(claude_home, tmp_path,
                                                        monkeypatch):
    """They are different sentences — one a fact, one an admission — and asserting
    both would be incoherent."""
    fh_mod = _load_annotate()
    f = _target(tmp_path, "v1\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    tl = fh_mod.main(action="history", file=f, enrich=True)
    assert (tl["at_earliest"], tl["unconfirmed"]) == (True, False)


# ======================================== review round 5: the guard-layer class
# B1, N1 and N2 all had one root cause — a guard living BELOW the layer that
# decides whether to offer the action — so these pin the single authority
# (`file_history.offer_reason`) that every layer now consults, rather than the
# three point fixes.

# --- N1: a blocking skip WITH a live target -------------------------------

def _blocked_chain(claude_home, tmp_path, monkeypatch, unreadable="@v3"):
    """disk == v3; v1/v2 older. Making one version unreadable leaves a live
    target while `revert_plan` will refuse the automatic choice."""
    f = _target(tmp_path, "v3\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "v2\n", mtime=2000)
    write_version(claude_home, "s", f, "v3\n", mtime=3000)
    real_open = open

    def flaky(path, *a, **kw):
        if unreadable in str(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky)
    return f


def test_a_blocking_skip_with_a_live_target_offers_nothing(claude_home, tmp_path,
                                                            monkeypatch):
    """The other half of the unconfirmed fix, and the identical defect: with a
    target still alive the panel kept publishing it, struck that row as "this is
    what Revert does", and left the button enabled over a plan that always
    refused — the same click-refuse loop, surviving every reload."""
    ann = _load_annotate()
    f = _blocked_chain(claude_home, tmp_path, monkeypatch)

    tl = ann.main(action="history", file=f, enrich=True)
    assert tl["blocking"]
    assert tl["unconfirmed"] is False    # a target DOES exist...
    assert tl["offer"] is False          # ...and it is still not offerable
    assert tl["revert"] is None          # so no row is struck as the target
    assert "last change cannot be identified" in tl["offer_reason"]

    plan = ann.main(action="revert_plan", file=f)
    assert plan["ok"] is False
    # The panel's sentence and the plan's refusal are the same answer.
    assert tl["offer_reason"].split(" —")[0] in plan["error"]


def test_an_explicit_id_still_works_when_the_automatic_choice_is_blocked(
        claude_home, tmp_path, monkeypatch):
    """The asymmetry is deliberate and is stated in `offer_reason`: "revert the
    last change" is a question this module answers, and can decline to answer from
    an incomplete scan; "revert to THIS version" is the user naming the target, so
    there is nothing left to guess."""
    ann = _load_annotate()
    f = _blocked_chain(claude_home, tmp_path, monkeypatch)

    plan = ann.main(action="revert_plan", file=f, version_id="s@v1")
    assert plan["ok"] is True
    assert plan["version"] == 1
    # v3 being unreadable means disk matches no READABLE checkpoint, so the plan
    # correctly reports unique_current and the bridge correctly demands the token
    # — the explicit path is unblocked, not ungated.
    assert plan["unique_current"] is True
    out = ann.main(action="revert", file=f, version_id="s@v1",
                   confirm_unique=True)
    assert out["ok"] is True
    with open(f, encoding="utf-8") as h:
        assert h.read() == "v1\n"


def test_the_panel_and_the_plan_cannot_disagree_about_availability(claude_home,
                                                                   tmp_path,
                                                                   monkeypatch):
    """The property the single authority buys: whenever the panel offers the
    automatic revert the plan accepts it, and whenever it does not the plan
    refuses. Swept over the states that used to disagree."""
    ann = _load_annotate()
    cases = []

    # (1) ordinary: offerable
    f1 = _target(tmp_path, "v2\n", name="a.txt")
    write_version(claude_home, "s", f1, "v1\n", mtime=1000)
    write_version(claude_home, "s", f1, "v2\n", mtime=2000)
    cases.append(f1)
    # (2) terminal
    f2 = _target(tmp_path, "v1\n", name="b.txt")
    write_version(claude_home, "s", f2, "v1\n", mtime=1000)
    cases.append(f2)
    # (3) no versions at all
    cases.append(_target(tmp_path, "x\n", name="c.txt"))

    for f in cases:
        tl = ann.main(action="history", file=f, enrich=True)
        plan = ann.main(action="revert_plan", file=f)
        assert tl["offer"] == bool(plan.get("ok")), f


# --- N2: a symlink must not reach the sheet or the stash -----------------

def test_a_symlink_is_refused_at_the_plan_layer(claude_home, tmp_path):
    """`apply_revert` refused symlinks correctly and refused them ALONE, one layer
    below the decision to offer — so the sheet opened on a target that could not
    succeed and the stash ran first."""
    ann = _load_annotate()
    real = tmp_path / "real.txt"
    real.write_text("real content\n")
    link = str(tmp_path / "link.txt")
    os.symlink(str(real), link)
    write_version(claude_home, "s", link, "wanted\n")

    tl = ann.main(action="history", file=link, enrich=True)
    assert tl["writable"] is False
    assert "symlink" in tl["writable_reason"]
    assert tl["offer"] is False
    plan = ann.main(action="revert_plan", file=link)
    assert plan["ok"] is False
    assert "symlink" in plan["error"]


def test_a_failed_symlink_revert_does_not_write_the_stash(claude_home, tmp_path):
    """The damage the missing plan-layer check actually did: `_stash` read THROUGH
    the link, so a revert that then raised had already put the WRONG file's content
    into the sidecar."""
    ann = _load_annotate()
    real = tmp_path / "real.txt"
    real.write_text("real content\n")
    link = str(tmp_path / "link.txt")
    os.symlink(str(real), link)
    write_version(claude_home, "s", link, "wanted\n")

    out = ann.main(action="revert", file=link, version_id="s@v1",
                   confirm_unique=True)
    assert "error" in out
    assert "symlink" in out["error"]
    assert not os.path.exists(link + ".json")        # no sidecar at all
    assert not os.path.exists(str(real) + ".json")
    assert os.path.islink(link)
    with open(str(real), encoding="utf-8") as h:
        assert h.read() == "real content\n"          # untouched


def test_the_symlink_reason_is_not_the_read_only_wording(claude_home, tmp_path):
    """A user can act on "this is a symlink"; "read-only" would send them to
    chmod, which would not help."""
    ann = _load_annotate()
    real = tmp_path / "real.txt"
    real.write_text("x\n")
    link = str(tmp_path / "link.txt")
    os.symlink(str(real), link)
    write_version(claude_home, "s", link, "wanted\n")
    reason = ann.main(action="history", file=link)["writable_reason"]
    assert "symlink" in reason
    assert "read-only" not in reason


@skip_root
def test_no_unwritable_target_reaches_the_stash(claude_home, tmp_path):
    """Generalized past the symlink: `_stash` runs BEFORE the write by design, so
    ANY target the write will reject has to be refused before it — a chmod'd file
    used to stash and then raise."""
    ann = _load_annotate()
    f = _target(tmp_path, "keep\n")
    write_version(claude_home, "s", f, "other\n")
    os.chmod(f, 0o444)
    try:
        out = ann.main(action="revert", file=f, version_id="s@v1",
                       confirm_unique=True)
        assert "error" in out
        assert not os.path.exists(_sidecar(f))
        with open(f, encoding="utf-8") as h:
            assert h.read() == "keep\n"
    finally:
        os.chmod(f, 0o644)


def test_a_directory_target_is_refused_the_same_way(claude_home, tmp_path):
    """Folded into the same answer rather than staying a separate raise in
    apply_revert."""
    ann = _load_annotate()
    d = tmp_path / "adir"
    d.mkdir()
    write_version(claude_home, "s", str(d), "payload\n")
    tl = ann.main(action="history", file=str(d), enrich=True)
    assert tl["offer"] is False
    assert "directory" in tl["writable_reason"]
    assert "error" in ann.main(action="revert", file=str(d), version_id="s@v1")
    assert os.path.isdir(str(d))
