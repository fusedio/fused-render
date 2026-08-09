"""claude's annotation screenshots: a PNG crop of the element the user
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

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")
TEMPLATE = os.path.join(TEMPLATE_DIR, "template.html")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_shots_" + name, path)
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

    Same extraction as tests/test_claude_app_state.py's `_node`: what
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


_ENCODE_FNS = _CAPS + ["const SHOT_WEBP_QUALITY", "function shotFit(",
                       "function shotBlob(", "function shotExt(",
                       "let shotWebpOk", "async function shotEncode("]

# A canvas whose toBlob behaves like a real one — including the way WKWebView
# behaves, which is the whole point of this group: asked for webp it hands back a
# PNG blob, at byte-identical size, with no throw and no null.
_ENCODE_STUBS = """
var calls = [];
var WEBP_REAL = true;
var SIZE = (type, q, w) => 10;
var document = {createElement: () => ({
  width: 0, height: 0,
  getContext: () => ({drawImage: () => {}}),
  toBlob: function (cb, type, q) {
    calls.push({type: type, q: q === undefined ? null : q, w: this.width});
    const got = (type === "image/webp" && WEBP_REAL) ? "image/webp" : "image/png";
    cb({type: got, size: SIZE(got, q, this.width)});
  },
})};
var PANE = {canvas: {}, width: 1600, height: 1000};
"""


def _encode(html, body):
    return _node(_ENCODE_FNS, _ENCODE_STUBS + body, html)


def test_a_shot_is_named_from_the_bytes_it_holds_not_the_format_asked_for(html):
    """The one rule that makes trying WebP safe at all. WKWebView cannot encode
    WebP and fails SILENTLY: `toBlob(cb, "image/webp", q)` yields a blob whose
    `type` is "image/png", at byte-identical size, with no throw and no null. A
    file named `.webp` from what we ASKED for would hold PNG bytes — worse than
    never trying — so the extension comes off `blob.type`."""
    out = _encode(html, """
WEBP_REAL = false;                       // this is WKWebView
(async () => {
  const blob = await shotEncode(PANE, {left: 0, top: 0, width: 640, height: 400});
  console.log(JSON.stringify({type: blob.type, ext: shotExt(blob),
                              asked: calls.map((c) => c.type)}));
})();
""")
    assert out["type"] == "image/png"
    assert out["ext"] == ".png", "a PNG blob is a .png file whatever we requested"
    # it did try, once, and then stopped asking
    assert out["asked"] == ["image/webp", "image/png"]


def test_where_webp_is_real_the_file_is_a_webp(html):
    out = _encode(html, """
(async () => {
  const blob = await shotEncode(PANE, {left: 0, top: 0, width: 640, height: 400});
  console.log(JSON.stringify({type: blob.type, ext: shotExt(blob),
                              asked: calls.map((c) => [c.type, c.q])}));
})();
""")
    assert out["ext"] == ".webp"
    # and it costs ONE encode when the first quality already fits
    assert out["asked"] == [["image/webp", 0.8]]


def test_a_silent_webp_failure_is_only_discovered_once(html):
    """Probing per crop would double the encodes for every WKWebView user, on a
    capability that cannot change mid-session."""
    out = _encode(html, """
WEBP_REAL = false;
(async () => {
  for (let i = 0; i < 4; i++) {
    await shotEncode(PANE, {left: 0, top: 0, width: 640, height: 400});
  }
  console.log(JSON.stringify({asked: calls.map((c) => c.type), ok: shotWebpOk}));
})();
""")
    assert out["ok"] is False
    assert out["asked"] == ["image/webp"] + ["image/png"] * 4, out["asked"]


def test_quality_steps_down_before_resolution_is_halved(html):
    """The reason to prefer WebP at all: PNG has no quality dial, so the only knob
    was resolution — and halving 640px to 320px to 160px destroys the legibility
    that is the entire point of sending a picture. Where WebP is real, every
    quality step is spent at full size before a single pixel is given up."""
    out = _encode(html, """
// Nothing fits until the image is halved, so every knob gets exercised.
SIZE = (type, q, w) => (w > 320 ? 999999 : 10);
(async () => {
  const blob = await shotEncode(PANE, {left: 0, top: 0, width: 640, height: 400});
  console.log(JSON.stringify({tried: calls.map((c) => [c.type, c.q, c.w]),
                              ext: shotExt(blob), qualities: SHOT_WEBP_QUALITY}));
})();
""")
    tried = out["tried"]
    widths = [w for _t, _q, w in tried]
    # the first resolution is tried with every quality AND with png before the
    # first halving
    first = [[t, q] for t, q, w in tried if w == widths[0]]
    assert first == [["image/webp", 0.8], ["image/webp", 0.6], ["image/png", None]], first
    assert widths[0] > widths[-1], "and only then did resolution give way"
    assert sorted(widths, reverse=True) == widths, "resolution never goes back up"


def test_the_png_only_path_is_exactly_what_it_was(html):
    """WKWebView users must get today's behaviour, not a degraded version of it:
    same halve-and-retry, same three attempts, same null when nothing fits."""
    out = _encode(html, """
