"""The automatic layout check (SPEC §28 CV-9, D1512): what
`canvas_layout_check` finds in a canvas.toml, and how the canvas session
hook (`POST /api/canvases/agent/hook`, "post") hands it to a session after a
tool call that changed the layout.
"""
import pytest
from fastapi.testclient import TestClient

import fused_render.canvases as canvases_mod
from fused_render import canvas_layout_check as check
from fused_render.server import create_app

GUARD = {"X-Fused": "1"}
ENV = "unstable"

HEAD = 'type = "canvas"\nversion = 2\n\n[canvas]\nedges = %s\n\n'


def _node(name, x, y, w=700, h=500, extra=""):
    return ('[[canvas.nodes]]\nudfName = "%s"\nx = %s\ny = %s\nwidth = %s\nheight = %s\n%s\n'
            % (name, x, y, w, h, extra))


def _folder(name, x, y, w, h, children):
    kids = ", ".join('"%s"' % c for c in children)
    return ('[[canvas.nodes]]\nudfName = "%s"\ntype = "udf-folder"\nfolderName = "%s"\n'
            'x = %s\ny = %s\nwidth = %s\nheight = %s\nchildUdfOrder = [%s]\n\n'
            % (name, name, x, y, w, h, kids))


# A canvas with nothing wrong: one folder of two nodes side by side, one node
# beside the folder, edges between things that exist.
CLEAN = (HEAD % '[["a", "b"], ["b", "c"]]'
         + _folder("Group", 0, 0, 1555, 615, ["a", "b"])
         + _node("a", 40, 75) + _node("b", 815, 75)
         + _node("c", 1630, 0))


def _doc(text):
    import tomllib
    return tomllib.loads(text)


# ------------------------------------------------------------------ the check

def test_a_clean_canvas_has_no_problems():
    assert check.problems(_doc(CLEAN)) == []
    note = check.note(_doc(CLEAN))
    assert note.startswith("Layout check (fused-render ran it because canvas.toml's layout changed; "
                           "3 nodes, 1 folder, 2 edges)")
    assert "nothing overlaps" in note
    assert 'canvas_screenshot with what: "layout"' in note


def test_boxes_that_only_touch_do_not_overlap():
    doc = _doc(HEAD % "[]" + _node("a", 0, 0) + _node("b", 700, 0) + _node("c", 0, 500))
    assert check.problems(doc) == []


def test_top_level_overlaps_are_named_with_their_size():
    doc = _doc(HEAD % "[]" + _node("a", 0, 0) + _node("b", 600, 400)
               + _folder("Group", 650, 0, 800, 300, []))
    found = check.problems(doc)
    assert "node `a` and node `b` overlap by 100 x 100 px" in found
    assert "folder `Group` and node `a` overlap by 50 x 300 px" in found
    assert "folder `Group` and node `b` overlap by 650 x 0 px" not in found   # they miss
    assert len(found) == 2


def test_a_child_is_placed_relative_to_its_folder():
    # At x = 40, y = 75 INSIDE a folder at 2000, 3000: not on top of the node
    # that sits at 40, 75 on the canvas itself.
    doc = _doc(HEAD % "[]" + _folder("Group", 2000, 3000, 780, 615, ["inside"])
               + _node("inside", 40, 75) + _node("outside", 40, 75))
    assert check.problems(doc) == []


def test_a_child_outside_its_folder_says_which_edges():
    doc = _doc(HEAD % "[]" + _folder("Group", 0, 0, 780, 615, ["a"]) + _node("a", 200, 300))
    found = check.problems(doc)
    assert len(found) == 1
    assert found[0].startswith("`a` is not inside its folder `Group`: "
                               "120 px past its right edge, 185 px past its bottom edge")


def test_children_of_one_folder_overlapping():
    doc = _doc(HEAD % "[]" + _folder("Group", 0, 0, 2000, 700, ["a", "b"])
               + _node("a", 40, 75) + _node("b", 440, 75))
    assert check.problems(doc) == [
        "node `a` and node `b` overlap inside folder `Group` by 300 x 500 px"]


def test_names_left_behind_by_a_deleted_node():
    doc = _doc(HEAD % '[["a", "gone"], ["ghost", "phantom"]]'
               + _folder("Group", 0, 0, 780, 615, ["a", "gone"]) + _node("a", 40, 75))
    found = check.problems(doc)
    assert "folder `Group` lists `gone` in childUdfOrder, but no node has that name" in found
    assert "edge `a` -> `gone` names `gone`, which is not on the canvas" in found
    assert "edge `ghost` -> `phantom` names `ghost` and `phantom`, which is not on the canvas" in found


