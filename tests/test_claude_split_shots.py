"""claude_split's annotation screenshots: a PNG crop of the element the user
pointed at, attached to the annotation BY PATH.

The shape of the feature, and why each half is the way it is:

* **one pane capture, cropped N times.** The left pane is serialised once (clone
  the framed body, inline every computed style, rasterise each `<canvas>`, run it
  through an `<svg><foreignObject>` into a canvas) and each annotation's crop is
  cut out of that one bitmap using its on-screen rect. Serialising each annotated
  subtree on its own would render a flex child alone, collapsing it and losing
  exactly the ancestor layout that makes it look like what the user pointed at.
* **a path, not an inline image.** The annotation JSON carries a filesystem path,
  so the crop costs nothing until the agent decides the visual matters and reads
  it. `--allowed-tools` pre-approves `Read` of that one directory so choosing to
  look does not raise a permission card.
* **blank WebGL is reported, never shipped.** maplibre/deck.gl make their context
  with `preserveDrawingBuffer: false`, so `toDataURL` on their canvas reads back
  fully transparent. An agent shown a blank image believes the app rendered
  nothing, which costs a debugging loop — so those annotations get `shot: null`
  and a `shotNote` saying why.

What node cannot cover, and what therefore is NOT asserted anywhere below:
rasterisation fidelity (whether the bitmap looks like the app), real capture
latency, and whether a real WebGL canvas actually reads back transparent. Those
need a browser; the tests here pin the arithmetic, the JSON shape, the
degradation paths and the caps.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude_split")
TEMPLATE = os.path.join(TEMPLATE_DIR, "template.html")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_split_shots_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


@pytest.fixture
def html():
    return open(TEMPLATE, encoding="utf-8").read()


# ------------------------------------------------- where the crops are allowed

def test_the_shots_dir_is_a_sibling_of_the_runs_dir_not_inside_one(agent):
    """The ordering constraint that decides the whole layout: annotations are
    captured while the outgoing message is composed, and the run dir does not
    exist until `_start` runs, strictly afterwards. A per-run directory would
    mean writing the crops somewhere else first and moving them."""
    assert os.path.dirname(agent.SHOTS) == os.path.dirname(agent.RUNS)
    assert agent.SHOTS != agent.RUNS
    assert not agent.SHOTS.startswith(agent.RUNS + os.sep)


def test_a_crop_never_lands_in_the_users_project(agent):
    """Screenshots are ours, not the user's files. Writing them next to their
    source would put untracked binaries in a repo they did not ask for."""
    assert agent.SHOTS.startswith(tempfile.gettempdir() + os.sep)


def test_the_read_rule_uses_the_double_slash_the_cli_needs(agent):
    """The load-bearing detail, verified against claude 2.1.221: the CLI reads a
    rule path as RELATIVE unless it starts with `//`, so a single-slash rule
    matches nothing and every crop raises a card instead."""
    assert agent._read_rule("/tmp/fr/shots") == "Read(//tmp/fr/shots/**)"
    # Windows: backslashes become forward ones, the drive letter survives.
    assert agent._read_rule(r"C:\Users\a\shots") == "Read(//C:/Users/a/shots/**)"


def test_the_rule_and_the_page_spell_a_windows_shots_path_the_same_way(agent, html):
    """D146, and the reason `_wire_path` exists at all.

    The CLI matches an allow-rule as TEXT, not as a resolved path (see
    `_read_rule`), so the path inside `Read(//…/**)` and the path the page puts
    in the annotation JSON have to be the SAME STRING or every crop raises a card
    and the whole pre-approval is defeated. On POSIX they agreed by accident; on
    Windows `SHOTS` comes off `os.path.join`, so the rule said
    `C:/Users/a/shots` while the page joined `C:\\Users\\a\\shots\\x.png`.
    One normalisation on the python side is what makes them agree."""
    win = r"C:\Users\a\AppData\Local\Temp\fr\shots"
    handed = agent._wire_path(win)          # what the page is given
    rule = agent._read_rule(win)            # what the spawn line pre-approves
    assert handed == "C:/Users/a/AppData/Local/Temp/fr/shots"
    assert rule == "Read(//C:/Users/a/AppData/Local/Temp/fr/shots/**)"
    # The page's own join, run for real: the crop path has to sit under the
    # rule's prefix textually, which is the only way the CLI compares them.
    crop = _node(["function shotJoin("],
                 "console.log(JSON.stringify(shotJoin(%s, 'x.png')));"
                 % json.dumps(handed), html)
    assert "\\" not in crop, crop
    assert crop.lstrip("/").startswith(rule[len("Read(//"):-len("**)")]), crop


def test_the_directory_handed_to_the_page_is_the_one_the_rule_names(
        agent, tmp_path, monkeypatch):
    """The other half of the agreement: `_shots_dir` must hand out the wire
    spelling, not the raw `os.path.join` one, or the normalisation in the rule
    has nothing to agree with."""
    shots = tmp_path / "fr" / "shots"
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    assert agent._shots_dir() == {"dir": agent._wire_path(str(shots))}


def test_a_forward_slash_windows_path_is_still_writable_by_the_upload(agent):
    """Why forward slashes are the form that WINS rather than backslashes: the
    crop is written through `/api/fs/upload`, whose only path shape requirement
    is `os.path.isabs`. Windows' `ntpath` treats both separators as separators,
    so the one spelling the allow-rule can name is also a path the server
    writes — checked against ntpath directly, since this suite cannot run on
    Windows."""
    import ntpath
    p = agent._wire_path(r"C:\Users\a\shots") + "/x.png"
    assert ntpath.isabs(p)
    assert ntpath.dirname(p) == "C:/Users/a/shots"
    assert ntpath.basename(p) == "x.png"


def test_the_spawn_line_pre_approves_reading_a_crop_and_nothing_else(
        agent, tmp_path, monkeypatch):
    """The user attached the screenshot deliberately; carding a read of it would
    make them approve their own annotation. Scoped to the one directory."""
    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(agent, "SHOTS", str(tmp_path / "shots"))
    project = tmp_path / "proj"
    project.mkdir()
    seen = {}

    class _Proc:
        pid = 4242

    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: (seen.__setitem__("cmd", cmd), _Proc())[1])
    assert "error" not in agent._start(str(project), "hi", "", "", "")
    cmd = seen["cmd"]
    allowed = cmd[cmd.index("--allowed-tools") + 1].split(",")
    assert agent._read_rule(str(tmp_path / "shots")) in allowed
    # Not a blanket Read: a rule with no path would allow the whole filesystem.
    assert "Read" not in allowed
    # And the prompt bridge is still wired for everything else.
    assert "--permission-prompt-tool" in cmd


# ------------------------------------------------------- preparing the directory

def test_the_shots_dir_is_created_private_and_adopted_on_a_second_call(
        agent, tmp_path, monkeypatch):
    """Unlike a run dir this one is SHARED and long-lived, so an existing
    directory is adopted rather than refused — the exclusive-create that makes a
    run dir's 0700 meaningful would fail on the second message."""
    root = tmp_path / "fr" / "runs"
    monkeypatch.setattr(agent, "RUNS", str(root))
    shots = tmp_path / "fr" / "shots"
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    assert agent.main(action="shots_dir") == {"dir": str(shots)}
    assert os.path.isdir(shots)
    if hasattr(os, "geteuid"):
        assert stat.S_IMODE(os.lstat(shots).st_mode) == 0o700
    # second message, same directory
    assert agent.main(action="shots_dir") == {"dir": str(shots)}


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX mode bits")
def test_a_shots_dir_anyone_can_write_to_is_refused(agent, tmp_path, monkeypatch):
    """The temp root is world-writable and our path under it is predictable, so
    another account can pre-create this directory. Adopting theirs would hand
    them every picture of this user's screen."""
    shots = tmp_path / "fr" / "shots"
    shots.mkdir(parents=True)
    os.chmod(shots, 0o777)
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    with pytest.raises(PermissionError):
        agent._shots_dir()


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX mode bits")
def test_an_adopted_shots_dir_is_tightened_to_owner_only(agent, tmp_path,
                                                         monkeypatch):
    """`_require_private` only refuses a directory others can WRITE to, which is
    the right test for a parent. It is not enough for this leaf: a crop is a
    picture of the user's screen, so a merely world-READABLE directory (one an
    earlier version, or a stray mkdir, left at 0755) has to be tightened."""
    shots = tmp_path / "fr" / "shots"
    shots.mkdir(parents=True)
    os.chmod(shots, 0o755)
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    assert agent._shots_dir() == {"dir": str(shots)}
    assert stat.S_IMODE(os.lstat(shots).st_mode) == 0o700


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX mode bits")
def test_an_adopted_dir_that_cannot_be_tightened_is_refused(agent, tmp_path,
                                                            monkeypatch):
    """The failure path of the tightening above, and it must not be best-effort.
    A crop is a picture of the user's screen; handing back a directory the check
    just PROVED others can read would invert the asymmetry this function is built
    on — a refusal only denies the user screenshots, adopting denies them their
    privacy. So the mode is re-read after the chmod (an exotic filesystem or an
    ACL can accept the call and keep the bits) and a still-loose directory is an
    error, which the page degrades to sending no screenshots."""
    shots = tmp_path / "fr" / "shots"
    shots.mkdir(parents=True)
    os.chmod(shots, 0o755)
    monkeypatch.setattr(agent, "SHOTS", str(shots))

    monkeypatch.setattr(agent.os, "chmod",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))
    out = agent._shots_dir()
    assert "dir" not in out and out.get("error"), out

    # The subtler one: the call SUCCEEDS and the bits do not move.
    monkeypatch.setattr(agent.os, "chmod", lambda *a, **k: None)
    out = agent._shots_dir()
    assert "dir" not in out and out.get("error"), out

    # And the success path still hands the directory over.
    monkeypatch.undo()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    assert agent._shots_dir().get("dir")


