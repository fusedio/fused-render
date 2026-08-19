"""Tests for annotate.py's revert actions — the seam between the annotate view
and the Claude-file-history reader (SPEC §34, D194).

`file_history.py` owns the store and the write; annotate.py owns the one thing
it cannot: turning every failure into an `{"error": ...}` DICT (a raised
exception would become the red traceback overlay, which is not an acceptable
answer to "this file has no history").

The sharpest hazard in the feature: current on-disk content is frequently in NO
checkpoint, so a restore can vaporize work that exists nowhere else. The UI's
confirm step (driven by `unique_current`) is the whole defence — no copy is
kept anywhere (D359 removed the sidecar stash).
"""
import importlib.util
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


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # The mounts dir hangs off home_dir() — pin FUSED_RENDER_HOME so these
    # tests never touch a real ~/.fused-render.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _target(tmp_path, content="a\nb\nc\n", name="page.html"):
    f = tmp_path / name
    f.write_text(content)
    return str(f)


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

def test_revert_restores_the_previous_content(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "unsaved work\n")
    write_version(claude_home, "s", f, "wanted\n")

    out = ann.main(action="revert", file=f, version_id="s@v1",
                   confirm_unique=True)
    assert out["ok"] is True and out["action"] == "restore"
    with open(f, encoding="utf-8") as h:
        assert h.read() == "wanted\n"


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


def test_a_large_file_is_reverted(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "x" * 2_000_000 + "\n")
    write_version(claude_home, "s", f, "small\n")
    out = ann.main(action="revert", file=f, version_id="s@v1",
                   confirm_unique=True)
    assert out["ok"] is True
    with open(f, encoding="utf-8") as h:
        assert h.read() == "small\n"


def test_binary_content_is_still_reverted(claude_home, tmp_path):
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
    assert out["ok"] is True
    with open(f, "rb") as h:
        assert h.read() == b"\xff\xfe\x00wanted"


def test_a_revert_across_the_creation_boundary_deletes_the_file(claude_home,
                                                                tmp_path):
    """Reverting across a did-not-exist boundary removes the file outright."""
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
    assert not os.path.exists(f)


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
    assert "current.size" in body and "target.size" in body  # byte counts
    assert "plan.added" in body            # and the line delta


def test_the_sheet_knows_no_copy_is_kept(source):
    """D359 removed the sidecar stash, so the warning is driven by
    `plan.unique_current` alone and must not hedge about a copy being kept —
    there is none, anywhere."""
    assert "plan.stash" not in source
    assert "revertStash" not in source
    body = source[source.index("async function askRevert"):]
    body = body[:body.index('getElementById("confirmgo").addEventListener')]
    assert "warn.hidden = !plan.unique_current" in body
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


# ============================================================ review findings

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

def test_the_confirm_button_is_never_gated_only_ever_in_flight(source):
    """Removed as friction by the owner. This is a real reduction in gating, so
    the assertions below are about what MUST remain.

    The button IS disabled for one window — between the click and the call
    returning, while the sheet stays up saying "Reverting…" — and that is the
    opposite of a gate: it is the click having been accepted. So the rule is not
    "never disabled" but "never disabled by a widget's state": no acknowledgement
    control, and the only writes to `disabled` sit inside the click handler."""
    assert "confirmack" not in source
    assert "ack.checked" not in source
    ask = source[source.index("async function askRevert"):]
    assert "go.disabled" not in ask[:ask.index(
        'getElementById("confirmgo").addEventListener')]
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    assert handler.count("go.disabled = no.disabled = ") == 2  # in flight, then not


def test_the_irreversible_warning_is_what_remains_and_carries_weight(source):
    """It is now the only thing between a click and unrecoverable loss."""
    body = source[source.index("async function askRevert"):]
    body = body[:body.index('getElementById("confirmgo").addEventListener')]
    # The phrase is line-wrapped across JS string concatenation, so it is
    # asserted on the source with the quoting stripped.
    joined = body.replace('" +\n        "', "").replace('"', "")
    assert "cannot be recovered from anywhere" in joined
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


# --- N2: a symlink must not reach the sheet -------------------------------