def test_a_node_in_two_folders_and_a_folder_in_a_folder():
    doc = _doc(HEAD % "[]" + _folder("One", 0, 0, 780, 615, ["a", "Two"])
               + _folder("Two", 1000, 0, 780, 615, ["a"]) + _node("a", 40, 75))
    found = check.problems(doc)
    assert any(f.startswith("`a` is listed by 2 folders (`One`, `Two`)") for f in found)
    assert any("lists the folder `Two` as a child" in f for f in found)


def test_missing_sizes_take_the_workbench_defaults():
    # No width/height: a UDF is 700 x 500, a note (its source is a .md) 350 x 250.
    text = (HEAD % "[]"
            + '[[canvas.nodes]]\nudfName = "u"\nx = 0\ny = 0\n\n'
            + '[[canvas.nodes]]\nudfName = "n"\nx = 400\ny = 0\n\n')
    assert check.problems(_doc(text), present=["u.py", "n.md"]) == [
        "node `u` and node `n` overlap by 300 x 250 px"]


def test_a_long_list_is_cut_and_counted():
    text = HEAD % "[]" + "".join(_node("n%d" % i, 0, 0) for i in range(8))
    note = check.note(_doc(text))
    assert "found 28 problems:" in note
    assert note.count("\n- ") == check.MAX_LINES + 1 and "- (+16 more)" in note
    assert note.rstrip().endswith('look at the result: canvas_screenshot with what: "layout".')


def test_problems_from_before_the_turn_are_counted_not_listed():
    # The canvas already had `a` on top of `b`. The session then puts `c` on
    # top of `d`: that one is listed, the old one only counted.
    old = HEAD % "[]" + _node("a", 0, 0) + _node("b", 100, 100)
    before = check.problems(_doc(old))
    assert len(before) == 1
    now = _doc(old + _node("c", 5000, 0) + _node("d", 5100, 0))
    note = check.note(now, before=before)
    assert "found 1 problem that came in this turn:" in note
    assert "- node `c` and node `d` overlap by 600 x 500 px" in note
    assert "`a` and node `b`" not in note
    assert "1 problem the canvas already had before this turn is unchanged and not listed" in note
    # A change that adds nothing says so, and still counts the old one.
    moved = _doc(old + _node("c", 5000, 0))
    note = check.note(moved, before=before)
    assert "your changes this turn added no problem." in note and "found" not in note
    assert "1 problem the canvas already had" in note
    # Touching the old overlap makes it the session's: the sentence changed.
    worse = _doc(HEAD % "[]" + _node("a", 0, 0) + _node("b", 50, 100))
    assert "- node `a` and node `b` overlap by 650 x 400 px" in check.note(worse, before=before)


def test_two_pairs_with_one_sentence_are_counted_apart():
    # Two nodes may share a name; so may two overlapping pairs their
    # sentence. One such pair was there before, a second is added: one new.
    pair = _node("a", 0, 0) + _node("b", 100, 100)
    before = check.problems(_doc(HEAD % "[]" + pair))
    assert len(before) == 1
    twice = _doc(HEAD % "[]" + pair + _node("a", 0, 0) + _node("b", 100, 100))
    found = check.problems(twice)
    assert found.count(before[0]) >= 2
    note = check.note(twice, before=before)
    assert "that came in this turn" in note and "added no problem" not in note
    assert "1 problem the canvas already had before this turn is unchanged" in note


def test_a_canvas_too_big_to_go_through_is_never_called_clean(monkeypatch):
    # Every pair of boxes at one spot overlaps. Past the budget the check
    # stops and says so, as a problem that is always listed.
    monkeypatch.setattr(check, "MAX_PROBLEMS", 10)
    text = HEAD % "[]" + "".join(_node("n%d" % i, 0, 0) for i in range(30))
    found = check.problems(_doc(text))
    assert len(found) == 11 and found[-1] == check.INCOMPLETE
    note = check.note(_doc(text), before=found)
    assert "stopped early" in note and "added no problem" not in note
    monkeypatch.setattr(check, "MAX_PROBLEMS", 500)
    monkeypatch.setattr(check, "MAX_PAIRS", 5)
    spread = HEAD % "[]" + "".join(_node("n%d" % i, i * 1000, 0) for i in range(8))
    assert check.problems(_doc(spread)) == [check.INCOMPLETE]


def test_what_makes_a_row_a_note_is_layout():
    # No size, no source file: `textBoxColor` decides between 350 x 250 and
    # 700 x 500, so adding or dropping it moves the box. Recolouring does not.
    def canvas(extra):
        return _doc(HEAD % "[]" + '[[canvas.nodes]]\nudfName = "a"\nx = 0\ny = 0\n%s\n' % extra
                    + '[[canvas.nodes]]\nudfName = "b"\nx = 400\ny = 0\n\n')
    yellow, pink, plain = (canvas('textBoxColor = "#FFF9C4"'), canvas('textBoxColor = "#FCE4EC"'),
                           canvas(""))
    assert check.fingerprint(yellow) == check.fingerprint(pink)
    assert check.fingerprint(yellow) != check.fingerprint(plain)
    assert check.problems(yellow) == [] and len(check.problems(plain)) == 1