def test_a_shots_dir_that_cannot_be_made_is_an_error_not_a_crash(
        agent, tmp_path, monkeypatch):
    """No directory means no screenshots, which the page degrades to sending the
    annotations without them. It must never mean no message."""
    blocker = tmp_path / "fr"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory")
    monkeypatch.setattr(agent, "SHOTS", str(blocker / "shots"))
    out = agent._shots_dir()
    assert out.get("error") and "dir" not in out


def test_stale_and_excess_crops_are_pruned(agent, tmp_path, monkeypatch):
    """A crop stops mattering when its conversation does, and nothing else ever
    deletes them: the page names the file and the agent only reads it."""
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    monkeypatch.setattr(agent, "SHOTS_KEEP", 3)
    old = shots / "ancient.png"
    old.write_bytes(b"x")
    os.utime(old, (0, time.time() - agent.SHOTS_TTL - 10))
    fresh = []
    for i in range(5):
        p = shots / ("f%d.png" % i)
        p.write_bytes(b"x")
        os.utime(p, (0, time.time() - (5 - i)))
        fresh.append(p)
    agent._prune_shots()
    assert not old.exists(), "a crop past its TTL is not kept"
    # oldest-first over the count cap, so the recent conversation is what survives
    left = sorted(p.name for p in shots.iterdir())
    assert left == ["f2.png", "f3.png", "f4.png"], left


def test_pruning_never_fails_the_action(agent, tmp_path, monkeypatch):
    """Housekeeping on a temp directory. No failure here is worth refusing the
    user a screenshot over."""
    monkeypatch.setattr(agent, "SHOTS", str(tmp_path / "never-made"))
    agent._prune_shots()  # must not raise