def test_a_symlink_is_refused_at_the_plan_layer(claude_home, tmp_path):
    """`apply_revert` refused symlinks correctly and refused them ALONE, one layer
    below the decision to offer — so the sheet opened on a target that could not
    succeed."""
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


def test_a_failed_symlink_revert_touches_nothing(claude_home, tmp_path):
    """A symlink revert is refused at the plan layer, and the refusal must leave
    the link and the real file exactly as they were."""
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


# ================================================== the diff in the confirm sheet
# The sheet used to show only aggregates — bytes now, bytes after, `+N / −M` —
# which answer how MUCH changes and never WHAT. On the one destructive action in
# this view the second is the question being confirmed.

def test_the_bridge_passes_the_plans_diff_through_untouched(claude_home,
                                                           tmp_path):
    """The bridge is a bare pass-through, so the diff reaches the sheet exactly
    as file_history computed it — no second scan, no second framing to keep in
    step with `_delta`'s."""
    ann = _load_annotate()
    f = _target(tmp_path, "a\nb\n")
    write_version(claude_home, "s", f, "a\n")
    plan = ann.main(action="revert_plan", file=f)
    assert plan["diff"]["reason"] == ""
    assert "-b" in plan["diff"]["lines"]
    assert plan["diff"]["changed"] == 1


def _const_line(src, name):
    """A module-level `const NAME = ...;` line, verbatim — so a threshold the test
    reasons about is the shipping one rather than a copy that can drift."""
    for line in src.splitlines():
        if line.strip().startswith("const %s =" % name):
            return line.strip()
    raise AssertionError(f"no `const {name}` in the template")


#: A DOM just large enough to run the template's own sheet/stage code verbatim —
#: nodes by id, a class list, text-only children. A copy of the logic here would
#: keep passing after the shipping code regressed, which is the whole point.
_DOM_STUB = (
    "class El {\n"
    "  constructor(id) {\n"
    "    this.id = id; this.children = []; this.hidden = false;\n"
    "    this.className = ''; this.disabled = false; this.style = {};\n"
    "    this._text = '';\n"
    "    const set = new Set();\n"
    "    this.classList = {\n"
    "      add: (c) => set.add(c), remove: (c) => set.delete(c),\n"
    "      contains: (c) => set.has(c),\n"
    "      toggle: (c, on) => (on ? set.add(c) : set.delete(c)),\n"
    "    };\n"
    "    this.classes = set;\n"
    "  }\n"
    "  set textContent(v) { this._text = v; if (v === '') this.children = []; }\n"
    "  get textContent() { return this._text; }\n"
    "  appendChild(c) { this.children.push(c); return c; }\n"
    "  addEventListener(_type, fn) { this.click = fn; }\n"
    "}\n"
    "const nodes = {};\n"
    "const document = {\n"
    "  getElementById: (id) => (nodes[id] = nodes[id] || new El(id)),\n"
    "  createElement: (tag) => new El(tag),\n"
    "};\n"
)


def _diff_prelude(source):
    """A DOM just large enough to run the shipping diff renderer verbatim."""
    return (
        _DOM_STUB
        + "const rows = () => nodes.confirmdiff.children.map(\n"
        "  (c) => [c.className, c.textContent]);\n"
        + _const_line(source, "DIFF_OPEN_LINES") + "\n"
        "let diffChanged = 0;\n"
        + _js_block(source, "function renderConfirmDiff(diff)") + "\n"
        + _js_block(source, "function setDiffDisclosure(open)") + "\n"
    )


def test_a_small_diff_is_shown_expanded_with_added_and_removed_apart(source,
                                                                    tmp_path):
    """Small is the common case (one edit, a handful of lines) and it is the case
    where a disclosure is pure friction — the sheet asks a question the diff
    answers, so the answer is on screen."""
    got = _run("""
      renderConfirmDiff({
        lines: ["--- on disk now", "+++ v1 (session s)", "@@ -1,2 +1,1 @@",
                " keep", "-gone"],
        changed: 1, truncated: false, reason: "",
      });
      console.log(JSON.stringify({
        rows: rows(),
        hidden: nodes.confirmdiff.hidden,
        toggleHidden: nodes.confirmdifftoggle.hidden,
        wide: nodes.confirmbox.classes.has("wide"),
      }));
    """, tmp_path, _diff_prelude(source))
    assert got["hidden"] is False and got["toggleHidden"] is True
    assert got["wide"] is True
    assert got["rows"] == [
        ["cdfile", "--- on disk now"],
        ["cdfile", "+++ v1 (session s)"],
        ["cdhunk", "@@ -1,2 +1,1 @@"],
        ["", " keep"],
        ["cdminus", "-gone"],
    ]


