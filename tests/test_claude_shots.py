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

D1310: the page half of this file (source pins and node probes over the
retired iframe chat page, templates/claude/template.html) went with that
page; the native chat under frontend/src/apps/claude owns it now. What
stays is the backend half.
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")


def _load(name):
    path = os.path.join(AGENT_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_shots_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


class _HostProc:
    """Stands in for the session_host.py process `_start` now Popens instead
    of the CLI itself: the CLI's argv is built by `_claude_argv` inside THAT
    process, from the JSON request written to this stub's stdin — so a test
    that wants the argv has to capture the request and rebuild it, the same
    call session_host.py's own `main()` makes."""
    pid = 4242

    def wait(self, timeout=None):
        # `_start` parks a daemon reaper thread in wait() on the host.
        return 0

    class _Stdin:
        def __init__(self, seen):
            self._seen = seen
            self._buf = b""

        def write(self, data):
            self._buf += data

        def close(self):
            self._seen["req"] = json.loads(self._buf.decode("utf-8"))

    def __init__(self, seen):
        self.stdin = _HostProc._Stdin(seen)


def _argv_from_req(agent, req, run_dir):
    return agent._claude_argv(
        run_dir, req["pane"], req["cli_mode"] or None, req["session_id"],
        req["model"], req["effort"], req["extra_read_dirs"], req["file"])


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

    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen", lambda cmd, **kw: _HostProc(seen))
    out = agent._start(str(project), "hi", "", "", "")
    assert "error" not in out, out
    run_dir = os.path.join(agent.RUNS, out["run_id"])
    cmd = _argv_from_req(agent, seen["req"], run_dir)
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
    # The dir comes back through _wire_path (forward slashes on every
    # platform, so the page's crop paths match the Read(//…/**) rule) — not
    # the raw join, which is backslashed on Windows.
    assert agent.main(action="shots_dir") == {"dir": agent._wire_path(str(shots))}
    assert os.path.isdir(shots)
    if hasattr(os, "geteuid"):
        assert stat.S_IMODE(os.lstat(shots).st_mode) == 0o700
    # second message, same directory
    assert agent.main(action="shots_dir") == {"dir": agent._wire_path(str(shots))}


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


# ------------------------------------- transcoding what neither side can decode

def _pillow():
    pytest.importorskip("PIL", reason="pillow is a bundled extra")
    from PIL import Image
    return Image


def _tiff(path, size=(40, 30), frames=1, mode="RGB"):
    """A real TIFF on disk — the format the bug was reported with, and one no
    browser engine here decodes."""
    Image = _pillow()
    pages = [Image.new(mode, size, (i * 40 % 255, 60, 200))
             for i in range(frames)]
    pages[0].save(str(path), format="TIFF",
                  save_all=frames > 1, append_images=pages[1:])
    return path


def test_a_tiff_in_the_shots_dir_becomes_a_png_beside_it(agent, tmp_path,
                                                        monkeypatch):
    """The whole point of the action: `Read` cannot open a .tif, so the page hands
    the path over and gets one it can. The original STAYS — it is what the user
    attached, and the pruner already owns everything in this directory."""
    Image = _pillow()
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    src = _tiff(shots / "20260828-aaaa.tif")
    out = agent.main(action="image_to_png", path=str(src))
    assert "error" not in out, out
    assert out["path"] == agent._wire_path(str(shots / "20260828-aaaa.png"))
    assert os.path.exists(out["path"])
    assert (out["source_w"], out["source_h"]) == (40, 30)
    assert (out["width"], out["height"]) == (40, 30), "small enough already"
    assert out["bytes"] == os.path.getsize(out["path"])
    assert Image.open(out["path"]).format == "PNG"
    assert src.exists(), "the original is the pruner's to delete, not ours"


def test_a_big_picture_comes_back_capped_at_the_pages_own_edge(agent, tmp_path,
                                                              monkeypatch):
    """SHOT_PNG_EDGE is the page's SHOT_VIEW_EDGE: a picture the user brought in
    is never carried at a size the app's own whole-pane captures are not."""
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    src = _tiff(shots / "huge.tif", size=(4000, 1000))
    out = agent.main(action="image_to_png", path=str(src))
    assert "error" not in out, out
    assert agent.SHOT_PNG_EDGE == 1600
    assert (out["source_w"], out["source_h"]) == (4000, 1000)
    assert out["width"] == 1600, "longest edge capped"
    assert out["height"] == 400, "and the aspect kept"


def test_a_multi_frame_tiff_is_answered_with_its_first_frame(agent, tmp_path,
                                                            monkeypatch):
    """A scanned stack or a fax is many pages in one file, and the one the user
    means by "the picture" is the first."""
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    src = _tiff(shots / "stack.tif", frames=3)
    out = agent.main(action="image_to_png", path=str(src))
    assert "error" not in out, out
    assert (out["width"], out["height"]) == (40, 30)


def test_a_greyscale_or_cmyk_source_is_converted_rather_than_refused(
        agent, tmp_path, monkeypatch):
    """PNG will not take CMYK and the agent's reader would not thank us for
    16-bit grey, so every mode that is not already RGB/RGBA leaves its own."""
    Image = _pillow()
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    for i, mode in enumerate(("CMYK", "L", "I;16")):
        src = shots / ("m%d.tif" % i)
        Image.new(mode, (12, 9)).save(str(src), format="TIFF")
        out = agent.main(action="image_to_png", path=str(src))
        assert "error" not in out, (mode, out)
        assert Image.open(out["path"]).mode in ("RGB", "RGBA"), mode


def test_transparency_survives_and_a_jpeg_fallback_flattens_it(agent, tmp_path,
                                                              monkeypatch):
    """Alpha is kept where it exists — a diagram with a transparent background
    reads wrong flattened — and only the JPEG ladder, which has no alpha, puts it
    on white."""
    Image = _pillow()
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    src = shots / "logo.tif"
    Image.new("RGBA", (20, 20), (10, 20, 30, 0)).save(str(src), format="TIFF")
    out = agent.main(action="image_to_png", path=str(src))
    assert out["path"].endswith(".png")
    assert Image.open(out["path"]).mode == "RGBA"
    # and with the budget squeezed to nothing, the same file goes out as a JPEG
    monkeypatch.setattr(agent, "SHOT_PNG_MAX_BYTES", 1)
    out = agent.main(action="image_to_png", path=str(src))
    assert out["path"].endswith(".jpg"), out
    assert Image.open(out["path"]).format == "JPEG"
    # past the ladder it still ships: an oversize picture the agent CAN read beats
    # a perfectly sized one it cannot
    assert out["bytes"] == os.path.getsize(out["path"])


def test_a_path_outside_the_shots_dir_is_refused(agent, tmp_path, monkeypatch):
    """The action reads bytes off disk and writes a sibling next to them, and the
    only place either may happen is the directory the page uploads into. `-evil`
    is the case a string prefix would have let through."""
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    outside = _tiff(tmp_path / "secret.tif")
    evil = tmp_path / "shots-evil"
    evil.mkdir()
    sneaky = _tiff(evil / "x.tif")
    for bad in (str(outside), str(sneaky), str(shots / ".." / "secret.tif"),
                "relative.tif", ""):
        out = agent.main(action="image_to_png", path=bad)
        assert "error" in out, bad
        assert "path" not in out
    assert not list(evil.glob("*.png")), "and nothing was written out there"


def test_bytes_that_are_not_a_picture_are_an_error_not_a_crash(agent, tmp_path,
                                                              monkeypatch):
    """Never raises across the bridge: the page's whole answer to a failure is to
    keep the bytes-and-a-glyph attachment it already had, so an exception would
    cost the user the attachment in order to report a problem with it."""
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    junk = shots / "notreally.tif"
    junk.write_bytes(b"this is not a tiff, or anything else" * 20)
    out = agent.main(action="image_to_png", path=str(junk))
    assert "error" in out and "path" not in out, out
    missing = agent.main(action="image_to_png", path=str(shots / "gone.tif"))
    assert missing == {"error": "no such file"}


@pytest.mark.skipif(sys.platform != "darwin", reason="sips is macOS' own")
def test_heic_reaches_the_os_decoder_pillow_does_not_have(agent, tmp_path,
                                                          monkeypatch):
    """HEIC is the DEFAULT camera format on every iPhone and the one format both
    halves of this feature are blind to: no browser engine here decodes it, and
    Pillow needs `pillow-heif`, a compiled wheel we do not ship. `sips` is present
    on every macOS install, so the fallback costs nothing at rest."""
    Image = _pillow()
    shots = tmp_path / "shots"
    shots.mkdir()
    monkeypatch.setattr(agent, "SHOTS", str(shots))
    seed = shots / "seed.png"
    Image.new("RGB", (60, 40), (200, 30, 30)).save(str(seed))
    heic = shots / "IMG_4031.heic"
    made = subprocess.run(["/usr/bin/sips", "-s", "format", "heic", str(seed),
                           "--out", str(heic)], capture_output=True)
    if made.returncode != 0 or not heic.exists():
        pytest.skip("this macOS cannot write heic")
    out = agent.main(action="image_to_png", path=str(heic))
    assert "error" not in out, out
    assert out["path"].endswith(".png")
    assert (out["source_w"], out["source_h"]) == (60, 40)
    assert Image.open(out["path"]).format == "PNG"
    # and nothing of the conversion is left behind
    assert not list(shots.glob("conv-*")), "the sips temp file is cleaned up"


# ---------------------------------------------- the page's own JS, under node


_CAPS = ["const SHOT_MAX_EDGE", "const SHOT_MAX_BYTES",
         "const SHOT_MIN_AREA", "const SHOT_TIMEOUT_MS"]


# ------------------------------------------------------------ the crop rect math


# ------------------------------------------------------------- the downscale math


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


# --------------------------------------------- blank WebGL, detected not shipped


# --------------------------- one capture, ONE labelled overview: the orchestration

# Everything annCaptureOverview touches outside itself, recorded rather than
# performed. `shotPane` and `shotEncodeBadged` are reassigned (a function
# declaration is a mutable binding) because rasterising is exactly the part node
# cannot do — what is under test is which annotations get a badge, what the
# others are told, and that ONE file is written for all of them.
_CAPTURE_FNS = _CAPS + ["const SHOT_VIEW_EDGE", "const SHOT_VIEW_BYTES",
                        "function shotPaneNote(", "function shotTrustLine(",
                        "function shotImageNote(",
                        "function shotCropRect(", "function shotBlankRegions(",
                        "function shotExt(", "function shotFit(",
                        "function annLabelFor(", "function annPointXY(",
                        "const APP_STATE_UNREADABLE",
                        "async function annCaptureOverview(", "function shotJoin(",
                        # One stamp writer for both the overview and the whole-pane
                        # shot, so two files out of one click cannot collide.
                        "function shotStamp(",
                        "function annApplyOverview(", "function shotRevoke(",
                        "function annOverview("]

_CAPTURE_STUBS = """
var uploaded = [];
var fused = {uploadFile: async (path, blob) => { uploaded.push(path); return {}; }};
var crypto = {randomUUID: () => "abcdef01-2345-6789"};
var URL = {createObjectURL: (b) => "blob:" + uploaded.length};
var annotations = [];
var RECTS = {};
var BLANKS = [];
var PANE = {canvas: {}, width: 800, height: 600, blanks: BLANKS};
var annXO = false;
var ANN_XO_SCROLL = {scrollX: 0, scrollY: 0};
function annResolve(c, doc) { return c.el === undefined ? {contains: () => false} : c.el; }
function annStageRect(el) { return RECTS[el.name] || {left: 10, top: 10, width: 100, height: 50}; }
function annIntrinsic(el) { return el.nat || null; }
function annContentBox(el) { return el.box || annStageRect(el); }
var annFrame = {contentDocument: {defaultView: {scrollX: 0, scrollY: 0}}};
function shotDirPath() { return Promise.resolve("/tmp/fr/shots"); }
"""

_CAPTURE_TAIL = """
var LAST_ENCODE = {};
shotPane = async () => PANE;
shotEncodeBadged = async (pane, badges, limits) => {
  LAST_ENCODE = {badges: badges, limits: limits};
  return {size: 10};
};
shotEncode = async (pane, rect, limits) => {
  LAST_ENCODE = {rect: rect, limits: limits};
  return {size: 10, rect: rect};
};
"""


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


# ------------------- the walk yields, so the live DOM can move underneath it


# ------------------------------- the note names the cause, not a guess at it


# ------------------------- how much of a caveated shot the agent should trust


# ----------------------------- the opt-in full-pane shot: a picture of the LAYOUT


# `targetNoun` is what formatAnnotations' preamble names the target kind
# from — one writer for every piece of chrome that says "project"/"file"
# (test_claude_kind.py), and the annotation block is one of them.
# The wire shape a session recorded BEFORE the screenshot button existed carries.
# Built by hand, and deliberately so: those blocks were written by a composer
# control that has been deleted and rewritten since, with a different caption, and
# the stripper has to peel one off regardless of which writer produced it. Reading
# an old wire format is a permanent obligation.
_LEGACY_WIRE = """const paneBlock = (v) => "<" + PANE_SHOT_TAG + ">\\nlegacy caption\\n"
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

_WIRE_ALSO = ["let targetNoun", "let paneNoun", "const ANN_TAG", "const ANN_NO_WORDS",
             "function annClock(", "function annStanza(", "function formatAnnotations(", "const PANE_SHOT_TAG",
              "function stripPaneBlock(",
              "function stripAnnBlock(", "function stripAppStateBlock(",
              "const APP_STATE_TAG", "function appStateBlock(",
              "const MARKER_ANN", "const MARKER_VIEW", "const MARKER_IMG",
              "const MARKER_FILE",
              "const MARKERS", "const MARKER_JOIN",
              "function isMarkerOnly(", "function paneShotBlock(",
              # stripBlocks reads the block back to choose WHICH picture marker
              "function paneShotIn(",
              "function stripBlocks(", "function composeOutgoing("]


# ----------------------------------------------------------------- the wire shape

# `targetNoun` is what formatAnnotations' preamble names the target kind
# from — one writer for every piece of chrome that says "project"/"file"
# (test_claude_kind.py), and the annotation block is one of them.
_WIRE_FNS = ["let targetNoun", "let paneNoun", "const ANN_TAG", "const ANN_NO_WORDS",
             "function annClock(", "function annStanza(", "function formatAnnotations(",
             "function stripAnnBlock(",
             "function stripAppStateBlock(", "function stripBlocks(",
             "const APP_STATE_TAG", "function appStateBlock(",
             "const PANE_SHOT_TAG", "function paneShotBlock(",
             "const MARKER_ANN", "const MARKER_VIEW", "const MARKER_IMG",
             "const MARKER_FILE",
             "const MARKERS", "const MARKER_JOIN",
             "function isMarkerOnly(",
             "function stripPaneBlock(", "function paneShotIn(",
             "function composeOutgoing("]


# What `annotationsIn` needs on top of the writer's own list: the stanza
# grammar it parses back. Declared once — three tests read the wire.
_READER_FNS = ["const ANN_STANZA_HEAD", "const ANN_STANZA_CLOCK",
               "const ANN_STANZA_SEP", "const ANN_STANZA_POINT",
               "const ANN_STANZA_TAG", "const ANN_STANZA_OFFSCREEN",
               # ANN_NO_WORDS is the WRITER's too and is already in _WIRE_FNS —
               # extracting it twice is a duplicate `const` and a SyntaxError.
               "function annStanzaIn(", "function annotationsIn("]


def test_the_agent_cannot_ask_for_a_screenshot():
    """Deliberately out of the app_state tool's reach. Letting the model request
    pixels every turn is the cost this design avoids: app_state already answers
    "did my edit land" symbolically. The whole-pane picture and the crops are BOTH
    the user's own act — a button they pressed, a note they wrote — and neither is
    something the model may reach for."""
    server = open(os.path.join(AGENT_DIR, "permission_server.py"),
                  encoding="utf-8").read()
    for word in ("pane_shot", "paneShot", "screenshot", "view_shot"):
        assert word not in server, word


# ------------------ the pane-shot toggle wears the composer's own clothes


# ------------------------------------------------- the composer's screenshot button
#
# The control that captures the WHOLE visible pane. It existed once as a
# per-message TOGGLE, was deleted for being one ("it doesn't make sense": a picture
# nobody had seen, of a moment nobody chose, behind a switch that had to be
# re-armed every turn), and came back as a BUTTON that captures on click and hangs
# the result above the composer as a chip. The wire format is the one that was
# already there — `<pane-shot>`, whose reader never went away — so these tests pin
# the new half: when the picture is taken, what the chip does, and what rides the
# message.

# `shotCapturePane` reaches for the same helpers a crop does, plus the pane-only
# caps and the blank-region prose. `shotPane`/`shotEncode` are reassigned by
# _CAPTURE_TAIL for the same reason as ever — rasterising is the part node cannot
# do, and what is under test is the orchestration around it.
_VIEW_FNS = _CAPTURE_FNS + ["async function shotCapturePane("]


# --------------------------------------------- the second pass: seeing the picture
#
# The button shipped and the report was three sentences long: it was not obvious
# that a screenshot had been taken, there was no way to look at one before sending
# it, and a sent one vanished into a four-word marker. All three are the same
# failure — a picture the user could never actually SEE — and these pin the three
# answers: a flash over the photographed pane, a viewer behind every thumbnail,
# and a receipt that survives a reload.


# ============================================================================
# FIDELITY: the two ways a capture lied about the screen it photographed
#
# Both were found the same way — a user took a picture and looked at it — and
# both are properties of the SVG/foreignObject technique rather than of any one
# app, so the tests are about the technique.
#
#   SCROLL. `cloneNode` copies attributes, and `scrollTop`/`scrollLeft` are
#     properties. There is no markup for "scrolled 3160px down", so a clone of a
#     scrolled page is a clone of that page at the top. shotPane compensated for
#     the WINDOW's scroll only, which is right for a document that scrolls itself
#     and wrong for every app whose content lives in an inner `overflow: auto`
#     box. Reproduced against the markdown preview (one `.cm-scroller`): scrolled
#     to an API list halfway down, the capture came out as the document's title on
#     an empty page — because CodeMirror only renders the rows near the viewport,
#     so the top of an unscrolled clone of a scrolled editor is spacer.
#   IMAGES. An `<svg>` loaded through an `<img>` renders with external resource
#     loading disabled, at every origin. Reproduced against the README's hero
#     screenshot (served by our own `/api/fs/raw`, same-origin, and it made no
#     difference): a broken-image glyph and its alt text where the picture was.
# ============================================================================

_SCROLL_STUBS = """
function el(style) {
  return { attrs: {style: style || ""}, children: [],
           getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
           setAttribute(k, v) { this.attrs[k] = String(v); } };
}
function styleOf(e) { return e.getAttribute("style"); }
"""


# ------------------------------------------------------- inlining the images

_IMG_FNS = ["const SHOT_IMG_MAX", "const SHOT_IMG_MAX_BYTES",
            "function shotDataUrl(", "async function shotUrlAsData(",
            "function shotStyleUrls(", "function shotImagePlaceholder(",
            "async function shotInlineImages("]

_IMG_STUBS = """
var FETCHED = [];
var FAIL = {};
function mkEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(), attrs: {}, children: [], parent: null,
    naturalWidth: 0, alt: "", textContent: "",
    get currentSrc() { return this.attrs.src || ""; },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    append(c) { c.parent = el; el.children.push(c); return c; },
    remove() {
      if (this.parent) this.parent.children =
        this.parent.children.filter((n) => n !== this);
    },
    replaceWith(n) {
      const p = this.parent; if (!p) return;
      p.children = p.children.map((c) => (c === this ? n : c));
      n.parent = p;
    },
    querySelectorAll(sel) { return all(el, sel); },
  };
  el.ownerDocument = { createElement: mkEl };
  return el;
}
function walkAll(root, out) { for (const c of root.children) { out.push(c); walkAll(c, out); } }
function all(root, sel) {
  const out = []; walkAll(root, out);
  if (sel === "*") return out;
  if (sel === "img") return out.filter((e) => e.tagName === "IMG");
  if (sel === "picture source")
    return out.filter((e) => e.tagName === "SOURCE" && e.parent
                             && e.parent.tagName === "PICTURE");
  return [];
}
var document = { createElement: mkEl };
var fetch = async (url) => {
  FETCHED.push(url);
  if (FAIL[url]) throw new Error("no route to host");
  return { ok: true, blob: async () => ({ size: 10, url: url }) };
};
function FileReader() {
  this.readAsDataURL = (blob) => {
    this.result = "data:image/png;base64," + blob.url;
    this.onload();
  };
}
// One <img> in a clone, and its live twin, built as a pair.
function pair(src) {
  const s = mkEl("img"), d = mkEl("img");
  s.attrs.src = src; d.attrs.src = src;
  return [s, d];
}
"""


# ============================================================================
# THE OTHER WAY A PICTURE GETS INTO A MESSAGE: paste and drag-and-drop
#
# "If I have a screenshot I should be able to paste it from clipboard or drag and
# drop." The camera cannot serve this — a crop from the OS shortcut, a photo of a
# phone, a mock a designer sent — because none of it is on the pane. It rides the
# SAME pipeline: the shots directory (already the one path `--allowed-tools`
# pre-approves a Read of, already pruned, already served back through
# /api/fs/raw), one chip, one viewer, one wire block, one receipt.
# ============================================================================


_ATTACH_FNS = ["const SHOT_ATTACH_MAX_BYTES",
               "let shotAttached", "function shotFileExt(", "function shotIsImage(",
               "function shotKindFor(",
               "function shotSaveExt(", "function shotSizeLabel(",
               "function shotJoin(", "function shotStamp(",
               # the resize path: it is `shotEncode`'s own ladder, not a copy, so
               # the whole ladder has to come along with it
               "const SHOT_MAX_EDGE", "const SHOT_MAX_BYTES",
               "const SHOT_VIEW_EDGE", "const SHOT_WEBP_QUALITY",
               "let shotWebpOk", "function shotFit(", "function shotBlob(",
               "function shotExt(", "async function shotEncode(",
               "async function shotPixels(", "async function shotShrink(",
               # the server transcode: the answer for a picture NEITHER side can
               # read, which is the only thing in this path that leaves the page
               "function shotFormatLabel(", "async function shotServerPng(",
               "async function shotAttachFile(", "async function shotAttachFiles("]

# What the PATH shape needs on top: it makes no upload and reads no bytes, so it
# shares only the list with the copying path.
_PATH_FNS = ["let shotAttached", "let shotDirSeen",
             "function shotIsImage(",
             "function shotKindFor(", "const SHOT_PATH_TYPE",
             "function shotBase(", "function shotDirOf(",
             "function shotDropPaths(", "function shotAttachPaths(",
             "function shotReadDirs("]

_ATTACH_STUBS = """
var uploaded = [];
var wrote = [];
var renders = 0;
// What the server answers `image_to_png` with. null is a server that could not
// decode it either (no pillow, a format ImageIO does not know, a Mac-less box),
// which is the branch that has to keep D613's bytes-and-a-glyph attachment.
var CONVERT = null;
var pythons = [];
var AGENT = "agent.py";
var fused = {uploadFile: async (path, blob) => {
  uploaded.push(path); wrote.push(blob); return {};
}, runPython: async (mod, params, opts) => {
  pythons.push({mod: mod, params: params, opts: opts});
  return CONVERT ? CONVERT(params.path) : {error: "could not decode: nope"};
}, rawUrl: (p) => "/api/fs/raw?path=" + p};
var crypto = {randomUUID: () => "abcdef01-2345-6789"};
var revoked = [];
var URL = {createObjectURL: (b) => "blob:" + uploaded.length,
           revokeObjectURL: (u) => revoked.push(u)};