def test_the_page_asks_for_the_directory_by_the_action_the_agent_serves(
        agent, html, tmp_path, monkeypatch):
    """D146, the two-sided wire: the page names the action, agent.py routes it."""
    assert 'action: "shots_dir"' in html
    monkeypatch.setattr(agent, "SHOTS", str(tmp_path / "shots"))
    assert agent.main(action="shots_dir").get("dir") == str(tmp_path / "shots")


# ---------------------------------------------- the page's own JS, under node

def _node(fn_names, call, html, prelude=""):
    """Run named top-level functions/consts out of template.html under node.

    Same extraction as tests/test_claude_split_app_state.py's `_node`: what
    matters is the object the agent ends up reading, not the source that built
    it. Kept as its own copy rather than imported across test modules — the two
    suites are independent and a shared harness would couple them."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own capture helpers")
    chunks = []
    for name in fn_names:
        start = html.index(name)
        if name.startswith("function") or name.startswith("async function"):
            end = html.index("\n}\n", start) + 3      # closing brace at column 0
            chunks.append(html[start:end])
            continue
        taken = []
        for line in html[start:].split("\n"):
            taken.append(line)
            if line.split("//")[0].rstrip().endswith(";"):
                break
        chunks.append("\n".join(taken))
    script = prelude + "\n" + "\n".join(chunks) + "\n" + call
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_CAPS = ["const SHOT_MAX_EDGE", "const SHOT_MAX_BYTES", "const SHOT_MAX_COUNT",
         "const SHOT_MIN_AREA", "const SHOT_TIMEOUT_MS"]


# ------------------------------------------------------------ the crop rect math

def test_a_crop_rect_is_whole_pixels_that_cover_the_element(html):
    """Floor the origin and ceil the far edge: rounding both the same way loses
    the element's last row of pixels, which on a 1px border is the whole point
    of the crop."""
    out = _node(_CAPS + ["function shotCropRect("],
                "console.log(JSON.stringify(shotCropRect("
                "{left: 10.4, top: 20.6, width: 30.3, height: 40.1}, 800, 600)));",
                html)
    assert out == {"left": 10, "top": 20, "width": 31, "height": 41}


def test_a_crop_rect_is_clamped_to_the_pane_bitmap(html):
    """The bitmap is the VISIBLE pane, so an element hanging off the edge is
    cropped to what was actually captured — drawImage from outside the source
    would give transparent padding and read as missing UI."""
    out = _node(_CAPS + ["function shotCropRect("],
                "console.log(JSON.stringify({over: shotCropRect("
                "{left: 780, top: 590, width: 100, height: 100}, 800, 600),"
                " under: shotCropRect({left: -20, top: -10, width: 60, height: 50},"
                " 800, 600)}));", html)
    assert out["over"] == {"left": 780, "top": 590, "width": 20, "height": 10}
    assert out["under"] == {"left": 0, "top": 0, "width": 40, "height": 40}


def test_an_element_with_nothing_to_look_at_gets_no_crop(html):
    """Zero-area (display:none resolves to a 0x0 rect), a sliver below the area
    floor, and entirely outside the visible pane — the last is the element
    scrolled out of view, which is the same condition that hides its pin."""
    out = _node(_CAPS + ["function shotCropRect("],
                "console.log(JSON.stringify({"
                "zero: shotCropRect({left: 5, top: 5, width: 0, height: 0}, 800, 600),"
                "sliver: shotCropRect({left: 5, top: 5, width: 60, height: 1}, 800, 600),"
                "below: shotCropRect({left: 5, top: 900, width: 60, height: 30}, 800, 600),"
                "right: shotCropRect({left: 900, top: 5, width: 60, height: 30}, 800, 600)"
                "}));", html)
    assert out["zero"] is None
    assert out["sliver"] is None, "a 60x1 sliver is 60px² — under the floor"
    assert out["below"] is None
    assert out["right"] is None


# ------------------------------------------------------------- the downscale math

def test_a_crop_is_downscaled_by_its_longest_edge(html):
    out = _node(_CAPS + ["function shotFit("],
                "console.log(JSON.stringify({"
                "wide: shotFit(1600, 400), tall: shotFit(400, 1600),"
                "small: shotFit(320, 200), exact: shotFit(640, 640)}));", html)
    assert out["wide"] == {"width": 640, "height": 160, "scale": 0.4}
    assert out["tall"] == {"width": 160, "height": 640, "scale": 0.4}
    # never UPscaled: a 320px button blown up to 640 is bigger bytes and no more
    # information
    assert out["small"] == {"width": 320, "height": 200, "scale": 1}
    assert out["exact"]["width"] == 640 and out["exact"]["scale"] == 1


def test_a_downscale_never_rounds_an_edge_to_zero(html):
    """A 1000x1 rule would scale to 640x0.64 and a zero-height canvas throws on
    toBlob, which would lose the whole capture rather than one crop."""
    out = _node(_CAPS + ["function shotFit("],
                "console.log(JSON.stringify(shotFit(4000, 2)));", html)
    assert out["height"] >= 1


# --------------------------------------------- blank WebGL, detected not shipped

def test_a_transparent_readback_is_recognised_as_unreadable(html):
    """The failure this exists to prevent: an agent shown a blank image believes
    the app rendered nothing, and spends a debugging loop on it."""
    out = _node(["function shotReadbackIsBlank("],
                "console.log(JSON.stringify({"
                "blank: shotReadbackIsBlank('data:image/png;base64,AAA',"
                " 'data:image/png;base64,AAA'),"
                "drawn: shotReadbackIsBlank('data:image/png;base64,ZZZ',"
                " 'data:image/png;base64,AAA'),"
                "threw: shotReadbackIsBlank('', 'data:image/png;base64,AAA'),"
                "noprobe: shotReadbackIsBlank('data:image/png;base64,ZZZ', '')}));",
                html)
    assert out["blank"] is True, "identical to an empty canvas of the same size"
    assert out["drawn"] is False
    assert out["threw"] is True, "a tainted/failed toDataURL is not readable either"
    # No probe means we cannot tell — and 'cannot tell' has to be unreadable, not
    # 'fine': shipping a maybe-blank image is the whole failure mode.
    assert out["noprobe"] is True


_BLANK_FNS = ["const SHOT_BLANK_BLOCK", "function shotRectOverlap(",
              "function shotBlankCover("]

# The blank map fills the pane; the elements below sit at their own rects over it.
_BLANK_STUBS = """
var RECTS = {};
function annStageRect(el) { return RECTS[el.name]; }
const cv = {name: "cv", tagName: "CANVAS", contains: (n) => n === cv};
RECTS.cv = {left: 0, top: 0, width: 800, height: 600};
const cover = (el) => shotBlankCover(el, [cv], RECTS[el.name]);
"""


def test_an_annotation_on_or_around_a_blank_canvas_is_blocked(html):
    """`is or contains`: pointing at the map itself, and pointing at the panel
    the map is inside, are both "these pixels are not readable". Kept as an
    absolute regardless of geometry — a panel far bigger than the map it holds
    is still a claim about the map."""
    out = _node(_BLANK_FNS, _BLANK_STUBS + """