def test_the_file_names_are_classed_by_position_not_by_prefix(source, tmp_path):
    """A removed content line reading `--` arrives as `---`, and a prefix test
    would paint it as a file header — a REMOVAL rendered as scaffolding, in the
    one place the colour is what the user is reading."""
    got = _run("""
      renderConfirmDiff({
        lines: ["--- on disk now", "+++ v2 (session s)", "@@ -1,1 +1,1 @@",
                "---", "+++"],
        changed: 2, truncated: false, reason: "",
      });
      console.log(JSON.stringify(rows()));
    """, tmp_path, _diff_prelude(source))
    assert got[3] == ["cdminus", "---"]
    assert got[4] == ["cdplus", "+++"]


def test_a_large_diff_waits_behind_a_disclosure_that_says_how_much(source,
                                                                  tmp_path):
    """Same idiom as #histtoggle — one disclosure vocabulary in this view — and the
    label carries `changed` so the user knows what pressing it costs."""
    got = _run("""
      const lines = ["--- on disk now", "+++ v1 (session s)", "@@ -1,80 +1,0 @@"];
      for (let i = 0; i < 80; i++) lines.push("-line-" + i);
      renderConfirmDiff({ lines, changed: 80, truncated: false, reason: "" });
      const shut = { hidden: nodes.confirmdiff.hidden,
                     label: nodes.confirmdifftoggle.textContent,
                     toggleHidden: nodes.confirmdifftoggle.hidden,
                     wide: nodes.confirmbox.classes.has("wide") };
      setDiffDisclosure(true);
      const open = { hidden: nodes.confirmdiff.hidden,
                     label: nodes.confirmdifftoggle.textContent };
      console.log(JSON.stringify({ shut, open }));
    """, tmp_path, _diff_prelude(source))
    assert got["shut"]["hidden"] is True
    assert got["shut"]["label"] == "▸ Show 80 changed lines"
    assert got["shut"]["toggleHidden"] is False
    # Wide while still shut: sizing the box on the open state would reflow the
    # whole sheet under the cursor on the very click that opens it.
    assert got["shut"]["wide"] is True
    assert got["open"]["hidden"] is False
    assert got["open"]["label"] == "▾ Hide diff"


def test_the_disclosure_button_flips_the_state_it_reads(source):
    """The listener passes the CURRENT hidden flag straight into the setter, so the
    label and the box can never drift out of step with each other."""
    handler = _js_block(
        source, 'document.getElementById("confirmdifftoggle").addEventListener')
    assert "setDiffDisclosure(document.getElementById(\"confirmdiff\").hidden)" \
        in handler


def test_a_truncated_diff_says_so_with_the_full_count(source, tmp_path):
    """Trailing off silently would present a prefix of the change as the whole of
    it — the same thing the byte cap one layer down refuses to do."""
    got = _run("""
      renderConfirmDiff({
        lines: ["--- on disk now", "+++ v1 (session s)", "@@ -1,3 +1,1 @@", "-a"],
        changed: 900, truncated: true, reason: "",
      });
      setDiffDisclosure(true);
      console.log(JSON.stringify(rows()));
    """, tmp_path, _diff_prelude(source))
    assert got[-1][0] == "cdnote"
    assert "900 lines change in total" in got[-1][1]
    assert "first 4 lines" in got[-1][1]