WEBP_REAL = false;
SIZE = () => 999999;                     // nothing ever fits
(async () => {
  const blob = await shotEncode(PANE, {left: 0, top: 0, width: 640, height: 400});
  console.log(JSON.stringify({blob: blob,
    pngWidths: calls.filter((c) => c.type === "image/png").map((c) => c.w)}));
})();
""")
    assert out["blob"] is None, "over budget even halved: no shot beats a huge one"
    assert out["pngWidths"] == [640, 320, 160], out["pngWidths"]


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
_CAPTURE_FNS = _CAPS + _BLANK_FNS + ["function shotPaneNote(", "function shotTrustLine(",
                        "function shotExt(", "function shotCropRect(",
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
var LAST_ENCODE = {};
shotPane = async () => PANE;
shotEncode = async (pane, rect, limits) => {
  LAST_ENCODE = {rect: rect, limits: limits};
  return {size: 10, rect: rect};
};
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


def test_a_crop_that_encoded_as_webp_is_uploaded_under_a_webp_name(html):
    """End to end, because the naming is where a silent WebP failure would do its
    damage: the path in the annotation JSON is the one the agent Reads, and a
    `.webp` holding PNG bytes is a file it cannot open."""
    out = _capture(html, """
shotEncode = async () => ({size: 10, type: "image/webp"});
const a = {id: "a", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({shot: a.shot, uploaded: uploaded}));
})();
""")
    assert out["shot"].endswith("-A.webp")
    assert out["uploaded"] == [out["shot"]]


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
                              incomplete: r.incomplete, chunk: SHOT_STYLE_CHUNK,
                              mispaired: mispaired()}));
})();
""", html)
    assert out["styled"] == 1000 and out["incomplete"] == ""
    assert out["mispaired"] == [], "an undisturbed walk pairs every node correctly"
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


# ------------------- the walk yields, so the live DOM can move underneath it

def test_a_node_detached_mid_walk_is_skipped_rather_than_read(html):
    """Yielding bought boundedness and gave up atomicity: between chunks the app
    can re-render. getComputedStyle on a node that has left the document returns
    empty/meaningless values (verified in a real browser: a detached subtree
    enumerates ZERO properties), so reading one would write an authoritative
    "this element has no styling" onto the clone."""
    out = _node(_WALK_FNS, _TREE + """
const src = mk(400), dst = mk(400);
// At the walk's first yield, everything past the first chunk leaves the document.
setTimeout(() => { src.children.forEach((k) => { k.isConnected = false; }); }, 0);
(async () => {
  const r = await shotInlineStyles(src, dst, Date.now() + 60000);
  console.log(JSON.stringify({styled: r.styled, incomplete: r.incomplete,
                              mispaired: mispaired()}));
})();
""", html)
    assert out["incomplete"] == "detached"
    assert out["styled"] < 400, "the detached nodes were not read"
    assert out["mispaired"] == []


def test_a_frame_that_navigated_mid_walk_does_not_throw(html):
    """`s.ownerDocument.defaultView` is null once the frame navigates, which used
    to be a TypeError out of the middle of a capture rather than a degradation."""
    out = _node(_WALK_FNS, _TREE + """
const src = mk(400), dst = mk(400);
setTimeout(() => { src.children.forEach((k) => { k.ownerDocument = {defaultView: null}; }); }, 0);
(async () => {
  const r = await shotInlineStyles(src, dst, Date.now() + 60000);
  console.log(JSON.stringify({styled: r.styled, incomplete: r.incomplete}));
})();
""", html)
    assert out["incomplete"] == "detached"
    assert out["styled"] < 400


def test_children_that_stopped_corresponding_are_never_paired(html):
    """The worse symptom, and the one a detachment test cannot show. The clone was
    taken from this very tree, so a child list that no longer matches it means the
    app re-rendered — and pairing a[i] with b[i] past that point lands each live
    node's styles on a DIFFERENT clone node. Demonstrated in a real browser before
    this fix: 375 clone nodes wearing another element's computed style, with
    nothing reported."""
    out = _node(_WALK_FNS, _TREE + """
// 300 leaves so the walk yields (chunk is 200) before it reaches the wrapper,
// whose live children lose their FIRST entry mid-walk — so a naive a[i]/b[i]
// pairing shifts every one of them onto the wrong clone node.
const kids = [];
for (let i = 0; i < 300; i++) kids.push(node("l" + i));
const inner = ["w0", "w1", "w2"].map((n) => node(n));
kids.push(node("w", inner));
const src = node("root", kids);
const dstKids = [];
for (let i = 0; i < 300; i++) dstKids.push(node("l" + i));
dstKids.push(node("w", ["w0", "w1", "w2"].map((n) => node(n))));
const dst = node("root", dstKids);
setTimeout(() => { inner.shift(); }, 0);
(async () => {
  const r = await shotInlineStyles(src, dst, Date.now() + 60000);
  console.log(JSON.stringify({incomplete: r.incomplete, mispaired: mispaired(),
                              styled: r.styled}));
})();
""", html)
    assert out["mispaired"] == [], "a style must never land on the wrong element"
    assert out["incomplete"] == "mutated"
    # the wrapper itself is styled; only its now-unpairable children are skipped
    assert out["styled"] == 302