const panel = {name: "panel", tagName: "DIV", contains: (n) => n === panel || n === cv};
RECTS.panel = {left: 0, top: 0, width: 800, height: 600};
const far = {name: "far", tagName: "BUTTON", contains: () => false};
RECTS.far = {left: 810, top: 10, width: 60, height: 20};
console.log(JSON.stringify({
  itself: cover(cv), ancestor: cover(panel), elsewhere: cover(far),
  none: shotBlankCover(panel, [], RECTS.panel), block: SHOT_BLANK_BLOCK,
}));
""", html)
    assert out["itself"] == 1
    assert out["ancestor"] == 1
    # A sibling that does not overlap the canvas renders in the DOM and captures
    # fine. This is the case the guard must NOT widen into.
    assert out["elsewhere"] == 0
    assert out["none"] == 0
    assert out["block"] <= 1


def test_an_element_sitting_over_a_blank_canvas_is_blocked_too(html):
    """The gap the containment-only guard left, and it is the single most common
    map-app layout: a legend/zoom control absolutely positioned OVER the map is
    neither the canvas nor a container of it, so it used to get a real crop —
    the control floating on a blank backdrop, with no note to say the map behind
    it is missing. Geometry, not DOM ancestry, is what decides this."""
    out = _node(_BLANK_FNS, _BLANK_STUBS + """
const legend = {name: "legend", tagName: "DIV", contains: () => false};
RECTS.legend = {left: 600, top: 500, width: 180, height: 80};
const clipped = {name: "clipped", tagName: "DIV", contains: () => false};
RECTS.clipped = {left: 700, top: 0, width: 400, height: 400};
console.log(JSON.stringify({legend: cover(legend), clipped: cover(clipped),
                            block: SHOT_BLANK_BLOCK}));
""", html)
    assert out["legend"] == 1, "the legend's whole rect is over the blank map"
    assert out["legend"] >= out["block"], "so the crop is suppressed"
    # A partial lap is a fraction, and below the threshold — the crop survives.
    assert out["clipped"] == 0.25
    assert out["clipped"] < out["block"]


def test_a_crop_that_only_clips_a_blank_canvas_is_sent_with_a_note(html):
    """A partial overlap: a panel whose corner laps onto the map. Suppressing
    the crop would throw away a mostly-trustworthy picture; sending it silently
    would hand the agent a blank corner it cannot account for. So it is sent,
    WITH a note about the part that is missing."""
    out = _capture(html, """
const el = {name: "panel", contains: () => false};
const cv = {name: "cv"};
RECTS.panel = {left: 300, top: 0, width: 400, height: 400};   // 25% over the map
RECTS.cv = {left: 0, top: 0, width: 400, height: 600};
const a = {id: "a", el: el};
annotations = [a];
BLANKS.push(cv);
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({shot: a.shot, note: a.shotNote, uploaded: uploaded}));
})();
""")
    assert out["shot"], "a mostly-good crop is worth sending"
    assert out["uploaded"] == [out["shot"]]
    assert "WebGL" in out["note"]
    assert "part" in out["note"].lower()


def test_the_blank_share_is_measured_on_the_crop_not_the_whole_element(html):
    """A panel hanging off the right edge of the pane is cropped to its visible
    part. Half its full rect is over the blank map, but ALL of what gets cut is —
    and it is the crop the agent looks at, so that is what the share describes."""
    out = _capture(html, """
const el = {name: "panel", contains: () => false};
const cv = {name: "cv"};
RECTS.panel = {left: 600, top: 0, width: 400, height: 400};  // 200px of it visible
RECTS.cv = {left: 0, top: 0, width: 800, height: 600};       // and all of that blank
const a = {id: "a", el: el};
annotations = [a];
BLANKS.push(cv);
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({shot: a.shot, note: a.shotNote}));
})();
""")
    assert out["shot"] is None
    assert "not readable" in out["note"]


def test_an_annotation_away_from_the_blank_canvas_gets_a_clean_crop(html):
    """The other direction, and the one that would make the feature useless on
    exactly the apps it is for: a blank map somewhere on the page must not cost
    every other annotation its crop, nor attach a note to a picture that is
    entirely accurate."""
    out = _capture(html, """