def test_a_reason_is_rendered_where_the_diff_would_have_been(source, tmp_path):
    """"No diff" is never silent: an absent `<pre>` in a sheet that normally shows
    one reads as the diff being empty, which is a different fact from "too large to
    diff" or "not UTF-8 text"."""
    got = _run("""
      renderConfirmDiff({
        lines: [], changed: 0, truncated: false,
        reason: "This content is too large to diff.",
      });
      console.log(JSON.stringify({
        rows: rows(),
        hidden: nodes.confirmdiff.hidden,
        toggleHidden: nodes.confirmdifftoggle.hidden,
        wide: nodes.confirmbox.classes.has("wide"),
      }));
    """, tmp_path, _diff_prelude(source))
    assert got["rows"] == [["cdnote", "This content is too large to diff."]]
    assert got["hidden"] is False
    assert got["toggleHidden"] is True
    # A sheet with nothing but a sentence must not grow for nothing.
    assert got["wide"] is False


def test_a_plan_with_no_diff_key_renders_nothing_at_all(source, tmp_path):
    """`ok: False` plans carry no `diff`, and a template served from a store older
    than this change would not either — neither may leave an empty box behind."""
    got = _run("""
      renderConfirmDiff(undefined);
      console.log(JSON.stringify({
        hidden: nodes.confirmdiff.hidden,
        toggleHidden: nodes.confirmdifftoggle.hidden,
        wide: nodes.confirmbox.classes.has("wide"),
      }));
    """, tmp_path, _diff_prelude(source))
    assert got == {"hidden": True, "toggleHidden": True, "wide": False}


def test_a_previous_diff_never_leaks_into_the_next_sheet(source, tmp_path):
    """One sheet, reused for every target. A stale diff under a fresh set of counts
    is the same class of error as the write-order bugs in the note below it."""
    got = _run("""
      renderConfirmDiff({
        lines: ["--- on disk now", "+++ v1 (session s)", "@@ -1,1 +1,1 @@", "-old"],
        changed: 1, truncated: false, reason: "",
      });
      renderConfirmDiff({ lines: [], changed: 0, truncated: false,
                          reason: "Nothing to show." });
      console.log(JSON.stringify(rows()));
    """, tmp_path, _diff_prelude(source))
    assert got == [["cdnote", "Nothing to show."]]


def test_the_diff_content_is_only_ever_text(source):
    """These lines are file content off the user's disk, and this template
    routinely annotates HTML. A text node keeps a checkpointed `<script>` inert;
    innerHTML would run it inside the page holding the revert controls."""
    block = _js_block(source, "function renderConfirmDiff(diff)")
    assert "innerHTML" not in block
    assert "textContent = ln" in block


def test_the_diff_sits_between_the_counts_and_the_hazard(source):
    """Reading order: how much changes, what changes, what it costs. Everything
    else about the sheet is untouched — the facts list, the irreversible warning
    and the unwritable-target bail all still stand."""
    box = source[source.index('<div id="confirmbox"'):source.index('id="confirmacts"')]
    assert box.index('id="confirmfacts"') < box.index('id="confirmdiff"')
    assert box.index('id="confirmdiff"') < box.index('id="confirmwarn"')
    assert 'if (plan.writable === false)' in source
    assert 'warn.classList.toggle("hard", irreversible)' in source


# ============================================== the write, and what the user sees
# Four things the revert did to the eye rather than to the file: the sheet vanished
# before the work started, the outcome landed at the bottom of the sidebar, the
# framed reload flashed a blank document under live pins, and the row list showed
# the pre-revert position for a whole extra round trip.

def _sheet_prelude(source):
    """The close path, driven for real."""
    return (
        _DOM_STUB
        + "const confirmEl = document.getElementById('confirm');\n"
        "let busy = false;\n"
        "let pending = { id: 's@v1' };\n"
        "confirmEl.classList.add('open');\n"
        + _js_block(source, "function closeConfirm()") + "\n"
    )


def test_no_close_path_can_fire_while_the_write_is_in_flight(source, tmp_path):
    """`closeConfirm` nulls `pending`, which the click handler is still reading —
    and Cancel, the backdrop and Escape ALL route through it. So the guard belongs
    in the one function rather than at three call sites, which is also what stops
    the in-flight state being taken off screen mid-write."""
    got = _run("""
      busy = true;
      closeConfirm();
      const during = { open: confirmEl.classes.has("open"), pending };
      busy = false;
      closeConfirm();
      console.log(JSON.stringify({
        during,
        after: { open: confirmEl.classes.has("open"), pending },
      }));
    """, tmp_path, _sheet_prelude(source))
    assert got["during"] == {"open": True, "pending": {"id": "s@v1"}}
    assert got["after"] == {"open": False, "pending": None}
    # ...and all three paths really do go through it, so none can grow its own copy.
    assert 'getElementById("confirmno").addEventListener("click", closeConfirm)' \
        in source
    assert "if (e.target === confirmEl) closeConfirm();" in source
    assert 'e.key === "Escape" && confirmEl.classList.contains("open")' in source