def test_the_fingerprint_follows_layout_and_nothing_else():
    base = check.fingerprint(_doc(CLEAN))
    # A title, a description, a colour: not layout.
    retitled = CLEAN.replace('udfName = "c"\n', 'udfName = "c"\ntitle = "New"\ndescription = "d"\n')
    assert check.fingerprint(_doc(retitled)) == base
    # A move, a resize, a child list, an edge, a deleted row: layout.
    for changed in (CLEAN.replace("x = 1630", "x = 1700"),
                    CLEAN.replace("width = 1555", "width = 1600"),
                    CLEAN.replace('childUdfOrder = ["a", "b"]', 'childUdfOrder = ["a"]'),
                    CLEAN.replace('[["a", "b"], ["b", "c"]]', '[["a", "b"]]'),
                    CLEAN.replace(_node("c", 1630, 0), "")):
        assert check.fingerprint(_doc(changed)) != base


def test_state_reads_the_file_and_knows_a_broken_one(tmp_path):
    path = tmp_path / "canvas.toml"
    assert check.state(str(path)) == ("", None, None)          # no file: nothing to say
    path.write_text(CLEAN)
    mark, doc, error = check.state(str(path))
    assert mark == check.fingerprint(doc) and error is None
    path.write_text(CLEAN + "\n[[canvas.nodes\n")
    mark2, doc2, error2 = check.state(str(path))
    assert mark2.startswith("bad:") and mark2 != mark and doc2 is None and error2
    assert "no longer parses" in check.error_note(error2)


# -------------------------------------------------------------------- the hook

@pytest.fixture
def clone(tmp_path, monkeypatch):
    root = tmp_path / "canvases"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(root))
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(tmp_path / "creds.json"))
    monkeypatch.delenv("FUSED_RENDER_WORKBENCH_ENV", raising=False)
    canvases_mod._cli_cache_clear()
    canvases_mod._layout_seen.clear()
    folder = root / ENV / "user" / "demo"
    folder.mkdir(parents=True)
    (folder / "canvas.toml").write_text(CLEAN)
    yield folder
    canvases_mod._layout_seen.clear()
    canvases_mod._cli_cache_clear()