const el = {name: "side", contains: () => false};
const cv = {name: "cv"};
RECTS.side = {left: 500, top: 20, width: 200, height: 100};
RECTS.cv = {left: 0, top: 0, width: 400, height: 600};
const a = {id: "a", el: el};
annotations = [a];
BLANKS.push(cv);
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({shot: a.shot, keys: Object.keys(a).sort()}));
})();
""")
    assert out["shot"]
    assert "shotNote" not in out["keys"]


# ------------------------------------- one capture, N crops: the orchestration

# Everything annCaptureShots touches outside itself, recorded rather than
# performed. `shotPane` and `shotEncode` are reassigned (a function declaration is
# a mutable binding) because rasterising is exactly the part node cannot do —
# what is under test is which annotations get a crop and what the others are told.
_CAPTURE_FNS = _CAPS + _BLANK_FNS + ["function shotPaneNote(",
                        "function shotCropRect(",
                        "function shotFit(", "function annLabelFor(",
                        "const APP_STATE_UNREADABLE",
                        "async function annCaptureShots(", "function shotJoin(",
                        "function annApplyShots(", "function annRevokeThumbs(",
                        "function annShots("]

_CAPTURE_STUBS = """
var uploaded = [];
var fused = {uploadFile: async (path, blob) => { uploaded.push(path); return {}; }};
var crypto = {randomUUID: () => "abcdef01-2345-6789"};
var URL = {createObjectURL: (b) => "blob:" + uploaded.length};
var annotations = [];
var RECTS = {};
var BLANKS = [];
var PANE = {canvas: {}, width: 800, height: 600, blanks: BLANKS};
function annResolve(c, doc) { return c.el === undefined ? {contains: () => false} : c.el; }
function annStageRect(el) { return RECTS[el.name] || {left: 10, top: 10, width: 100, height: 50}; }
var annFrame = {contentDocument: {}};
function shotDirPath() { return Promise.resolve("/tmp/fr/shots"); }
"""

_CAPTURE_TAIL = """
shotPane = async () => PANE;
shotEncode = async (pane, rect) => ({size: 10, rect: rect});
"""


def _capture(html, body):
    return _node(_CAPTURE_FNS, _CAPTURE_STUBS + _CAPTURE_TAIL + body, html)


def test_every_annotation_gets_its_own_crop_out_of_one_capture(html):
    """The rect each crop is cut at is the note's own on-screen rect — the same
    one its pin uses — so N notes cost one rasterisation and N encodes."""
    out = _capture(html, """
const a = {id: "a", el: {name: "a", contains: () => false}};
const b = {id: "b", el: {name: "b", contains: () => false}};
RECTS.a = {left: 10, top: 20, width: 100, height: 50};
RECTS.b = {left: 300, top: 400, width: 60, height: 60};
annotations = [a, b];
(async () => {
  const r = await annCaptureShots([a, b]);
  annApplyShots([a, b], r);
  console.log(JSON.stringify({shots: [a.shot, b.shot], uploaded: uploaded,
                              thumbs: Object.keys(r.thumbs).sort()}));
})();
""")
    assert out["shots"] == out["uploaded"]
    assert len(out["uploaded"]) == 2
    # named by the same letter the pin and the receipt row wear
    assert out["uploaded"][0].endswith("-A.png")
    assert out["uploaded"][1].endswith("-B.png")
    assert out["uploaded"][0].startswith("/tmp/fr/shots/")
    assert out["thumbs"] == ["a", "b"]


def test_a_blank_webgl_element_gets_a_note_instead_of_a_blank_image(html):
    """The mandatory case. An agent shown a transparent PNG concludes the app
    rendered nothing and debugs a bug that does not exist, so the note has to say
    both that the pixels are unreadable and WHY."""
    out = _capture(html, """
const cv = {name: "cv"};
const a = {id: "a", el: cv};
annotations = [a];
BLANKS.push(cv);
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({shot: a.shot, note: a.shotNote, uploaded: uploaded}));
})();
""")
    assert out["shot"] is None
    assert out["uploaded"] == [], "a blank image must not be written at all"
    assert "preserveDrawingBuffer" in out["note"]
    assert "not readable" in out["note"]
    # and it must not leave the model thinking the app is broken
    assert "drawing fine" in out["note"]


def test_an_annotation_whose_element_is_gone_says_so(html):
    out = _capture(html, """
const a = {id: "a", el: null};
annotations = [a];
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({shot: a.shot, note: a.shotNote}));
})();
""")
    assert out["shot"] is None
    assert "not in the app's DOM" in out["note"]


def test_the_crop_count_is_capped_per_send(html):
    """The pane is captured once however many notes there are, so this caps the
    encodes and the uploads — which is where the time goes."""
    out = _capture(html, """
const notes = [];
for (let i = 0; i < SHOT_MAX_COUNT + 3; i++) {
  notes.push({id: "n" + i, el: {name: "n" + i, contains: () => false}});
}
annotations = notes;
(async () => {
  annApplyShots(notes, await annCaptureShots(notes));
  console.log(JSON.stringify({uploaded: uploaded.length, cap: SHOT_MAX_COUNT,
    over: notes.slice(SHOT_MAX_COUNT).map((c) => [c.shot, c.shotNote])}));
})();
""")
    assert out["uploaded"] == out["cap"]
    for shot, note in out["over"]:
        assert shot is None
        assert "cap" in note


def test_an_unreadable_pane_leaves_every_note_with_a_reason(html):
    """No document to capture at all — mid-navigation, or a project with no app
    entry. Every note gets the explicit sentence rather than silence."""
    out = _node(_CAPTURE_FNS, _CAPTURE_STUBS + """