def test_a_mid_walk_re_render_that_keeps_the_shape_is_still_reported(html):
    """The case a structural check cannot catch: a list re-rendered with the same
    number of children. Node counts still match, so the pairing looks sound while
    every pair may be wrong — only an observer on the source can see it. (This is
    half of the real-browser repro: 400 wrappers, half losing a child and half
    rotating one, and the rotations are invisible to the length check.)"""
    out = _node(_WALK_FNS, _TREE + """
var moFire = null;
view.MutationObserver = function (cb) {
  this.observe = () => { moFire = cb; };
  this.disconnect = () => {};
  this.takeRecords = () => [];
};
const src = mk(400), dst = mk(400);
// The app re-renders between chunks: same shape, different nodes.
setTimeout(() => { if (moFire) moFire([{type: "childList"}]); }, 0);
(async () => {
  const r = await shotInlineStyles(src, dst, Date.now() + 60000);
  console.log(JSON.stringify({styled: r.styled, incomplete: r.incomplete}));
})();
""", html)
    assert out["incomplete"] == "mutated"
    # It cannot un-style what it already did, so it finishes and reports instead
    # of throwing the work away.
    assert out["styled"] == 400


def test_a_reordered_child_list_is_not_paired_by_index(html):
    """The rotation case, which is where reporting alone was not enough. A row
    whose first child moves to the end keeps its child COUNT, so the structural
    check waves it through and every one of its children then wears its
    neighbour's styles. Measured in a real browser with only the count check: 225
    clone nodes wearing another element's computed style — reported, but wrong. A
    style landing on the wrong element is a lie about the page, so the subtree is
    dropped instead."""
    out = _node(_WALK_FNS, _TREE + """
var moFire = null;
view.MutationObserver = function (cb) {
  this.observe = () => { moFire = cb; };
  this.disconnect = () => {};
  this.takeRecords = () => [];
};
// 300 leaves so the walk yields before reaching the wrapper.
const kids = [];
for (let i = 0; i < 300; i++) kids.push(node("l" + i));
const inner = ["w0", "w1", "w2"].map((n) => node(n));
const wrap = node("w", inner);
kids.push(wrap);
const src = node("root", kids);
const dstKids = [];
for (let i = 0; i < 300; i++) dstKids.push(node("l" + i));
dstKids.push(node("w", ["w0", "w1", "w2"].map((n) => node(n))));
const dst = node("root", dstKids);
setTimeout(() => {
  inner.push(inner.shift());          // rotate: same count, different order
  moFire([{type: "childList", target: wrap}]);
}, 0);
(async () => {
  const r = await shotInlineStyles(src, dst, Date.now() + 60000);
  console.log(JSON.stringify({incomplete: r.incomplete, mispaired: mispaired(),
                              styled: r.styled}));
})();
""", html)
    assert out["mispaired"] == [], "a rotated row must not be paired by index"
    assert out["incomplete"] == "mutated"
    assert out["styled"] == 302, "the wrapper is styled; its children are dropped"


def test_a_pair_formed_before_a_re_render_is_still_honoured(html):
    """The other side of the same reasoning, and why the fix is not "abandon the
    capture": a queued pair holds direct node references, so a pairing made before
    the app re-rendered is still that node's own clone however the live tree is
    shuffled afterwards. Throwing those away would cost the whole page's styling
    for one late mutation somewhere else."""
    out = _node(_WALK_FNS, _TREE + """
var moFire = null;
view.MutationObserver = function (cb) {
  this.observe = () => { moFire = cb; };
  this.disconnect = () => {};
  this.takeRecords = () => [];
};
const src = mk(400), dst = mk(400);
// Everything is already paired by the time this lands (the root was processed in
// the first chunk), and it names a node that is not an ancestor of anything left.
setTimeout(() => { moFire([{type: "childList", target: node("elsewhere")}]); }, 0);
(async () => {
  const r = await shotInlineStyles(src, dst, Date.now() + 60000);
  console.log(JSON.stringify({styled: r.styled, incomplete: r.incomplete,
                              mispaired: mispaired()}));
})();
""", html)
    assert out["styled"] == 400, "already-queued pairs are still styled"
    assert out["mispaired"] == []
    assert out["incomplete"] == "mutated", "and the re-render is still reported"


def test_a_mutation_seen_only_at_the_end_is_still_reported(html):
    """An observer's callback is a microtask, so a mutation in the final chunk may
    not have been delivered when the loop ends. takeRecords is what closes that
    window."""
    out = _node(_WALK_FNS, _TREE + """
var pending = [{type: "childList"}];
view.MutationObserver = function (cb) {
  this.observe = () => {};
  this.disconnect = () => {};
  this.takeRecords = () => pending.splice(0);
};
(async () => {
  const r = await shotInlineStyles(mk(50), mk(50), Date.now() + 60000);
  console.log(JSON.stringify({styled: r.styled, incomplete: r.incomplete}));
})();
""", html)
    assert out["incomplete"] == "mutated"
    assert out["styled"] == 50


def test_a_walk_with_no_observer_available_still_completes(html):
    """MutationObserver is the precision, not the requirement: where it is absent
    the structural check is still there and the walk must not throw."""
    out = _node(_WALK_FNS, _TREE + """
(async () => {
  const r = await shotInlineStyles(mk(50), mk(50), Date.now() + 60000);
  console.log(JSON.stringify({styled: r.styled, incomplete: r.incomplete,
                              mispaired: mispaired()}));
})();
""", html)
    assert out == {"styled": 50, "incomplete": "", "mispaired": []}