def test_the_sheet_stays_up_with_an_in_flight_button_until_the_call_returns(source):
    """It closed BEFORE the await, so an os.replace and a full
    re-enumeration of the store all happened with nothing on screen changing — the
    click read as having done nothing."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    before = handler[:handler.index("await callHistory")]
    after = handler[handler.index("await callHistory"):]
    # Nothing closes the sheet before the call...
    assert "closeConfirm()" not in before
    # ...the controls go into an in-flight state instead...
    assert "busy = true;" in before
    assert "go.disabled = no.disabled = true;" in before
    assert '"Deleting…" : "Reverting…"' in before
    # ...and only the SUCCESS side closes it.
    assert "closeConfirm();" in after
    failure = after[after.index("if (out.error"):after.index("closeConfirm();")]
    assert "closeConfirm" not in failure


def test_the_in_flight_label_is_restored_rather_than_recomputed(source):
    """`askRevert` picks between four wordings (delete/revert × permanent or not);
    rebuilding that decision after the call is how the two drift apart."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    assert "const goLabel = go.textContent;" in handler
    assert "go.textContent = goLabel;" in handler
    # The button comes back live too — it is only ever disabled while in flight.
    assert "go.disabled = no.disabled = false;" in handler
    assert "#confirmacts button:disabled { opacity: 0.5; cursor: default; }" in source


def test_a_failed_revert_reports_inside_the_sheet_and_still_writes_the_note(source):
    """The reason used to land only in #histnote: 11px, muted, at the bottom of the
    sidebar, reached only by looking away from the centered modal that had just
    vanished. Nothing changed on disk, so the sheet is still describing the truth —
    it stays, with the reason in it. The note is still written, because the reload
    and carry-slot paths read it."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    failure = handler[handler.index("if (out.error"):handler.index("closeConfirm();")]
    # renderHistory FIRST, then the message — it rewrites histNote from the
    # timeline's own note, and the other order was a silent no-op.
    assert failure.index("renderHistory();") < failure.index("setNote(")
    assert "err.textContent = out.error" in failure
    assert "err.hidden = false;" in failure
    # A previous failure must not greet the next target — one node, many plans.
    ask = source[source.index("async function askRevert"):]
    assert 'getElementById("confirmerr").hidden = true' in \
        ask[:ask.index("confirmEl.classList.add(\"open\")")]


def _toast_prelude(source):
    return (
        _DOM_STUB
        + "let scheduled = null;\n"
        "globalThis.setTimeout = (fn, ms) => { scheduled = { fn, ms }; return 1; };\n"
        "globalThis.clearTimeout = () => { scheduled = null; };\n"
        + _const_line(source, "TOAST_MS") + "\n"
        "let toastTimer = 0;\n"
        + _js_block(source, "function showToast(text)") + "\n"
    )


def test_the_outcome_is_also_said_where_the_eye_is(source, tmp_path):
    """A centered modal closes and the outcome appears at the bottom of a sidebar
    that may not even be expanded. The toast is TRANSIENT on purpose — it reports
    one click, and a banner still up minutes later is reporting history."""
    got = _run("""
      showToast("Reverted to v2.");
      const up = { text: nodes.toast.textContent, hidden: nodes.toast.hidden,
                   ms: scheduled.ms };
      scheduled.fn();
      const gone = nodes.toast.hidden;
      // A second revert restarts the window rather than leaving two timers to
      // race — the first would hide the second one's message.
      showToast("File deleted.");
      const first = scheduled;
      showToast("Reverted to v1.");
      console.log(JSON.stringify({
        up, gone, restarted: first !== scheduled,
        text: nodes.toast.textContent, hidden: nodes.toast.hidden,
      }));
    """, tmp_path, _toast_prelude(source))
    assert got["up"]["text"] == "Reverted to v2."
    assert got["up"]["hidden"] is False
    assert got["up"]["ms"] > 0
    assert got["gone"] is True
    assert got["restarted"] is True
    assert got["text"] == "Reverted to v1." and got["hidden"] is False


def test_the_note_write_is_not_replaced_by_the_toast(source):
    """Explicitly additive: the carry slot and the post-reload boot path both read
    #histnote, and `reportOutcome` is the only thing that qualifies a success with
    "the timeline could not be reloaded"."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    assert "carryOutcome(outcome);" in handler
    assert "reportOutcome(outcome, false, reloadErr);" in handler
    assert handler.index("reportOutcome(outcome") < handler.index("showToast(outcome)")