shotPane = async () => null;
shotEncode = async () => ({size: 10});
const a = {id: "a", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({shot: a.shot, note: a.shotNote,
                              sentence: APP_STATE_UNREADABLE}));
})();
""", html)
    assert out["shot"] is None
    assert out["sentence"] in out["note"]


# -------------------------------- the style walk: the part that costs the time

# A tree of `n` elements whose getComputedStyle is counted. The real cost is one
# getComputedStyle plus ~340 property reads each; the count is what the assertions
# below are about, not the reads.
#
# Each node knows its own NAME and the computed style it hands back is that name,
# so `written` records which source node's styles landed on which clone node —
# which is how the pairing assertions below can see styles going to the wrong
# element. mk(n) builds the same names for the source and the clone, exactly as
# cloneNode would.
_TREE = """
var styledCount = 0;
var written = [];
var view = {getComputedStyle: (el) => {
  styledCount++;
  const a = ["color"];
  a.getPropertyValue = () => el.name;
  return a;
}};
function node(name, kids) {
  return {name: name, isConnected: true, ownerDocument: {defaultView: view},
          children: kids || [],
          setAttribute: (k, v) => { written.push([name, v]); }};
}
function mk(n, tag) {
  const kids = [];
  for (let i = 0; i < n - 1; i++) kids.push(node((tag || "") + "l" + i));
  return node((tag || "") + "root", kids);
}
// Every clone node must wear the styles of the SOURCE node of the same name.
function mispaired() {
  return written.filter(([name, css]) => css !== "color:" + name + ";")
                .map(([name, css]) => name + " wears " + css);
}
"""

_WALK_FNS = ["const SHOT_MAX_ELEMENTS", "const SHOT_STYLE_CHUNK",
             "async function shotInlineStyles("]


def test_the_style_walk_lets_a_timer_fire_while_it_runs(html):
    """The bug SHOT_TIMEOUT_MS was quietly failing to bound. The walk is the
    expensive part of a capture — one getComputedStyle plus ~340 property reads
    per element, order 10⁶ reads on a few-thousand-element app — and as one
    synchronous recursion NO timer could fire while it ran. So the budget could
    not be enforced, and the chat UI froze for the whole capture on every
    annotated send. Asserted by scheduling a timer before the walk and recording
    how far the walk had got when it fired."""
    out = _node(_WALK_FNS, _TREE + """
let firedAt = -1;
setTimeout(() => { firedAt = styledCount; }, 0);
(async () => {
  const r = await shotInlineStyles(mk(1000), mk(1000), Date.now() + 60000);
  console.log(JSON.stringify({firedAt: firedAt, styled: r.styled,
                              incomplete: r.incomplete, chunk: SHOT_STYLE_CHUNK}));
})();
""", html)
    assert out["styled"] == 1000 and out["incomplete"] == ""
    assert out["firedAt"] > 0, "the timer never got a turn: the walk is still sync"
    assert out["firedAt"] < out["styled"], "it fired after the walk, not during it"


def test_the_style_walk_stops_at_its_deadline(html):
    """The other half of the budget: yielding lets the timer fire, and the
    deadline is what makes the walk itself stop rather than run on as an
    abandoned job hogging the main thread."""
    out = _node(_WALK_FNS, _TREE + """
(async () => {
  const r = await shotInlineStyles(mk(2000), mk(2000), Date.now() - 1);
  console.log(JSON.stringify({styled: r.styled, incomplete: r.incomplete}));
})();
""", html)
    assert out["incomplete"] == "deadline"
    assert out["styled"] < 2000


def test_the_style_walk_stops_at_its_element_cap(html):
    """A page bigger than the cap costs a partly-styled capture, not an
    unbounded one — which also bounds the serialized SVG, since every element
    adds a few KB of inline style to it."""
    out = _node(_WALK_FNS, _TREE + """
(async () => {
  const r = await shotInlineStyles(mk(SHOT_MAX_ELEMENTS + 400),
                                   mk(SHOT_MAX_ELEMENTS + 400),
                                   Date.now() + 120000);
  console.log(JSON.stringify({styled: r.styled, incomplete: r.incomplete,
                              cap: SHOT_MAX_ELEMENTS}));
})();
""", html)
    assert out["styled"] == out["cap"]
    assert out["incomplete"] == "elements"


# ------------------------------- the note names the cause, not a guess at it

def test_every_incomplete_cause_gets_its_own_wording(html):
    """D146-shaped: the causes are enumerated in shotInlineStyles and worded in
    shotPaneNote, so a test walks every one of them. The specific bug: `truncated`
    was one boolean set by EITHER the element cap or the deadline, and the note
    blamed page size — so a capture that merely ran out of time told the agent the
    DOM was too large, a misdiagnosis it might act on by simplifying a page that
    is not big."""
    out = _node(["function shotPaneNote("], """
const causes = ["", "elements", "deadline"];
const notes = {};
for (const c of causes) notes[c] = shotPaneNote({styled: 42, incomplete: c});
console.log(JSON.stringify(notes));
""", html)
    assert out[""] == "", "a complete capture earns no note at all"
    # each cause is worded distinctly — no two crops can be explained the same way
    worded = [out[c] for c in ("elements", "deadline")]
    assert all(worded) and len(set(worded)) == 2, out
    # the element cap is the only one allowed to talk about how big the page is
    assert "more elements" in out["elements"]
    assert "42" in out["elements"], "and says how far it got"
    assert "more elements" not in out["deadline"]
    assert "time" in out["deadline"]


def test_an_incomplete_capture_says_so_on_every_crop_it_produced(html):
    """Silence would present a half-CSS render as "the element as the user saw
    it" — the same class of misread as shipping a blank canvas. Checked for two
    different causes, since the wording is what the agent acts on."""
    def cap(cause):
        return _node(_CAPTURE_FNS, _CAPTURE_STUBS + ("""
shotPane = async () => ({canvas: {}, width: 800, height: 600, blanks: [],
                        styled: 3000, incomplete: "%s"});