function renderAnn() { renders++; }
function shotDirPath() { return Promise.resolve("/tmp/fr/shots"); }
// A `File` as the browser hands one over, plus the one thing only a real decode
// can tell us: `px` is the pixel size, and a file given NONE is one this engine
// cannot decode at all (a .tiff, a .heic, a raw camera file).
function file(name, type, size, px) {
  return {name: name, type: type, size: size || 10, px: px || null};
}
// createImageBitmap the way a browser's behaves: it THROWS on a format it cannot
// decode. There is no `Image` here either, so the <img> fallback declines too —
// which is exactly a headless engine that can read nothing at all.
var closed = 0;
async function createImageBitmap(f) {
  if (!f.px) throw new Error("The source image cannot be decoded.");
  return {width: f.px[0], height: f.px[1], close: () => { closed++; }};
}
// One canvas, one encoder. ENCODED is what a re-encode COSTS at a given size;
// the default is "a byte per eight pixels", which is roughly what a webp step
// does to a photograph and puts a 4200x2800 original comfortably over the cap
// until it has been fitted to SHOT_VIEW_EDGE.
var encodes = [];
var ENCODED = (type, q, w, h) => Math.round(w * h / 8);
var document = {createElement: () => ({
  width: 0, height: 0,
  getContext: () => ({drawImage: () => {}}),
  toBlob: function (cb, type, q) {
    encodes.push({type: type, q: q === undefined ? null : q,
                  w: this.width, h: this.height});
    const got = type === "image/webp" ? "image/webp" : "image/png";
    cb({type: got, size: ENCODED(got, q, this.width, this.height)});
  },
})};
"""


# ============================================================================
# ANY FILE, and a PATH where there is one. Two halves of the same complaint: a
# chat that could be handed a screenshot OF a spreadsheet but not the
# spreadsheet, and a drag from the explorer beside it that could only ever
# deliver a COPY of a file the user already had. The first is a gate that came
# out; the second is a payload (`application/x-fused-path`) the chat now prefers
# over `files` whenever a drag carries it.
# ============================================================================


def test_the_spawn_line_grows_a_read_rule_per_attachment_directory(
        agent, tmp_path, monkeypatch):
    """The other end of `read_dirs`, and the reason the page has to say it at all:
    a real-path attachment sits outside the shots directory, so without a rule of
    its own the agent cards a Read of the very file the user just dragged in.

    Validated here rather than trusted, because a grant is a grant: the parameter
    crosses the bridge as a plain string, and this is the last place before it
    becomes a permission rule. Relative paths, paths that are not directories, a
    filesystem ROOT (whose rule would be the whole disk) and anything past the cap
    are all dropped, and an unparseable value is an empty list — never an error,
    since a refused grant costs one card and a refused send costs the message."""
    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(agent, "SHOTS", str(tmp_path / "shots"))
    project = tmp_path / "proj"
    project.mkdir()
    drops = tmp_path / "drops"
    drops.mkdir()
    seen = {}

    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(agent.subprocess, "Popen", lambda cmd, **kw: _HostProc(seen))
    out = agent.main(action="start", file=str(project), message="hi",
                     read_dirs=json.dumps([str(drops)]))
    assert "error" not in out, out
    run_dir = os.path.join(agent.RUNS, out["run_id"])
    cmd = _argv_from_req(agent, seen["req"], run_dir)
    allowed = cmd[cmd.index("--allowed-tools") + 1].split(",")
    assert agent._read_rule(str(tmp_path / "shots")) in allowed, "still there"
    assert agent._read_rule(str(drops)) in allowed
    assert "Read" not in allowed, "never a blanket rule"

    # what the validator refuses, and never as an error
    root = "C:\\" if os.name == "nt" else "/"
    assert agent._attach_dirs(json.dumps(["relative/x", str(project / "nope"),
                                          root, str(project / "..")])) \
        == [agent._wire_path(str(tmp_path))], \
        "only an absolute, existing, non-root directory survives"
    assert agent._attach_dirs("not json") == []
    assert agent._attach_dirs(json.dumps({"dir": str(drops)})) == []
    assert agent._attach_dirs("") == []
    # NO COUNT LIMIT (D617): `_ATTACH_DIRS_MAX` (4) is gone, name and branch both.
    # Twenty rows out of two folders is two rules because they DEDUPE, which is
    # what kept the argv bounded in practice; the cap only ever cost the user a
    # permission card per attachment past the fourth.
    assert not hasattr(agent, "_ATTACH_DIRS_MAX"), "the number is gone, not raised"
    many = [str(drops)] + [str(tmp_path)] * 20
    assert agent._attach_dirs(json.dumps(many)) == [
        agent._wire_path(str(drops)), agent._wire_path(str(tmp_path))]
    lots = [str(d) for d in _many_dirs(tmp_path, 12)]
    assert agent._attach_dirs(json.dumps(lots)) == \
        [agent._wire_path(d) for d in lots], "twelve folders, twelve rules"


def _many_dirs(tmp_path, n):
    """n real directories, for asserting a grant list is not truncated."""
    out = []
    for i in range(n):
        d = tmp_path / ("drop%d" % i)
        d.mkdir(exist_ok=True)
        out.append(str(d))
    return out


# ============================================================================
# THE ATTACHMENT'S OWN PREVIEW (D616). A name and a size answer "which file is
# this"; they do not answer "is this the right file". fused-render already owns a
# template for a .csv, a .md, a .parquet — so the viewer frames the attachment in
# it. Icon by default, preview on the CLICK: the chip and the receipt stay one
# glyph wide, because a running template is not a 22px ornament.
# ============================================================================


# ------------------------------------------------------ the native screen shot