def test_a_correctness_problem_is_reported_over_a_mere_budget_one(html):
    """Both can be true at once, and the note has room for one cause. A re-render
    means styles may be WRONG anywhere in the capture; a cap means styles are
    merely MISSING past a point. The user of this field is an agent deciding
    whether to trust the picture, so the correctness news wins."""
    out = _node(_WALK_FNS, _TREE + """
var moFire = null;
view.MutationObserver = function (cb) {
  this.observe = () => { moFire = cb; };
  this.disconnect = () => {};
  this.takeRecords = () => [];
};
const src = mk(SHOT_MAX_ELEMENTS + 400), dst = mk(SHOT_MAX_ELEMENTS + 400);
setTimeout(() => { if (moFire) moFire([{type: "childList"}]); }, 0);
(async () => {
  const r = await shotInlineStyles(src, dst, Date.now() + 120000);
  console.log(JSON.stringify({styled: r.styled, incomplete: r.incomplete,
                              cap: SHOT_MAX_ELEMENTS}));
})();
""", html)
    assert out["styled"] == out["cap"], "the cap still fired"
    assert out["incomplete"] == "mutated", "but the re-render is the worse news"


# ------------------------------- the note names the cause, not a guess at it

def test_every_incomplete_cause_gets_its_own_wording(html):
    """D146-shaped: the causes are enumerated in shotInlineStyles and worded in
    shotPaneNote, so a test walks every one of them. The specific bug: `truncated`
    was one boolean set by EITHER the element cap or the deadline, and the note
    blamed page size — so a capture that merely ran out of time told the agent the
    DOM was too large, a misdiagnosis it might act on by simplifying a page that
    is not big."""
    out = _node(["function shotPaneNote("], """
const causes = ["", "elements", "deadline", "detached", "mutated"];
const notes = {};
for (const c of causes) notes[c] = shotPaneNote({styled: 42, incomplete: c});
console.log(JSON.stringify(notes));
""", html)
    assert out[""] == "", "a complete capture earns no note at all"
    # each cause is worded distinctly — no two crops can be explained the same way
    worded = [out[c] for c in ("elements", "deadline", "detached", "mutated")]
    assert all(worded) and len(set(worded)) == 4, out
    # the element cap is the only one allowed to talk about how big the page is
    assert "more elements" in out["elements"]
    assert "42" in out["elements"], "and says how far it got"
    for c in ("deadline", "detached", "mutated"):
        assert "more elements" not in out[c], (c, out[c])
    assert "time" in out["deadline"]
    # the two correctness causes have to say the picture may not match the screen,
    # which is a different warning from "some of it is unstyled"
    assert "re-render" in out["mutated"] or "changed" in out["mutated"]
    for c in ("detached", "mutated"):
        assert "while the capture was running" in out[c], (c, out[c])


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

    mutated = cap("mutated")
    assert "while the capture was running" in mutated["note"]


# ------------------------- how much of a caveated shot the agent should trust

def test_a_re_render_warning_does_not_also_call_the_crop_trustworthy(html):
    """The finding-2 contract biting from the other side. `shotNote` may ride a
    real `shot` — but only if the note and its closing line agree about HOW MUCH
    to trust. A `mutated` capture's note says the picture may not match the screen,
    and the standard closer said "the rest of the crop is what the user saw": one
    instruction to distrust and one to trust, which cancel to nothing."""
    out = _node(_CAPTURE_FNS, _CAPTURE_STUBS + """
shotPane = async () => ({canvas: {}, width: 800, height: 600, blanks: [],
                        styled: 3000, incomplete: "mutated"});
shotEncode = async () => ({size: 10});
const a = {id: "a", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({note: a.shotNote, shot: a.shot}));
})();
""", html)
    assert out["shot"], "the crop is still sent — this is about the wording"
    assert "what the user saw" not in out["note"], out["note"]
    # an unbounded doubt earns "corroborate", not "ignore that corner"
    assert "anchor" in out["note"] and "DOM outline" in out["note"]


def test_a_bounded_blank_region_still_says_the_rest_is_the_app(html):
    """The other kind of doubt, and why one closer cannot serve both: a blank
    WebGL region is spatially BOUNDED — the note names its rectangle — so "ignore
    that area, the rest is the app" is exactly right and must survive."""
    out = _capture(html, """
const el = {name: "panel", contains: () => false};
const cv = {name: "cv"};
RECTS.panel = {left: 300, top: 0, width: 400, height: 400};
RECTS.cv = {left: 0, top: 0, width: 400, height: 600};
const a = {id: "a", el: el};
annotations = [a];
BLANKS.push(cv);
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({note: a.shotNote, shot: a.shot}));
})();
""")
    assert out["shot"]
    assert "what the user saw" in out["note"], out["note"]


def test_the_worst_doubt_decides_the_closing_instruction(html):
    """Both at once: a blank region AND a re-render. The caveats are read as one
    instruction, so the closer follows the WORST of them — an unbounded doubt is
    not cancelled by a bounded one also being present."""
    out = _node(_CAPTURE_FNS, _CAPTURE_STUBS + """
shotPane = async () => ({canvas: {}, width: 800, height: 600, blanks: [{name: "cv"}],
                        styled: 3000, incomplete: "mutated"});
shotEncode = async () => ({size: 10});
RECTS.panel = {left: 300, top: 0, width: 400, height: 400};
RECTS.cv = {left: 0, top: 0, width: 400, height: 600};
const a = {id: "a", el: {name: "panel", contains: () => false}};
annotations = [a];
(async () => {
  annApplyShots([a], await annCaptureShots([a]));
  console.log(JSON.stringify({note: a.shotNote}));
})();
""", html)
    assert "WebGL" in out["note"], "the bounded caveat is still reported"
    assert "what the user saw" not in out["note"], out["note"]