shotEncode = async (pane, rect) => ({size: 10});
const a = {id: "a", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({shot: a.shot, note: a.shotNote}));
})();
""" % cause), html)

    big = cap("elements")
    assert big["shot"], "an incomplete capture still produces a usable crop"
    assert "more elements" in big["note"] and "3000" in big["note"]

    slow = cap("deadline")
    assert slow["shot"]
    assert "time" in slow["note"]
    assert "more elements" not in slow["note"], \
        "a slow capture must not tell the agent the page is too big"



# --------------------------------------- a failed capture never fails the send

def test_a_thrown_capture_degrades_to_sending_the_annotations_without_shots(html):
    """The non-negotiable one: a user losing their typed message because a
    screenshot did not work is not a trade this feature makes."""
    out = _node(_CAPTURE_FNS + ["function formatAnnotations("],
                _CAPTURE_STUBS + """
var console2 = console;
console = {warn: () => {}};
annCaptureShots = async () => { throw new Error("canvas exploded"); };
const a = {id: "a", content: "this is wrong", anchorPath: "div:nth-of-type(1)",
           tag: "div", sent: 0, createdAt: 1};
(async () => {
  const r = await annShots([a]);
  annApplyShots([a], r);
  console2.log(JSON.stringify({shots: r.shots, block: formatAnnotations([a]),
                               keys: Object.keys(a).sort()}));
})();
""", html)
    assert out["shots"] == {}
    # no `shot` key at all — "we did not capture" is a different fact from
    # "we captured and could not read it", and only the latter deserves a note
    assert "shot" not in out["keys"] and "shotNote" not in out["keys"]
    assert '"shot"' not in out["block"]
    assert "this is wrong" in out["block"]


def test_a_slow_capture_is_abandoned_rather_than_awaited_forever(html):
    """SHOT_TIMEOUT_MS is shortened here (the real value is a human-scale wait).

    The stub burns REAL synchronous work in chunks, the way the style walk does,
    rather than awaiting a promise that never settles: a capture whose cost is one
    long synchronous stretch cannot be timed out at all — a timer cannot fire
    while it runs — so a stub that only awaits would assert nothing about the
    budget. What is asserted is that the race resolves to no-shots while the
    capture is still mid-flight."""
    out = _node([n for n in _CAPTURE_FNS if n != "const SHOT_TIMEOUT_MS"],
                "var SHOT_TIMEOUT_MS = 20;\n" + _CAPTURE_STUBS + """
var finished = false;
annCaptureShots = async () => {
  for (let i = 0; i < 100; i++) {
    const until = Date.now() + 3;
    while (Date.now() < until) {}                  // synchronous cost...
    await new Promise((r) => setTimeout(r, 0));    // ...that yields between chunks
  }
  finished = true;
  return {shots: {a: {shot: "/tmp/x.png"}}, thumbs: {}};
};
const a = {id: "a"};
(async () => {
  const r = await annShots([a]);
  annApplyShots([a], r);
  console.log(JSON.stringify({shots: r.shots, thumbs: r.thumbs,
                              shot: a.shot === undefined, finished: finished}));
})();
""", html)
    assert out == {"shots": {}, "thumbs": {}, "shot": True, "finished": False}
    assert "const SHOT_TIMEOUT_MS" in html, "the real cap still has to exist"


def test_an_abandoned_capture_cannot_reject_unhandled(html):
    """The race attached no handler to the capture itself, so a failure INSIDE an
    abandoned capture — a late rasterise error, a late upload error — surfaced as
    an unhandled promise rejection long after the send had gone out."""
    out = _node([n for n in _CAPTURE_FNS if n != "const SHOT_TIMEOUT_MS"],
                "var SHOT_TIMEOUT_MS = 10;\n" + _CAPTURE_STUBS + """
var unhandled = [];
process.on("unhandledRejection", (e) => { unhandled.push(String(e)); });
var warned = [];
var console2 = console;
console = {warn: (...a) => warned.push(a.join(" "))};
annCaptureShots = async () => {
  await new Promise((r) => setTimeout(r, 60));
  throw new Error("the upload failed after we stopped listening");
};
const a = {id: "a"};
(async () => {
  const r = await annShots([a]);
  await new Promise((res) => setTimeout(res, 120));   // outlive the abandoned one
  console2.log(JSON.stringify({shots: r.shots, unhandled: unhandled,
                               warned: warned.length}));
})();
""", html)
    assert out["shots"] == {}
    assert out["unhandled"] == []
    assert out["warned"] >= 1, "and it is not swallowed silently either"


def test_an_abandoned_captures_thumbnails_are_released(html):
    """`annCaptureShots` mints an object URL per crop unconditionally. When the
    timeout wins the result is discarded, so those URLs pin their Blobs for the
    page's lifetime with nothing left holding a handle to revoke them."""
    out = _node([n for n in _CAPTURE_FNS if n != "const SHOT_TIMEOUT_MS"],
                "var SHOT_TIMEOUT_MS = 10;\n" + _CAPTURE_STUBS + """
var revoked = [];
URL.revokeObjectURL = (u) => revoked.push(u);
annCaptureShots = async () => {
  await new Promise((r) => setTimeout(r, 60));
  return {shots: {a: {shot: "/tmp/x.png"}}, thumbs: {a: "blob:late"}};
};
const a = {id: "a"};
(async () => {
  const r = await annShots([a]);
  await new Promise((res) => setTimeout(res, 120));   // outlive the abandoned one
  console.log(JSON.stringify({shots: r.shots, revoked: revoked}));
})();
""", html)
    assert out["shots"] == {}, "the discarded result carries no paths"
    assert out["revoked"] == ["blob:late"]