def _stage_prelude(source):
    return (
        _DOM_STUB
        + "const pinsEl = document.getElementById('pins');\n"
        "const hl = document.getElementById('hl');\n"
        "const ghost = document.getElementById('ghost');\n"
        + _js_block(source, "function coverStage()") + "\n"
        + _js_block(source, "function uncoverStage()") + "\n"
    )


def test_the_stage_is_covered_across_a_reload_and_uncovered_by_render(source,
                                                                    tmp_path):
    """The boot skeleton `remove()`d itself on the FIRST load, so the post-revert
    reload had no placeholder — and the pins sat in stage coordinates over an empty
    document, pointing at nothing, until the next `load` ran `render()`."""
    got = _run("""
      coverStage();
      const covered = { skel: nodes.stageskel.hidden, pins: nodes.pins.hidden,
                        hl: nodes.hl.style.display, ghost: nodes.ghost.style.display };
      uncoverStage();
      console.log(JSON.stringify({
        covered,
        clear: { skel: nodes.stageskel.hidden, pins: nodes.pins.hidden },
      }));
    """, tmp_path, _stage_prelude(source))
    assert got["covered"] == {"skel": False, "pins": True,
                             "hl": "none", "ghost": "none"}
    assert got["clear"] == {"skel": True, "pins": False}


def test_the_skeleton_node_is_kept_rather_than_removed(source):
    """One cover, two occasions. Removing it meant inventing a second placeholder
    for the reload — or, as it was, showing none."""
    assert "stageskel.remove()" not in source
    handler = source[source.index('frame.addEventListener("load"'):]
    handler = handler[:handler.index("focusFromParam()")]
    # Lifted only AFTER render(), which is what re-resolves every anchor against
    # the new document.
    assert handler.index("render();") < handler.index("uncoverStage();")
    # ...and the reload path puts it up before the document goes blank.
    go = source[source.index('getElementById("confirmgo").addEventListener'):]
    assert go.index("coverStage();") < go.index("contentWindow.location.reload()")


def test_repeat_loads_do_not_accumulate_observers_on_dead_documents(source):
    """This handler runs again on every reload, and the post-revert refresh is what
    made that a real accumulation rather than a theoretical one: an observer left
    watching a discarded document is a leak that also drives renders for a document
    nothing is looking at."""
    handler = source[source.index('frame.addEventListener("load"'):]
    handler = handler[:handler.index("focusFromParam()")]
    assert "if (frameObserver) frameObserver.disconnect();" in handler
    assert "frameObserver = new MutationObserver(queueRender);" in handler
    # ...and nothing creates an unheld one any more.
    assert "new MutationObserver(queueRender).observe" not in source


def test_the_page_adopts_the_timeline_that_rode_back_with_the_write(source):
    """Two serial round trips meant the row list kept showing the PRE-revert
    position for the whole second one — exactly the window in which the user is
    looking at it to see whether the revert worked."""
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    adopt = handler[handler.index("if (out.timeline)"):]
    assert "timeline = out.timeline;" in adopt
    # It is always enriched on the bridge side, so the panel may claim terminality.
    assert "enriched = true;" in adopt
    assert "renderHistory();" in adopt
    # The fallback stays, and it keeps its error channel: reportOutcome is what says
    # the panel on screen is stale.
    #
    # It forces enrichment rather than passing `enriched` through, because it is
    # standing in for a payload the bridge always enriches. `enriched` is the
    # DISCLOSURE state — false whenever the panel has not been expanded, which is the
    # common case — and an unenriched timeline cannot see the did-not-exist boundary,
    # so adopting one reports `at_earliest` a step early (FH-3). The pre-revert
    # disclosure state has no business deciding how accurate the POST-revert panel is.
    assert "reloadErr = await loadHistory(true);" in adopt
    assert "reportOutcome(outcome, false, reloadErr);" in handler