@pytest.fixture
def client(clone, tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _hook(client, clone, event, session="s1", tool="Edit"):
    res = client.post("/api/canvases/agent/hook", headers=GUARD, json={
        "event": event, "session_id": session, "cwd": str(clone), "tool_name": tool})
    assert res.status_code == 200
    return res.json()


def test_a_layout_change_is_told_after_the_tool_call_that_made_it(client, clone):
    assert _hook(client, clone, "start") == {}              # a turn begins: the baseline
    assert _hook(client, clone, "post", tool="Read") == {}  # nothing changed
    # The session moves `c` onto the folder.
    (clone / "canvas.toml").write_text(CLEAN.replace("x = 1630", "x = 1000"))
    context = _hook(client, clone, "post")["context"]
    assert context.startswith("Layout check (fused-render ran it because")
    assert "found 1 problem:" in context
    assert "- folder `Group` and node `c` overlap by 555 x 500 px" in context
    # Told once: the next call with the same layout says nothing.
    assert _hook(client, clone, "post") == {}
    # Fixed: one line saying so.
    (clone / "canvas.toml").write_text(CLEAN)
    assert "nothing overlaps" in _hook(client, clone, "post")["context"]


def test_a_deleted_node_is_caught_by_what_still_names_it(client, clone):
    _hook(client, clone, "start")
    (clone / "canvas.toml").write_text(CLEAN.replace(_node("b", 815, 75), ""))
    context = _hook(client, clone, "post", tool="Bash")["context"]
    assert "folder `Group` lists `b` in childUdfOrder, but no node has that name" in context
    assert "edge `a` -> `b` names `b`" in context and "edge `b` -> `c` names `b`" in context


def test_what_the_canvas_had_at_turn_start_stays_out_of_the_list(client, clone):
    # The canvas begins the turn with `c` on the folder (not this session's
    # doing). The session then adds `d` on top of `c`.
    start = CLEAN.replace("x = 1630", "x = 1000")
    (clone / "canvas.toml").write_text(start)
    _hook(client, clone, "start")
    (clone / "canvas.toml").write_text(start + _node("d", 1100, 100))
    context = _hook(client, clone, "post")["context"]
    assert "found 2 problems that came in this turn:" in context
    assert "node `c` and node `d` overlap" in context
    assert "folder `Group` and node `d` overlap" in context
    assert "folder `Group` and node `c` overlap" not in context
    assert "1 problem the canvas already had before this turn is unchanged" in context
    # Still listed on the next change while it is there (not only once).
    (clone / "canvas.toml").write_text(start + _node("d", 1100, 100) + _node("e", 9000, 0))
    assert "node `c` and node `d` overlap" in _hook(client, clone, "post")["context"]
    # A new turn: what is there now is the baseline again.
    _hook(client, clone, "start")
    (clone / "canvas.toml").write_text(start + _node("d", 1100, 100) + _node("e", 9100, 0))
    context = _hook(client, clone, "post")["context"]
    assert "your changes this turn added no problem." in context
    assert "3 problems the canvas already had before this turn are unchanged" in context


def test_an_edit_that_is_not_layout_says_nothing(client, clone):
    _hook(client, clone, "start")
    (clone / "canvas.toml").write_text(
        CLEAN.replace('udfName = "c"\n', 'udfName = "c"\ntitle = "Chart"\n'))
    assert _hook(client, clone, "post") == {}


def test_a_file_that_stops_parsing_is_told(client, clone):
    _hook(client, clone, "start")
    (clone / "canvas.toml").write_text(CLEAN + "\nx = = 3\n")
    assert "no longer parses" in _hook(client, clone, "post")["context"]


def test_a_sessions_first_call_only_sets_the_baseline(client, clone):
    # No "start" was heard (a session older than this server): the first
    # call is the baseline, and each session has its own.
    (clone / "canvas.toml").write_text(CLEAN.replace("x = 1630", "x = 1000"))
    assert _hook(client, clone, "post", session="old") == {}
    (clone / "canvas.toml").write_text(CLEAN)
    assert "nothing overlaps" in _hook(client, clone, "post", session="old")["context"]
    assert _hook(client, clone, "post", session="other") == {}


def test_a_reference_copy_is_never_checked(client, clone):
    from fused_render import canvas_marker

    (clone / canvas_marker.MARKER_NAME).write_text(
        '{"version": 1, "sync": "mirror", "scope": "user", "name": "demo"}')
    _hook(client, clone, "start")
    (clone / "canvas.toml").write_text(CLEAN.replace("x = 1630", "x = 1000"))
    assert _hook(client, clone, "post") == {}


def test_the_canvas_checked_is_the_sessions_own(client, clone, tmp_path):
    # The session works in `demo`; a tool call names a file in ANOTHER clone.
    # The note is still about `demo`, and the other clone gets no baseline.
    other = clone.parent / "other"
    other.mkdir()
    (other / "canvas.toml").write_text(CLEAN.replace("x = 1630", "x = 1000"))
    _hook(client, clone, "start")
    (clone / "canvas.toml").write_text(CLEAN.replace("x = 1630", "x = 900"))
    res = client.post("/api/canvases/agent/hook", headers=GUARD, json={
        "event": "post", "session_id": "s1", "cwd": str(clone), "tool_name": "Read",
        "tool_input": {"file_path": str(other / "a.py")}})
    assert "folder `Group` and node `c` overlap by 655 x 500 px" in res.json()["context"]
    assert all(key[1] == str(clone) for key in canvases_mod._layout_seen)
    # A session whose working directory is no clone is told nothing.
    res = client.post("/api/canvases/agent/hook", headers=GUARD, json={
        "event": "post", "session_id": "s2", "cwd": str(tmp_path), "tool_name": "Edit",
        "tool_input": {"file_path": str(clone / "canvas.toml")}})
    assert res.json() == {}


def test_an_overlapping_call_keeps_the_baseline(clone, monkeypatch):
    # While one call for a session is working out its note, a second one for
    # the same session must still find the baseline (not take it for new).
    assert canvases_mod._layout_note("s1", str(clone), announce=False) == ""
    (clone / "canvas.toml").write_text(CLEAN.replace("x = 1630", "x = 1000"))
    real = check.problems
    inner = []

    def slow(doc, present=()):
        if not inner:
            inner.append(canvases_mod._layout_seen.get(("s1", str(clone))))
        return real(doc, present)
    monkeypatch.setattr(check, "problems", slow)
    note = canvases_mod._layout_note("s1", str(clone), announce=True)
    assert "overlap" in note
    assert inner[0] is not None           # still there mid-call


def test_the_note_follows_what_the_sync_said():
    assert canvases_mod._with_layout({}, "") == {}
    assert canvases_mod._with_layout({"context": "user moved a"}, "") == {"context": "user moved a"}
    assert canvases_mod._with_layout({}, "Layout check") == {"context": "Layout check"}
    assert canvases_mod._with_layout({"context": "user moved a"}, "Layout check") == {
        "context": "user moved a\n\nLayout check"}