def test_the_mutated_note_states_the_problem_without_prescribing_twice(html):
    """The description of what went wrong and the instruction about what to do are
    separate jobs: shotPaneNote owns the first, shotTrustLine the second. Saying
    "trust the anchor over this crop" in both put the same rule in two places."""
    out = _node(["function shotPaneNote(", "function shotTrustLine("], """
console.log(JSON.stringify({note: shotPaneNote({styled: 9, incomplete: "mutated"}),
                            line: shotTrustLine("mutated")}));
""", html)
    assert "re-render" in out["note"]
    assert "anchor" not in out["note"], \
        "the note describes; only shotTrustLine prescribes"
    assert "anchor" in out["line"]


# ----------------------------- the opt-in full-pane shot: a picture of the LAYOUT

def test_a_thrown_capture_degrades_to_sending_the_annotations_without_shots(html):
    """The non-negotiable one: a user losing their typed message because a
    screenshot did not work is not a trade this feature makes."""
    out = _node(_CAPTURE_FNS + ["let targetNoun", "let paneNoun",
                                "function formatAnnotations("],
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


# `targetNoun` is what formatAnnotations' preamble names the target kind
# from — one writer for every piece of chrome that says "project"/"file"
# (test_claude_kind.py), and the annotation block is one of them.
_LEGACY_WIRE = """// The page no longer WRITES a pane-shot block — the composer control was
// deleted — so the legacy wire shape is built by hand here. That is the point:
// what is under test is the STRIPPER, which still has to peel one off a session
// recorded before the removal.
const paneBlock = (v) => "<" + PANE_SHOT_TAG + ">\\nlegacy caption\\n"
  + JSON.stringify(v) + "\\n</" + PANE_SHOT_TAG + ">";
const legacyWire = (msg, pend, st, v) => {
  const parts = [];
  if (st) parts.push(appStateBlock(st));
  if (v) parts.push(paneBlock(v));
  if (pend && pend.length) parts.push(formatAnnotations(pend));
  if (msg) parts.push(msg);
  return parts.join("\\n\\n");
};
"""

_WIRE_ALSO = ["let targetNoun", "let paneNoun", "function formatAnnotations(", "const PANE_SHOT_TAG",
              "function stripPaneBlock(",
              "function stripAnnBlock(", "function stripAppStateBlock(",
              "const APP_STATE_TAG", "function appStateBlock(",
              "const MARKER_ANN", "const MARKER_VIEW", "const MARKER_JOIN",
              "function isMarkerOnly(",
              "function stripBlocks(", "function composeOutgoing("]


def test_a_failed_capture_says_nothing_about_a_pane_shot_nobody_asked_for(html):
    """The other half: a send with the toggle OFF must be byte-identical to one
    from before this feature, failure or not."""
    out = _node([n for n in _CAPTURE_FNS if n != "const SHOT_TIMEOUT_MS"] + _WIRE_ALSO,
                "var SHOT_TIMEOUT_MS = 50;\n" + _CAPTURE_STUBS + """
var console2 = console;
console = {warn: () => {}};
annCaptureShots = async () => { throw new Error("canvas exploded"); };
(async () => {
  const r = await annShots([], false);
  console2.log(JSON.stringify({view: r.view === undefined,
    outgoing: composeOutgoing("just words", [], null, r.view)}));
})();
""", html)
    assert out["view"] is True, "no view field at all when none was requested"
    assert out["outgoing"] == "just words"


def test_a_marker_still_names_a_pane_shot_in_an_older_transcript(html):
    """The marker logic reads what the message CARRIED. A session recorded before
    the pane-shot control was deleted still holds those blocks, and a wordless one
    has to say what it carried instead of rendering an empty bubble."""
    out = _wire(html, """
""" + _LEGACY_WIRE + """
const failed = {view: null, viewNote: "no pane screenshot: it could not be saved"};
const wire = legacyWire("", [], null, failed);
console.log(JSON.stringify({stripped: stripBlocks(wire),
                            marker: isMarkerOnly(stripBlocks(wire))}));
""")
    assert out["stripped"] == "\U0001f5bc pane screenshot"
    assert out["marker"] is True, "still non-identifying, so re-attach must refuse it"


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

# `targetNoun` is what formatAnnotations' preamble names the target kind
# from — one writer for every piece of chrome that says "project"/"file"
# (test_claude_kind.py), and the annotation block is one of them.
_WIRE_FNS = ["let targetNoun", "let paneNoun", "function formatAnnotations(",
             "function stripAnnBlock(",
             "function stripAppStateBlock(", "function stripBlocks(",
             "const APP_STATE_TAG", "function appStateBlock(",
             "const PANE_SHOT_TAG",
             "const MARKER_ANN", "const MARKER_VIEW", "const MARKER_JOIN",
             "function isMarkerOnly(",
             "function stripPaneBlock(", "function composeOutgoing("]


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


def test_every_marker_a_strip_can_produce_is_known_to_be_non_identifying(html):
    """D146, and it has already drifted once. `resumeRun` refuses to match a prior
    turn on a marker because every such send collapses to the SAME text, so it
    identifies no particular turn and a false match trims another turn's assistant
    rows. It excluded the annotations marker by literal — then the pane shot added
    two more marker shapes and the check did not know about them, so a view-only
    send could match the wrong turn and destroy real transcript content.

    The markers are DERIVED here from stripBlocks itself rather than listed, so a
    fourth marker added later without teaching `isMarkerOnly` about it fails this
    test instead of silently reintroducing the bug."""
    out = _wire(html, """
""" + _LEGACY_WIRE + """
const a = {id: "x", content: "here", anchorId: "hdr"};
const view = {view: "/tmp/fr/shots/S-view.png"};
// Every combination that can leave the user's own words empty — the pane-shot
// ones only reachable from a transcript recorded before that control was cut.
const produced = [
  stripBlocks(composeOutgoing("", [a], null)),
  stripBlocks(legacyWire("", [], null, view)),
  stripBlocks(legacyWire("", [a], null, view)),
  stripBlocks(legacyWire("", [a], {entry: "/p"}, view)),
];
console.log(JSON.stringify({
  produced: produced,
  verdicts: produced.map(isMarkerOnly),
  real: ["fix the header", "📌 annotations please", "", "🖼"].map(isMarkerOnly),
}));
""")
    # every marker-only send really does collapse to a non-identifying text...
    assert all(out["produced"]), out["produced"]
    assert len(set(out["produced"])) == 3, out["produced"]
    # ...and the predicate recognises every one of them
    assert out["verdicts"] == [True, True, True, True], out["produced"]
    # a real message — including one that merely mentions a marker — is identifying
    assert out["real"] == [False, False, False, False]


def test_the_re_attach_check_asks_the_predicate_not_a_literal(html):
    """Wired where it matters: `resumeRun` must consult the one definition, so a
    new marker cannot reintroduce the false match. Asserted on the source because
    the alternative is a literal that drifts, which is the bug."""
    start = html.index("const probeMsg = stripBlocks(")
    branch = html[start:html.index("if (probe.done)", start)]
    assert "isMarkerOnly(probeMsg)" in branch, branch
    # and the old literal is gone from the comparison
    assert "probeMsg !== " not in branch, branch


def test_a_pane_shot_path_never_reaches_the_transcript_the_user_reads(html, agent):
    """Same requirement as a crop path: the path is for the model. Nothing writes
    one any more, and every combination is still checked, because a restored
    session from before the control was deleted has to read back cleanly — and the
    strip ORDER is what makes it work, since the annotation block is only
    recognised at position zero."""
    out = _wire(html, """
""" + _LEGACY_WIRE + """
const a = {id: "x", content: "here", anchorId: "hdr", shot: "/tmp/fr/shots/A.png"};
const view = {view: "/tmp/fr/shots/S-view.png", viewNote: "part is blank"};
const st = {entry: "/p/index.html"};
const cases = {
  all: legacyWire("fix this", [a], st, view),
  viewAndState: legacyWire("fix this", [], st, view),
  viewOnly: legacyWire("fix this", [], null, view),
  viewNoWords: legacyWire("", [], null, view),
  annAndViewNoWords: legacyWire("", [a], null, view),
};
const stripped = {};
for (const k of Object.keys(cases)) stripped[k] = stripBlocks(cases[k]);
console.log(JSON.stringify({cases: cases, stripped: stripped}));
""")
    for key, wire in out["cases"].items():
        assert "/tmp/fr/shots/S-view.png" in wire, key
        assert "S-view.png" not in out["stripped"][key], key
        assert "part is blank" not in out["stripped"][key], key
    assert out["stripped"]["all"] == "fix this"
    assert out["stripped"]["viewAndState"] == "fix this"
    assert out["stripped"]["viewOnly"] == "fix this"
    # nothing typed: the bubble names what the turn DID carry rather than being
    # empty, and names BOTH when both rode along
    assert out["stripped"]["viewNoWords"] == "\U0001f5bc pane screenshot"
    assert out["stripped"]["annAndViewNoWords"] == \
        "\U0001f4cc annotations + \U0001f5bc pane screenshot"
    # agent.py's half: it strips the app-state block for meta.json and leaves the
    # rest, exactly as it does for the annotation block — but a path must not be
    # what a user reads, so the page's strip is the one that has to cover this.
    meta = agent._strip_app_state(out["cases"]["viewAndState"])
    assert not meta.startswith("<%s>" % agent.APP_STATE_TAG)


def test_the_agent_cannot_ask_for_a_screenshot(html, agent):
    """Deliberately out of the app_state tool's reach. Letting the model request
    pixels every turn is the cost this design avoids: app_state already answers
    "did my edit land" symbolically. The user-facing whole-pane control that used
    to be the other half of this is gone; crops still ride annotations, which are
    the user's own act."""
    server = open(os.path.join(TEMPLATE_DIR, "permission_server.py"),
                  encoding="utf-8").read()
    for word in ("pane_shot", "paneShot", "screenshot", "view_shot"):
        assert word not in server, word


def _between(html, start, end):
    i = html.index(start)
    return html[i:html.index(end, i)]


def test_the_overlays_share_the_frames_box_so_pin_coordinates_still_line_up(html):
    """The reason for a `#leftview` wrapper rather than putting the strip straight
    into `#left`: pins, the highlight and the composer are positioned in FRAME
    viewport coordinates (annStageRect is just getBoundingClientRect inside the
    frame). Their offset parent therefore has to be the box the iframe fills, or
    every pin would sit the strip's height too high."""
    view = _between(html, '<div id="leftview"', "<!-- /leftview -->")
    for overlay in ("annhl", "annpins", "annpop"):
        assert 'id="%s"' % overlay in view, overlay
    assert "#leftview { position: relative" in html
    # and the two places that measure the host measure THAT box
    assert 'getElementById("left")' not in \
        _between(html, "function renderAnn()", "\n}\n")
    assert 'getElementById("left")' not in \
        _between(html, "function annPlacePop(", "\n}\n")


def test_turning_annotate_mode_off_stays_findable(html):
    """A control that moves must not lose its exit. The button stays visible and
    pressed-styled in the strip, and while active something names both ways out —
    the tooltip, because the visible label has to survive a narrow pane and a
    52-character sentence does not (see the degradation test below)."""
    # `annSetMode` is the one way in and out — Escape leaves the mode too, so the
    # click handler is now a one-line call into it.
    click = _between(html, "function annSetMode(", "\n}")
    assert 'aria-pressed' in click
    assert 'classList.toggle("on", annOn)' in click
    exit_ = click[click.index("annBtn.title ="):]
    assert "Esc" in exit_ or "esc" in exit_, \
        "something should say how to get out while active: " + exit_
    assert "#annbtn.on {" in html, "and the pressed state is still visibly distinct"


def test_hover_never_eats_an_active_state_in_either_control(html):
    """D146. Two controls carry an on/off accent — the composer's pane-shot pill and
    the left pane's banner — and both were one class + one qualifier away from the
    same cascade bug: `.pill:hover` is (0,2,0) and so is
    `.viewshot[aria-pressed="true"]`, so the later rule (hover) won and an ARMED
    toggle repainted itself neutral under the cursor, losing the only signal that
    this send carries a picture.

    So both follow one rule, stated in the selectors rather than left to source
    order: the neutral hover excludes the active state, and the active state hovers
    within its accent. Asserted for both, because a comment saying they agree is not
    a test that they do."""
    pairs = [('.pill:hover:not([aria-pressed="true"])',
              '.pill[aria-pressed="true"]:hover')]
    for neutral, active in pairs:
        assert neutral + " {" in html, neutral
        assert active + " {" in html, active
        # the active hover stays inside the accent instead of repainting it
        assert "filter: brightness" in _between(html, active + " {", "}")
        # ...and it is not bodged in with !important
        assert "!important" not in _between(html, neutral + " {", "}")
    # #annbtn no longer carries an accent fill at all (the subtle restyle), so it
    # has no hover-vs-active collision left to guard: both states paint the same
    # quiet foreground colour.
    assert "#annbtn.on:hover" not in html
    assert "!important" not in html, "specificity, not force"


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


# ------------------ the pane-shot toggle wears the composer's own clothes

def test_the_annotate_control_is_a_labelled_switch(html):
    """The annotate control is ONE labelled left-right sliding switch — plain
    label text plus track + knob, no icon, no filled pill, no accent border. The
    state is spelled out ("Annotating" / "Annotate") and the only colour it ever
    shows is the accent-filled track while armed. The old second switch (#annvis,
    pin visibility) is gone: pins follow the mode."""
    btn = _between(html, "#annbtn {", "}")
    assert "height: 26px" in btn, btn
    assert "white-space: nowrap" in btn, btn
    # quiet by construction: no border, no fill, no accent on the idle control
    assert "border: 0" in btn, btn
    assert "background: transparent" in btn, btn
    assert "var(--accent)" not in btn, "idle control carries no accent: " + btn
    # the switch anatomy: track + knob, and the knob SLIDES on toggle
    assert "#annbtn .track {" in html
    assert "#annbtn .knob {" in html
    assert "left: 14px" in _between(html, "#annbtn.on .knob {", "}")
    # armed state repaints only the small track with the accent — never the pill
    on = _between(html, "#annbtn.on {", "}")
    assert "background" not in on, "no filled pill when armed: " + on
    assert "background: var(--accent)" in _between(html, "#annbtn.on .track {", "}")
    # named, announced, and labelled in the markup — text only, no icon
    view = _between(html, '<div id="anntools">', "</div>")
    assert 'id="annbtn"' in view and 'aria-label="' in view, view
    assert 'class="lbl"' in view, view
    assert "<svg" not in view, "simple text + switch, no glyph: " + view
    assert 'annBtn.querySelector(".lbl").textContent' in html
    # the second switch is gone entirely
    assert "annvis" not in html and "annVisBtn" not in html
    assert "✎" not in html, "the pencil glyph is gone from the template"


def test_annotate_mode_defaults_on_and_owns_pin_visibility(html):
    """The one switch starts ON: an unset param means enabled, and only an
    explicit "0" — the user sliding it off — disables. Encoded as `!== "0"`
    rather than `=== "1"` so a first visit (no params at all) gets the default.
    Pin visibility and auto-send have no params of their own any more: pins
    follow the mode, and a saved new note always auto-sends."""
    assert 'annSetMode(fused.params.get("annmode") !== "0")' in html
    assert "annshow" not in html
    assert "annautosend" not in html
    # pins gate on the mode itself, and toggling the mode repaints them
    assert 'annPins.style.display = annOn ? "" : "none"' in html
    assert "renderAnn()" in _between(html, "function annSetMode(", "\n}")
    # ...and reading the default back through the one writer must not WRITE it.
    # runtime.js pushes a history entry on the first param write of a pristine
    # entry (so Back reaches the URL as loaded), so this boot-time normalisation
    # of an absent `annmode` to "1" burned a second entry: expanding the preview
    # pane to full screen took TWO presses of Back to undo. The DOM and state
    # work still runs; only the write is conditional, and only on a no-op —
    # writing "0" over an absent param is a real disarm (a narrow pane boots
    # that way) and still pushes.
    mode = _between(html, "function annSetMode(on) {", "\nannBtn.addEventListener")
    assert 'if ((fused.params.get("annmode") !== "0") !== annOn) {' in mode
    assert mode.count('fused.params.set("annmode"') == 1
    # auto-send is unconditional (bar the in-flight / typed-draft guards)
    assert "if (isNew && filled && !sending) annAutoSubmit();" in html
    assert "annAutoEl" not in html and 'id="annauto"' not in html


def test_the_annotation_switch_is_a_layout_row_not_an_overlay(html):
    """Every floating version of this control — corner icon, then margin pills —
    eventually landed on top of something (the app's corner, the chat topbar, an
    open transcript). A real flex ROW at the top of the chat pane cannot: layout
    reserves its height, so it covers nothing in either pane and in either view.

    The rule is SHARED with `#leftbar`, the left pane's own strip: they are the
    same row on the two sides of the divider and have to agree on height, so one
    selector declares both rather than two blocks that can drift. Anchoring on
    the second selector asserts the sharing as well as the row-ness."""
    row = _between(html, "#leftbar {", "}")
    assert "display: flex" in row, row
    assert "flex-shrink: 0" in row, row
    # the switch itself is a plain flow child — no absolute anchoring left
    btn = _between(html, "#annbtn {", "}")
    assert "position" not in btn, btn
    assert "top:" not in btn and "right:" not in btn, btn
    # the row is a child of the chat pane, above the topbar, and hidden in
    # NEITHER view — the home-view hide list must not grow to include it
    chat = _between(html, '<div id="chat"', '<div id="topbar">')
    assert 'id="anntools"' in chat, "the row sits above the topbar in the chat pane"
    assert "#anntools" not in _between(html, "#chat.home #topbar", ";"), \
        "visible on the home view too"


def test_the_annotation_controls_sit_outside_the_frame_so_they_cannot_be_captured(html):
    """The switches must not appear in a crop or the pane shot. Structural, not
    hopeful: `shotPane` rasterises `appWindow().document.body` — the FRAMED
    document — so anything in the parent document is unreachable by construction.
    They live in the chat pane's #anntools row, nowhere near the iframe."""
    view = _between(html, '<div id="leftview"', "<!-- /leftview -->")
    assert 'id="leftframe"' in view
    assert 'id="annbtn"' not in view, "the controls left the app pane entirely"
    assert 'id="annbtn"' in _between(html, '<div id="anntools">', "</div>")
    # and the capture still reads the frame, not the pane
    assert "appWindow()" in _between(html, "async function shotPane(", "\n}\n")


def test_toggling_annotate_mode_cannot_move_the_app(html):
    """Not cosmetic. Pins and `annStageRect` are in FRAME viewport coordinates, so
    anything that reflows the app mid-session moves the element the user is aiming
    at and drags already-placed pins off what they annotate. An overlay cannot
    resize the iframe at all — that is the structural half. The other half is that
    the armed state may only REPAINT: no size, no offset, no border box."""
    on = _between(html, "#annbtn.on {", "}")
    for prop in ("width", "height", "padding", "font-size", "margin", "top",
                 "bottom", "right", "transform", "border-radius", "border-width"):
        assert prop not in on, "the armed state may only repaint: " + on
    # and the toggle itself touches nothing that could relayout the pane
    click = _between(html, "function annSetMode(", "\n}")
    for banned in ("style.height", "style.width", "style.padding", "leftview"):
        assert banned not in click, click


def test_both_ways_out_of_annotate_mode_are_named_while_armed(html):
    """With no label there is nowhere on screen to write "Esc or click to stop", so
    the tooltip carries it — and it must name BOTH exits, because Escape is the one
    a user is least likely to guess and the click is the one they can find by
    hovering. The tooltip has no width limit, so this costs no space.

    Scoped to annSetMode's own write, because there are now TWO writers of this
    tooltip: applyPaneNoun sets the IDLE wording when the target's kind resolves
    (the idle half names the pane, "the app" or "the preview"), and annSetMode
    restores it on every disarm. That is why the idle string is a function with one
    definition — a literal in both places meant the first toggle-off threw away the
    kind-correct noun applyPaneNoun had just written. Only the ARMED half is
    asserted here; it names no kind and belongs to this function alone."""
    mode = _between(html, "function annSetMode(on) {", "\nannBtn.addEventListener")
    title = _between(mode, "annBtn.title =", ";")
    assert "Esc" in title and "click this button" in title, title
    # the armed tooltip is the one that names them
    armed = title.split(":")[0]
    assert "Esc" in armed, title
    # ...and the idle half is the shared builder, not a second literal.
    assert "annIdleTitle()" in title
    assert html.count('+ ", then send the notes to Claude"') == 1, \
        "the idle tooltip has one definition, annIdleTitle"