def test_the_revert_returns_the_post_write_timeline(claude_home, tmp_path):
    ann = _load_annotate()
    f = _target(tmp_path, "v2\n")
    write_version(claude_home, "s", f, "v1\n", mtime=1000)
    write_version(claude_home, "s", f, "v2\n", mtime=2000)

    out = ann.main(action="revert", file=f, version_id="s@v1")
    assert out["ok"] is True
    # The position marker has MOVED — this is a timeline of the file after the
    # write, not the one the page already had.
    assert out["timeline"]["position"] == "s@v1"
    assert out["timeline"]["enriched"] is True
    assert out["timeline"]["current"]["size"] == len("v1\n")


def test_a_timeline_that_cannot_be_computed_omits_the_field(claude_home, tmp_path,
                                                           monkeypatch):
    """The write already landed and is already reported, so a failure to
    re-enumerate the store must not turn a successful revert into an error. The
    field is simply absent and the page falls back to its own `history` call, which
    reports its own failure in its own words."""
    ann = _load_annotate()
    control = _target(tmp_path, "now\n", name="control.txt")
    write_version(claude_home, "s", control, "then\n")
    f = _target(tmp_path, "now\n")
    write_version(claude_home, "s", f, "then\n")
    # The same store, the same shape of revert: the field is there when it can be
    # computed, so its absence below is the failure and not the fixture.
    assert "timeline" in ann.main(action="revert", file=control,
                                 version_id="s@v1", confirm_unique=True)

    fh = ann._file_history()
    monkeypatch.setattr(fh, "timeline", lambda *a, **k: 1 / 0)
    out = ann.main(action="revert", file=f, version_id="s@v1",
                   confirm_unique=True)
    assert out["ok"] is True and out["action"] == "restore"
    assert "timeline" not in out
    with open(f, encoding="utf-8") as h:
        assert h.read() == "then\n"


def test_a_swallowed_timeline_failure_still_says_so_on_stderr(claude_home, tmp_path,
                                                              monkeypatch, capsys):
    """The user must not see this — the write landed — but SOMEONE has to be able to.

    Absorbing the exception with no trace at all means a timeline that has started
    failing on every single revert is indistinguishable from one that never fails:
    the page falls back, the panel still paints, and the only symptom is a paid-for
    round trip nobody can account for. stderr is where the engine already collects a
    run's diagnostics, so this costs the user nothing and costs a debugger nothing to
    find.
    """
    ann = _load_annotate()
    f = _target(tmp_path, "now\n")
    write_version(claude_home, "s", f, "then\n")
    fh = ann._file_history()

    def boom(*a, **k):
        raise RuntimeError("store went away")
    monkeypatch.setattr(fh, "timeline", boom)

    out = ann.main(action="revert", file=f, version_id="s@v1",
                   confirm_unique=True)
    assert out["ok"] is True and "timeline" not in out
    # Named exception TYPE and message — "could not refresh" alone sends the reader
    # looking in the wrong module.
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "store went away" in err


def test_the_carry_slot_machinery_is_untouched(source):
    """Explicitly out of scope: its own comments credit it with three past bugs, and
    the suspicion that it now guards a teardown that no longer happens needs
    confirming in the running app before anything here moves."""
    for name in ("HIST_KEY", "function carryOutcome(text)",
                 "function takeCarriedOutcome()", "function dropCarriedOutcome()"):
        assert name in source
    handler = source[source.index('getElementById("confirmgo").addEventListener'):]
    # Still exactly one writer, and the in-page display still spends the slot.
    assert handler.count("carryOutcome(") == 1
    assert "dropCarriedOutcome();" in handler