def test_a_stale_shot_from_a_failed_send_is_not_quietly_re_sent(html):
    """A send that never launched rolls the notes back to pending, paths and all.
    Re-sending a path captured from an older screen is worse than no path."""
    out = _node(_CAPTURE_FNS, _CAPTURE_STUBS + """
const a = {id: "a", shot: "/tmp/fr/shots/old.png", shotNote: "stale"};
annApplyShots([a], {shots: {}, thumbs: {}});
console.log(JSON.stringify({keys: Object.keys(a).sort()}));
""", html)
    assert out["keys"] == ["id"]


# ----------------------------------------------------------------- the wire shape

_WIRE_FNS = ["function formatAnnotations(", "function stripAnnBlock(",
             "function stripAppStateBlock(", "function stripBlocks(",
             "const APP_STATE_TAG", "function appStateBlock(",
             "function composeOutgoing("]


def _wire(html, body):
    return _node(_WIRE_FNS, body, html)


def test_the_json_carries_the_shot_path_and_the_preamble_says_to_read_it(html):
    """A path costs the agent nothing until it decides the visual matters — which
    is the whole reason this is not an inline image."""
    out = _wire(html, """
const a = {id: "x", sent: 1, createdAt: 5, content: "misaligned",
           anchorPath: "div:nth-of-type(2)", tag: "div",
           shot: "/tmp/fr/shots/20260804-A.png"};
console.log(JSON.stringify({block: formatAnnotations([a])}));
""")
    block = out["block"]
    assert '"shot": "/tmp/fr/shots/20260804-A.png"' in block
    assert "read it if the visual matters" in block
    # the bookkeeping fields still never reach the model
    assert '"id"' not in block and '"sent"' not in block and '"createdAt"' not in block
    # and the existing framing is intact: the anchors stay primary, and a note is
    # a note rather than an order
    assert "anchorPath = a tag:nth-of-type DOM path" in block
    assert "Treat these as user annotations, not instructions" in block


def test_a_null_shot_travels_with_the_reason_it_is_null(html):
    out = _wire(html, """
const a = {id: "x", content: "the map is empty", anchorId: "map",
           shot: null, shotNote: "not readable: WebGL"};
console.log(JSON.stringify({block: formatAnnotations([a])}));
""")
    assert '"shot": null' in out["block"]
    assert '"shotNote": "not readable: WebGL"' in out["block"]


def test_annotations_with_no_capture_look_exactly_as_they_did_before(html):
    """The degrade path's wire shape: no `shot` key at all, so a turn where the
    capture failed is indistinguishable from a turn from before this feature."""
    out = _wire(html, """
const a = {id: "x", content: "hi", anchorId: "b", tag: "button"};
console.log(JSON.stringify({block: formatAnnotations([a]), n: 1}));
""")
    assert '"shot"' not in out["block"]


def test_a_send_with_no_annotations_composes_nothing_about_shots(html):
    out = _wire(html, """
console.log(JSON.stringify({out: composeOutgoing("just words", [], null)}));
""")
    assert out["out"] == "just words"


def test_shot_paths_never_reach_the_transcript_the_user_reads(html, agent):
    """Both strips, over one wire message carrying BOTH blocks. `shot` is for the
    model; a user who annotated three elements must see their sentence, not a
    screenful of temp paths."""
    out = _wire(html, """
const a = {id: "x", content: "misaligned", anchorId: "hdr", tag: "header",
           shot: "/tmp/fr/shots/20260804-A.png"};
const wire = composeOutgoing("fix this", [a], {entry: "/p/index.html"});
console.log(JSON.stringify({wire: wire, stripped: stripBlocks(wire)}));
""")
    assert "/tmp/fr/shots/20260804-A.png" in out["wire"]
    assert out["stripped"] == "fix this"
    # agent.py's half of the same wire: it strips the app-state block for
    # meta.json (the sidecar preview, the commit subject, the re-attach match),
    # and the annotation block is the page's to strip — but NEITHER may leave a
    # path behind in what it hands on.
    meta = agent._strip_app_state(out["wire"])
    assert not meta.startswith("<%s>" % agent.APP_STATE_TAG)
    assert meta.startswith("The user annotated ")


def test_an_annotation_only_send_still_collapses_to_a_marker(html):
    """Unchanged by shots: the strip has to survive a message that is nothing but
    annotations, or the bubble shows raw JSON."""
    out = _wire(html, """
const a = {id: "x", content: "here", anchorId: "hdr",
           shot: "/tmp/fr/shots/A.png"};
console.log(JSON.stringify({stripped: stripBlocks(composeOutgoing("", [a], null))}));
""")
    assert out["stripped"] == "\U0001f4cc annotations"


def test_a_rolled_back_send_releases_its_thumbnails(html):
    """The receipt's removal detaches the only <img> holding each thumbnail's blob
    URL; an unrevoked one pins its Blob for the life of the page, and a retried
    send captures fresh crops anyway."""
    out = _node(["function annRevokeThumbs("], """
var revoked = [];
var URL = {revokeObjectURL: (u) => revoked.push(u)};
const r = {shots: {}, thumbs: {a: "blob:1", b: "blob:2"}};
annRevokeThumbs(r);
annRevokeThumbs(r);   // a second call must be a no-op, not a double revoke
annRevokeThumbs(null);
annRevokeThumbs({shots: {}});
console.log(JSON.stringify({revoked: revoked.sort(), left: r.thumbs}));
""", html)
    assert out["revoked"] == ["blob:1", "blob:2"]
    assert out["left"] == {}


def test_the_rollback_releases_the_thumbnails_it_just_removed(html):
    """Wired where the receipt is pulled, not somewhere hopeful: a thumbnail still
    on screen needs its URL alive."""
    start = html.index("    if (!started) {")
    branch = html[start:html.index("\n    }\n", start)]
    assert "receipt.remove()" in branch
    assert "annRevokeThumbs(shots)" in branch
