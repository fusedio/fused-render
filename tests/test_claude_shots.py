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
import re
import os
import shutil
import stat
import subprocess
import sys
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


class _HostProc:
    """Stands in for the session_host.py process `_start` now Popens instead
    of the CLI itself: the CLI's argv is built by `_claude_argv` inside THAT
    process, from the JSON request written to this stub's stdin — so a test
    that wants the argv has to capture the request and rebuild it, the same
    call session_host.py's own `main()` makes."""
    pid = 4242

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


def test_the_page_asks_for_the_directory_by_the_action_the_agent_serves(
        agent, html, tmp_path, monkeypatch):
    """D146, the two-sided wire: the page names the action, agent.py routes it."""
    assert 'action: "shots_dir"' in html
    monkeypatch.setattr(agent, "SHOTS", str(tmp_path / "shots"))
    # See the note in the "created private" test above: _wire_path forward-
    # slashes this, on every platform.
    assert agent.main(action="shots_dir").get("dir") == agent._wire_path(
        str(tmp_path / "shots"))


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


def test_the_page_asks_for_the_transcode_by_the_action_the_agent_serves(
        agent, html):
    """D146, the two-sided wire, for the second action this feature added."""
    assert 'action: "image_to_png"' in html
    assert 'if action == "image_to_png"' in open(
        os.path.join(TEMPLATE_DIR, "agent.py"), encoding="utf-8").read()


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
    # `encoding="utf-8"` is not decorative: `text=True` alone decodes the
    # child's stdout with locale.getpreferredencoding(False), and node always
    # writes its UTF-8 source glyphs (the shot markers' 🖼/📌) as UTF-8 bytes
    # regardless of platform. On Windows that locale default is commonly
    # cp1252, which decodes those bytes into mojibake without ever raising —
    # a silent corruption, not a crash, so it slipped past every POSIX run
    # where the locale default already happens to be UTF-8.
    out = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_CAPS = ["const SHOT_MAX_EDGE", "const SHOT_MAX_BYTES",
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


def _capture(html, body):
    return _node(_CAPTURE_FNS, _CAPTURE_STUBS + _CAPTURE_TAIL + body, html)


def test_every_annotation_shares_one_overview_with_its_own_badge(html):
    """N notes cost one rasterisation, one encode and ONE upload: the overview.
    Each badge lands at the note's own on-screen spot wearing the note's stored
    `label` — the same string the wire carries — and a note that got its badge
    is left without an `offscreen` excuse."""
    out = _capture(html, """
const a = {id: "a", label: "A", el: {name: "a", contains: () => false}};
const b = {id: "b", label: "B", kind: "point", x: 300, y: 400};
RECTS.a = {left: 10, top: 20, width: 100, height: 50};
annotations = [a, b];
(async () => {
  const r = await annCaptureOverview([a, b]);
  annApplyOverview([a, b], r);
  console.log(JSON.stringify({view: r.view, uploaded: uploaded, thumb: r.thumb,
    badges: LAST_ENCODE.badges, limits: LAST_ENCODE.limits, marks: r.marks,
    clean: [a.offscreen === undefined, b.offscreen === undefined]}));
})();
""")
    assert len(out["uploaded"]) == 1, "one picture for every note on the message"
    assert out["view"] == out["uploaded"][0]
    assert out["view"].startswith("/tmp/fr/shots/")
    assert out["view"].endswith("-overview.png")
    assert out["thumb"], "and a thumbnail, because the user has to see it first"
    # the pane's own caps, not the old crop caps: a whole pane squeezed into
    # 640px is unreadable, which defeats the reason to send it
    assert out["limits"] == {"maxEdge": 1600, "maxBytes": 900 * 1024}
    assert out["badges"] == [{"x": 60, "y": 45, "label": "A"},
                             {"x": 300, "y": 400, "label": "B"}]
    assert out["marks"] == {"a": True, "b": True}
    assert out["clean"] == [True, True]


def test_an_image_click_badges_the_clicked_pixel_not_the_box_center(html):
    """A note on an image/canvas stored iu/iv — fractions of the painted
    content box — and the on-page pin marks that spot, so the burned-in badge
    must name the same pixel (Bugbot, #661). Center is only for element notes
    that carry no fractional click."""
    out = _capture(html, """
const a = {id: "a", label: "A", iu: 0.25, iv: 0.5,
           el: {name: "a", contains: () => false, nat: {w: 400, h: 300},
                box: {left: 100, top: 40, width: 200, height: 150}}};
RECTS.a = {left: 0, top: 0, width: 800, height: 600};
annotations = [a];
(async () => {
  const r = await annCaptureOverview([a]);
  console.log(JSON.stringify({badges: LAST_ENCODE.badges, marks: r.marks}));
})();
""")
    assert out["badges"] == [{"x": 150, "y": 115, "label": "A"}], \
        "iu/iv against the CONTENT box, not the element box center"
    assert out["marks"] == {"a": True}


def test_the_viewer_stacks_over_the_sent_popup_and_escape_peels_in_order(html):
    """The popup's overview thumb opens the full-size viewer, so the viewer
    must land ON TOP (its z-index above the popup's) and Escape must close the
    viewer first, the popup second (Bugbot, #661)."""
    view_z = int(re.search(r"#shotview \{[^}]*z-index: (\d+)", html).group(1))
    pop_z = int(re.search(r"#sentpop \{[^}]*z-index: (\d+)", html).group(1))
    assert pop_z < view_z, "the viewer must not open BEHIND the popup"
    handler = _between(html, 'if (e.key !== "Escape" || sentPop.hidden) return;',
                       "}, true);")
    assert "if (!shotView.hidden) shotViewClose();" in handler
    assert "else sentPopClose();" in handler


def test_an_overview_that_encoded_as_webp_is_uploaded_under_a_webp_name(html):
    """End to end, because the naming is where a silent WebP failure would do its
    damage: the path in the pictures block is the one the agent Reads, and a
    `.webp` holding PNG bytes is a file it cannot open."""
    out = _capture(html, """
shotEncodeBadged = async () => ({size: 10, type: "image/webp"});
const a = {id: "a", label: "A", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  const r = await annCaptureOverview([a]);
  console.log(JSON.stringify({view: r.view, uploaded: uploaded}));
})();
""")
    assert out["view"].endswith("-overview.webp")
    assert out["uploaded"] == [out["view"]]


def test_a_gone_element_and_an_offscreen_point_get_reasons_not_badges(html):
    """A note whose spot cannot be found on the pane still goes out — anchors are
    what Claude edits from — but it must say WHY it has no badge, or the model
    hunts the overview for a letter that is not there."""
    out = _capture(html, """
const gone = {id: "g", label: "A", el: null};
const far = {id: "f", label: "B", kind: "point", x: 2000, y: 50};
const ok = {id: "k", label: "C", el: {name: "k", contains: () => false}};
RECTS.k = {left: 10, top: 20, width: 100, height: 50};
annotations = [gone, far, ok];
(async () => {
  const r = await annCaptureOverview([gone, far, ok]);
  annApplyOverview([gone, far, ok], r);
  console.log(JSON.stringify({badges: LAST_ENCODE.badges.map((b) => b.label),
    gone: gone.offscreen, far: far.offscreen, ok: ok.offscreen === undefined}));
})();
""")
    assert out["badges"] == ["C"], "only the findable note is badged"
    assert "not in the app's DOM" in out["gone"]
    assert "scrolled out of the visible pane" in out["far"]
    assert out["ok"] is True


def test_a_blank_webgl_region_annotates_the_overview_never_suppresses_it(html):
    """Suppressing the overview leaves the message with no picture at all, so a
    blank map ships WITH prose naming which rectangles show the app's backdrop
    instead of what was drawn — bounded doubt, which keeps its reassurance."""
    out = _capture(html, """
const cv = {name: "cv"};
RECTS.cv = {left: 0, top: 0, width: 400, height: 600};
BLANKS.push(cv);
const a = {id: "a", label: "A", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  const r = await annCaptureOverview([a]);
  console.log(JSON.stringify({view: r.view, note: r.viewNote}));
})();
""")
    assert out["view"], "a real picture, not a refusal"
    assert "400x600 at (0,0)" in out["note"], out["note"]
    assert "preserveDrawingBuffer" in out["note"]
    assert "The rest of the image is what the user saw." in out["note"]


def test_an_unreadable_pane_says_so_on_the_overview_itself(html):
    """No document to capture at all — mid-navigation, or a project with no app
    entry. The overview slot gets the explicit sentence rather than silence, and
    no file is written for a picture that is not one."""
    out = _node(_CAPTURE_FNS, _CAPTURE_STUBS + """
shotPane = async () => null;
const a = {id: "a", label: "A", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  const r = await annCaptureOverview([a]);
  annApplyOverview([a], r);
  console.log(JSON.stringify({view: r.view, note: r.viewNote,
    uploaded: uploaded, sentence: APP_STATE_UNREADABLE,
    keys: Object.keys(a).sort()}));
})();
""", html)
    assert out["view"] is None
    assert out["sentence"] in out["note"]
    assert out["uploaded"] == []
    assert "offscreen" not in out["keys"], \
        "an absent picture is the picture's news, not every note's"


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


def test_an_incomplete_capture_says_so_on_the_overview(html):
    """Silence would present a half-CSS render as "the pane as the user saw
    it" — the same class of misread as shipping a blank canvas. Checked for two
    different causes, since the wording is what the agent acts on."""
    def cap(cause):
        return _node(_CAPTURE_FNS, _CAPTURE_STUBS + ("""
shotPane = async () => ({canvas: {}, width: 800, height: 600, blanks: [],
                        styled: 3000, incomplete: "%s"});
shotEncodeBadged = async () => ({size: 10});
const a = {id: "a", label: "A", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  const r = await annCaptureOverview([a]);
  console.log(JSON.stringify({view: r.view, note: r.viewNote}));
})();
""" % cause), html)

    big = cap("elements")
    assert big["view"], "an incomplete capture still produces a usable picture"
    assert "more elements" in big["note"] and "3000" in big["note"]

    slow = cap("deadline")
    assert slow["view"]
    assert "time" in slow["note"]
    assert "more elements" not in slow["note"], \
        "a slow capture must not tell the agent the page is too big"

    mutated = cap("mutated")
    assert "while the capture was running" in mutated["note"]


# ------------------------- how much of a caveated shot the agent should trust

def test_a_re_render_warning_does_not_also_call_the_overview_trustworthy(html):
    """`viewNote` may ride a real `view` — but only if the note and its closing
    line agree about HOW MUCH to trust. A `mutated` capture's note says the
    picture may not match the screen, and the standard closer said "the rest is
    what the user saw": one instruction to distrust and one to trust, which
    cancel to nothing."""
    out = _node(_CAPTURE_FNS, _CAPTURE_STUBS + """
shotPane = async () => ({canvas: {}, width: 800, height: 600, blanks: [],
                        styled: 3000, incomplete: "mutated"});
shotEncodeBadged = async () => ({size: 10});
const a = {id: "a", label: "A", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  const r = await annCaptureOverview([a]);
  console.log(JSON.stringify({note: r.viewNote, view: r.view}));
})();
""", html)
    assert out["view"], "the picture is still sent — this is about the wording"
    assert "what the user saw" not in out["note"], out["note"]
    # an unbounded doubt earns "corroborate", not "ignore that corner"
    assert "anchor" in out["note"] and "DOM outline" in out["note"]


def test_the_worst_doubt_decides_the_closing_instruction(html):
    """Both at once: a blank region AND a re-render. The caveats are read as one
    instruction, so the closer follows the WORST of them — an unbounded doubt is
    not cancelled by a bounded one also being present."""
    out = _node(_CAPTURE_FNS, _CAPTURE_STUBS + """
shotPane = async () => ({canvas: {}, width: 800, height: 600,
                        blanks: [{name: "cv"}],
                        styled: 3000, incomplete: "mutated"});
shotEncodeBadged = async () => ({size: 10});
RECTS.cv = {left: 0, top: 0, width: 400, height: 600};
const a = {id: "a", label: "A", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  const r = await annCaptureOverview([a]);
  console.log(JSON.stringify({note: r.viewNote}));
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

def test_a_thrown_capture_degrades_to_sending_the_annotations_without_a_picture(html):
    """The non-negotiable one: a user losing their typed message because a
    screenshot did not work is not a trade this feature makes."""
    out = _node(_CAPTURE_FNS + ["let targetNoun", "let paneNoun",
                                "const ANN_TAG", "const ANN_NO_WORDS",
                                "function annClock(", "function annStanza(",
                                "function formatAnnotations("],
                _CAPTURE_STUBS + """
var console2 = console;
console = {warn: () => {}};
annCaptureOverview = async () => { throw new Error("canvas exploded"); };
const a = {id: "a", content: "this is wrong", anchorPath: "div:nth-of-type(1)",
           tag: "div", sent: 0, createdAt: 1};
(async () => {
  const r = await annOverview([a]);
  annApplyOverview([a], r);
  console2.log(JSON.stringify({view: r.view, note: r.viewNote,
                               block: formatAnnotations([a]),
                               keys: Object.keys(a).sort()}));
})();
""", html)
    assert out["view"] is None
    assert out["note"].startswith("no overview screenshot")
    # no `offscreen` key on the notes — "we did not capture" is the picture's
    # news, not a per-note excuse — and the wire never carried a `shot` field
    assert "offscreen" not in out["keys"]
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
annCaptureOverview = async () => {
  for (let i = 0; i < 100; i++) {
    const until = Date.now() + 3;
    while (Date.now() < until) {}                  // synchronous cost...
    await new Promise((r) => setTimeout(r, 0));    // ...that yields between chunks
  }
  finished = true;
  return {view: "/tmp/x.png", marks: {}};
};
const a = {id: "a"};
(async () => {
  const r = await annOverview([a]);
  annApplyOverview([a], r);
  console.log(JSON.stringify({view: r.view, note: r.viewNote,
                              off: a.offscreen === undefined,
                              finished: finished}));
})();
""", html)
    assert out["view"] is None
    assert "abandoned" in out["note"]
    assert out["off"] is True
    assert out["finished"] is False
    assert "const SHOT_TIMEOUT_MS" in html, "the real cap still has to exist"


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


def test_a_send_with_no_annotations_captures_no_overview(html):
    """The overview exists FOR the annotations: a plain message, or one carrying
    only the user's own pictures, must be byte-identical to one from before the
    overview existed — no capture, no file, no block."""
    out = _node([n for n in _CAPTURE_FNS if n != "const SHOT_TIMEOUT_MS"] + _WIRE_ALSO,
                "var SHOT_TIMEOUT_MS = 50;\n" + _CAPTURE_STUBS + """
(async () => {
  const r = await annCaptureOverview([]);
  console.log(JSON.stringify({empty: r === null, uploaded: uploaded,
    outgoing: composeOutgoing("just words", [], null, null)}));
})();
""", html)
    assert out["empty"] is True
    assert out["uploaded"] == []
    assert out["outgoing"] == "just words"


def test_a_screenshot_only_send_collapses_to_a_marker(html):
    """The marker logic reads what the message CARRIED. A picture with no typed
    words is a real send — most of the point of a screenshot button — so the bubble
    has to name what went out instead of rendering empty."""
    out = _wire(html, """
const failed = {kind: "pane", view: null,
                viewNote: "no pane screenshot: it could not be saved"};
const wire = composeOutgoing("", [], null, [failed]);
console.log(JSON.stringify({stripped: stripBlocks(wire),
                            marker: isMarkerOnly(stripBlocks(wire))}));
""")
    assert out["stripped"] == "\U0001f5bc pane screenshot"
    assert out["marker"] is True, "still non-identifying, so re-attach must refuse it"


def test_a_block_written_before_this_button_existed_still_strips(html):
    """The permanent obligation, and the reason `_LEGACY_WIRE` is still hand-built:
    the writer of these blocks was deleted and later rewritten with a different
    caption, so a session on disk holds a shape nothing in the page produces today.
    The stripper keys on the TAG and not on the caption, which is what makes that
    safe."""
    out = _wire(html, """
""" + _LEGACY_WIRE + """
const view = {view: "/tmp/fr/shots/S-view.png", viewNote: "part is blank"};
const old = legacyWire("fix this", [], null, view);
const now = composeOutgoing("fix this", [], null, view);
console.log(JSON.stringify({old: old, sameText: old === now,
                            oldStripped: stripBlocks(old),
                            nowStripped: stripBlocks(now)}));
""")
    assert "legacy caption" in out["old"], "the fixture really is the older shape"
    assert out["sameText"] is False, "and the page does not write that caption now"
    assert out["oldStripped"] == "fix this"
    assert out["nowStripped"] == "fix this"


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
annCaptureOverview = async () => {
  await new Promise((r) => setTimeout(r, 60));
  throw new Error("the upload failed after we stopped listening");
};
const a = {id: "a"};
(async () => {
  const r = await annOverview([a]);
  await new Promise((res) => setTimeout(res, 120));   // outlive the abandoned one
  console2.log(JSON.stringify({view: r.view, unhandled: unhandled,
                               warned: warned.length}));
})();
""", html)
    assert out["view"] is None
    assert out["unhandled"] == []
    assert out["warned"] >= 1, "and it is not swallowed silently either"


def test_an_abandoned_captures_thumbnail_is_released(html):
    """`annCaptureOverview` mints an object URL for the thumbnail. When the
    timeout wins the result is discarded, so that URL pins its Blob for the
    page's lifetime with nothing left holding a handle to revoke it."""
    out = _node([n for n in _CAPTURE_FNS if n != "const SHOT_TIMEOUT_MS"],
                "var SHOT_TIMEOUT_MS = 10;\n" + _CAPTURE_STUBS + """
var revoked = [];
URL.revokeObjectURL = (u) => revoked.push(u);
annCaptureOverview = async () => {
  await new Promise((r) => setTimeout(r, 60));
  return {view: "/tmp/x.png", thumb: "blob:late", marks: {}};
};
const a = {id: "a"};
(async () => {
  const r = await annOverview([a]);
  await new Promise((res) => setTimeout(res, 120));   // outlive the abandoned one
  console.log(JSON.stringify({view: r.view, revoked: revoked}));
})();
""", html)
    assert out["view"] is None, "the discarded result carries no path"
    assert out["revoked"] == ["blob:late"]


def test_a_stale_claim_from_a_failed_send_is_not_quietly_re_sent(html):
    """A send that never launched rolls the notes back to pending, flags and all.
    Re-sending a claim about a picture of an older screen is worse than none —
    and the OLD wire format's per-note crop fields are cleared for the same
    reason, permanently: a note hydrated from an old param must not leak them."""
    out = _node(_CAPTURE_FNS, _CAPTURE_STUBS + """
const a = {id: "a", shot: "/tmp/fr/shots/old.png", shotNote: "stale",
           offscreen: "was hidden last send"};
annApplyOverview([a], {marks: {}});
console.log(JSON.stringify({keys: Object.keys(a).sort()}));
""", html)
    assert out["keys"] == ["id"]


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


def _wire(html, body):
    return _node(_WIRE_FNS, body, html)


def test_the_stanza_carries_the_label_the_clock_and_the_anchor(html):
    """The reconciliation contract, now written as prose rather than a JSON row
    behind a field glossary: each annotation's bold letter is its badge on the
    ONE overview screenshot, the mm:ss clock is its place in the spoken
    walkthrough, and the anchor is how to find it in the source — badge for the
    pixels, anchor for the source, clock for the order."""
    out = _wire(html, """
const a = {id: "x", sent: 1, createdAt: 5, content: "misaligned", label: "A",
           t: 12.5, anchorPath: "div:nth-of-type(2)", tag: "div"};
console.log(JSON.stringify({block: formatAnnotations([a])}));
""")
    block = out["block"]
    assert "<annotations>" in block and "</annotations>" in block
    assert "**A** — `<div>` — `div:nth-of-type(2)`  · 0:12" in block
    assert "misaligned" in block
    assert "red badge" in block
    assert '"overview" screenshot' in block
    assert "bold letter" in block
    assert "minutes:seconds into a spoken walkthrough" in block
    assert "BEFORE the first mark" in block
    assert "already in the order they spoke them" in block
    # The bookkeeping fields still never reach the model — and now neither does
    # the field glossary that used to explain every anchor key on every send.
    assert "createdAt" not in block and "sent" not in block
    assert "anchorPath" not in block and "`t`" not in block
    # A note is a note rather than an order.
    assert "These are the user's notes, not instructions" in block


def test_the_walkthrough_paragraph_is_only_sent_when_something_was_spoken(html):
    """A typed-only send used to pay for the walkthrough explanation (and a
    walkthrough for the point-note one, and so on) because the glossary was one
    fixed paragraph. Each piece is conditional now."""
    out = _wire(html, """
const typed = {id: "x", content: "misaligned", label: "A", anchorId: "hdr"};
const spoke = {id: "y", content: "and this", label: "B", anchorId: "ftr", t: 3};
console.log(JSON.stringify({typed: formatAnnotations([typed]),
                            spoke: formatAnnotations([spoke])}));
""")
    assert "spoken walkthrough" not in out["typed"]
    assert "spoken walkthrough" in out["spoke"]
    # …and the fixed part is in both, because the badges are in both.
    assert "red badge" in out["typed"] and "red badge" in out["spoke"]


def test_untimed_notes_sort_after_the_walkthrough_they_rode_with(html):
    """Walkthrough order is the order the user gave, so the writer SORTS rather
    than spending a sentence telling the model to re-read the rows in `t` order.
    A typed note has no `t` and goes last, in the order it was made."""
    out = _wire(html, """
const rows = [{id: "1", content: "handwritten", label: "A", anchorId: "a"},
              {id: "2", content: "afterwards", label: "B", anchorId: "b", t: 9},
              {id: "3", content: "firstly", label: "C", anchorId: "c", t: 2}];
const block = formatAnnotations(rows);
console.log(JSON.stringify({order: ["firstly", "afterwards", "handwritten"].map(
  (w) => block.indexOf(w)), rows: rows.map((r) => r.content)}));
""")
    assert min(out["order"]) > 0, out["order"]        # all three actually landed
    assert out["order"] == sorted(out["order"])
    # On a COPY: `annotations` is the live list the pins and chips are drawn
    # from, and reordering it under them would renumber every badge mid-send.
    assert out["rows"] == ["handwritten", "afterwards", "firstly"]


def test_a_badgeless_note_travels_with_the_reason_it_has_no_badge(html):
    """And it travels WITH the entry it is about, not in a paragraph up top that
    would have to name which labels it applies to."""
    out = _wire(html, """
const a = {id: "x", content: "the map is empty", anchorId: "map", label: "B",
           offscreen: "the spot was scrolled out of the visible pane"};
console.log(JSON.stringify({block: formatAnnotations([a])}));
""")
    assert ("_no badge on the overview: the spot was scrolled out of the "
            "visible pane_\nthe map is empty") in out["block"]


def test_a_blank_line_inside_a_note_cannot_end_its_stanza(html):
    """A blank line is the stanza boundary, and the composer takes a newline on
    Shift+Enter — so two of them inside one note used to end the stanza mid-note
    and every reader dropped the rest of the user's words (Bugbot, PR #783).
    Single line breaks survive: they are the user's and cost nothing."""
    out = _wire(html, """
const a = {id: "x", label: "A", anchorId: "hdr", tag: "header",
           content: "first thought\\n\\n\\nsecond thought\\nthird"};
const b = {id: "y", label: "B", anchorId: "ftr", content: "and this one"};
const block = formatAnnotations([a, b]);
console.log(JSON.stringify({block, paras: block.split(/\\n\\s*\\n/).length}));
""")
    # preamble + two stanzas, not preamble + three
    assert out["paras"] == 3, out["block"]
    assert "first thought\nsecond thought\nthird" in out["block"]


def test_an_element_digest_that_quotes_coordinates_is_not_read_as_a_point(html):
    """The heading is parsed positionally, not searched: an element note whose
    `text` digest happens to say "point (12, 34)" used to come back as
    `kind: "point"` carrying the digest's numbers as the spot the user clicked
    (Bugbot, PR #783)."""
    out = _node(_WIRE_FNS + _READER_FNS, """
const a = {id: "x", label: "A", anchorId: "chart", tag: "figcaption",
           text: "Nearest point (12, 34) on the trend line", content: "wrong axis"};
const back = annotationsIn(formatAnnotations([a]));
console.log(JSON.stringify({back}));
""", html)
    got = out["back"][0]
    assert "kind" not in got and "x" not in got and "y" not in got, got
    assert got["tag"] == "figcaption" and got["content"] == "wrong axis"


def test_a_note_that_is_one_emphasised_word_survives_the_restore(html):
    """The machine lines are matched EXACTLY, not as "any wholly-italic line" —
    otherwise a user whose whole note is `_gone_` loses it to a rule about our
    own prose."""
    out = _node(_WIRE_FNS + _READER_FNS, """
const a = {id: "x", label: "A", anchorId: "hdr", tag: "h1", content: "_gone_"};
const b = {id: "y", label: "B", anchorId: "ftr", tag: "p", content: ""};
const back = annotationsIn(formatAnnotations([a, b]));
console.log(JSON.stringify({back}));
""", html)
    assert out["back"][0]["content"] == "_gone_"
    # …and the real placeholder still reads as "no words", not as the words.
    assert out["back"][1]["content"] == ""


def test_a_point_note_names_what_it_landed_inside(html):
    """The spot is the note, but a spot is usually inside something — and a
    point with nothing under it says so rather than going out as bare
    coordinates the model can only look at."""
    out = _wire(html, """
const inside = {id: "x", kind: "point", x: 412, y: 690, label: "A",
                content: "no chart here", nearPath: "div:nth-of-type(3)"};
const bare = {id: "y", kind: "point", x: 40, y: 40, label: "B",
              content: "wrong margin"};
console.log(JSON.stringify({inside: formatAnnotations([inside]),
                            bare: formatAnnotations([bare])}));
""")
    assert "**A** — point (412, 690) inside `div:nth-of-type(3)`" in out["inside"]
    assert "**B** — point (40, 40) — no element under it" in out["bare"]


def test_annotations_with_no_capture_carry_no_picture_fields_at_all(html):
    """The degrade path's wire shape: no badge caveat (and no legacy `shot`) at
    all — a turn where the capture failed carries clean anchors only."""
    out = _wire(html, """
const a = {id: "x", content: "hi", anchorId: "b", tag: "button"};
console.log(JSON.stringify({block: formatAnnotations([a]), n: 1}));
""")
    # "screenshot" is in the preamble, so the absence to pin is the FIELD, not
    # the substring: no crop path, no per-note note about one, no badge caveat.
    assert "shotNote" not in out["block"]
    assert "/tmp" not in out["block"] and ".png" not in out["block"]
    assert "no badge on the overview" not in out["block"]


def test_a_send_with_no_annotations_composes_nothing_about_shots(html):
    out = _wire(html, """
console.log(JSON.stringify({out: composeOutgoing("just words", [], null)}));
""")
    assert out["out"] == "just words"


def test_a_legacy_per_note_crop_never_reaches_the_wire_at_all(html, agent):
    """`shot`/`shotNote` were the OLD wire format's per-note crops, and a note
    hydrated from a param written back then still carries them —
    `annApplyOverview` deletes them before every send for staleness, and the
    markdown writer has no field for them either. So the path cannot reach the
    transcript for the strongest possible reason: it never goes out.

    The live picture path rides the pane-shot block instead, and never leaving
    THAT one behind is the sibling test above."""
    out = _wire(html, """
const a = {id: "x", content: "misaligned", anchorId: "hdr", tag: "header",
           shot: "/tmp/fr/shots/20260804-A.png"};
const wire = composeOutgoing("fix this", [a], {entry: "/p/index.html"});
console.log(JSON.stringify({wire: wire, stripped: stripBlocks(wire)}));
""")
    assert "/tmp/fr/shots/20260804-A.png" not in out["wire"]
    assert out["stripped"] == "fix this"
    # agent.py's half of the same wire: it strips the app-state block for
    # meta.json (the session-list preview, the commit subject, the re-attach match),
    # and the annotation block is the page's to strip — but NEITHER may leave a
    # path behind in what it hands on.
    meta = agent._strip_app_state(out["wire"])
    assert not meta.startswith("<%s>" % agent.APP_STATE_TAG)
    assert meta.startswith("<annotations>")


def test_an_annotation_only_send_still_collapses_to_a_marker(html):
    """Unchanged by shots: the strip has to survive a message that is nothing but
    annotations, or the bubble shows raw JSON."""
    out = _wire(html, """
const a = {id: "x", content: "here", anchorId: "hdr",
           shot: "/tmp/fr/shots/A.png"};
console.log(JSON.stringify({stripped: stripBlocks(composeOutgoing("", [a], null))}));
""")
    assert out["stripped"] == "\U0001f4cc annotations"


def test_the_annotation_wire_reader_is_the_exact_counterpart_of_the_writer(html):
    """Round trip: what composeOutgoing writes, annotationsIn reads back — with
    `label`, `t`, `tag`, the point coordinates and `offscreen` intact — so a
    reloaded session can rebuild the receipt rows and the "what was sent" popup
    instead of collapsing a send to the bare marker. Unparseable blocks answer an
    empty list, never a throw.

    Read back out of PROSE now, which is lossless for everything the two readers
    of these objects render (annReceiptRow: label, content, tag; the popup adds
    kind, x, y, t, offscreen) and lossy for exactly one thing: `t`'s tenth of a
    second, because the stanza prints mm:ss. Nothing reads the tenth — the popup
    renders `annRecClock(c.t)`, which is that same mm:ss — and the ANCHOR does
    not come back either, for the same reason: no reader shows it. Both are
    pinned below so a future reader that DOES want them fails here rather than
    silently rendering blanks."""
    out = _node(_WIRE_FNS + _READER_FNS, """
const a = {id: "x", sent: 1, createdAt: 5, content: "misaligned", label: "A",
           t: 12.5, anchorPath: "div:nth-of-type(2)", tag: "div"};
const b = {id: "y", content: "the map is empty", label: "B", kind: "point",
           x: 40, y: 50, offscreen: "the spot was scrolled out of the visible pane"};
const overview = {kind: "overview", view: "/tmp/fr/shots/S-overview.png"};
const wire = composeOutgoing("fix this", [a, b], {entry: "/p/index.html"},
                             [overview]);
console.log(JSON.stringify({
  back: annotationsIn(wire),
  none: annotationsIn("just words"),
  mangled: annotationsIn("The user annotated 1 thing. \\n```json\\n{oops\\n```"),
}));
""", html)
    back = out["back"]
    assert [c["label"] for c in back] == ["A", "B"]
    assert [c["content"] for c in back] == ["misaligned", "the map is empty"]
    assert back[0]["tag"] == "div"
    # mm:ss, so 12.5 comes home as 12 — see the docstring
    assert back[0]["t"] == 12
    assert "anchorPath" not in back[0]
    assert back[1]["kind"] == "point"
    assert (back[1]["x"], back[1]["y"]) == (40, 50)
    assert back[1]["offscreen"] == "the spot was scrolled out of the visible pane"
    # the bookkeeping fields were never written, so they cannot come back
    assert all("id" not in c and "sent" not in c and "createdAt" not in c
               for c in back)
    assert out["none"] == []
    assert out["mangled"] == []


def test_a_screenshot_beside_comments_opens_the_popup_not_the_bare_viewer(html):
    """When comments ride the message the picture belongs to them: clicking its
    thumb opens the "what was sent" popup (which shows the picture, with a
    click-through to the full-size viewer). Only a comment-less send keeps the
    plain viewer on its thumb — sentPopWire is simply never called for one."""
    wire = _between(html, "function sentPopWire(sum, payload)", "\n}\n")
    assert '".annsum-pane"' in wire
    assert "sentPopOpen(payload)" in wire
    live = _between(html, "async function sendMessage(message)", "\n}\n")
    assert "if (pending.length) {\n      sentPopWire(sum," in live, \
        "a picture-only send must not be wired to the popup"
    restore = _between(html, "function shotRestoreReceipt(turn, text)", "\n}\n")
    assert "if (notes.length) {" in restore


def test_a_restored_turn_rebuilds_the_receipt_rows_from_the_wire(html):
    """The restore path draws through the same builders the live send uses —
    annReceiptRow for the rows, sentPopWire for the popup — so the two receipt
    renderers cannot drift, and a reload keeps the comments clickable."""
    body = _between(html, "function shotRestoreReceipt(turn, text)", "\n}\n")
    assert "annotationsIn(text)" in body
    assert "annReceiptRow(c)" in body
    assert "sentPopWire(sum," in body
    live = _between(html, "async function sendMessage(message)", "\n}\n")
    assert "annReceiptRow(c)" in live
    assert "sentPopWire(sum," in live


def test_a_zero_click_walkthrough_sends_its_whole_transcript_as_the_prompt(html):
    """The degenerate case of the before-the-first-click rule: record, talk,
    never click, stop — everything came before the first click, so the whole
    transcript is the prompt rather than being discarded."""
    body = _between(html, "async function annRecEnd()", "\n}\n")
    # only a recording with no FILE behind it bails before transcribing — a
    # failed stop, an empty file, or a stop that landed on the start's own beat
    # (`fused.capture.audio` now writes the file, SPEC §45). Silence is not
    # this branch: it goes to the transcriber and comes back wordless.
    assert "if (!out || !out.path || !out.bytes || (out.seconds || 0) < 0.4) {" in body
    assert "!ids.length" not in body.split("transcribe")[0], \
        "a click-less recording still reaches the transcriber"
    assert "ids.length\n      ? annRecAssign(ids, rec.segments)" in body


def test_every_marker_a_strip_can_produce_is_known_to_be_non_identifying(html):
    """D146, and it has already drifted once. `resumeRun` refuses to match a prior
    turn on a marker because every such send collapses to the SAME text, so it
    identifies no particular turn and a false match trims another turn's assistant
    rows. It excluded the annotations marker by literal — then the pane shot added
    two more marker shapes and the check did not know about them, so a
    screenshot-only send could match the wrong turn and destroy real transcript
    content.

    The markers are DERIVED here from stripBlocks itself rather than listed, so a
    fourth marker added later without teaching `isMarkerOnly` about it fails this
    test instead of silently reintroducing the bug."""
    out = _wire(html, """
const a = {id: "x", content: "here", anchorId: "hdr"};
const view = {kind: "pane", view: "/tmp/fr/shots/S-view.png"};
const pasted = {kind: "image", view: "/tmp/fr/shots/S.png", name: "bug.png"};
// Every combination that can leave the user's own words empty. All of them are
// reachable from the composer: notes alone, a picture alone, a PASTED picture
// alone, and those together, with or without an app-state block in front.
const produced = [
  stripBlocks(composeOutgoing("", [a], null)),
  stripBlocks(composeOutgoing("", [], null, [view])),
  stripBlocks(composeOutgoing("", [a], null, [view])),
  stripBlocks(composeOutgoing("", [a], {entry: "/p"}, [view])),
  stripBlocks(composeOutgoing("", [], null, [pasted])),
  stripBlocks(composeOutgoing("", [], null, [view, pasted])),
];
console.log(JSON.stringify({
  produced: produced,
  verdicts: produced.map(isMarkerOnly),
  real: ["fix the header", "📌 annotations please", "", "🖼"].map(isMarkerOnly),
}));
""")
    # every marker-only send really does collapse to a non-identifying text...
    assert all(out["produced"]), out["produced"]
    assert len(set(out["produced"])) == 4, out["produced"]
    # a send of nothing but pasted images says IMAGES, not "pane screenshot": the
    # bubble is the only record of a wordless turn, and naming a photo the user
    # brought in as a picture of this app is the one thing it must not do
    assert out["produced"][4] == "🖼 images"
    # ...while a mixed send is still led by the pane, which is the wider claim
    assert out["produced"][5] == "🖼 pane screenshot"
    # ...and the predicate recognises every one of them
    assert out["verdicts"] == [True] * 6, out["produced"]
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
    """Same requirement as a crop path: the path is for the model, and a user who
    attached a picture must see their own sentence rather than a temp path. Every
    combination is checked because the strip ORDER is what makes it work — the
    annotation block is only recognised at position zero, so the pane block has to
    come off first."""
    out = _wire(html, """
const a = {id: "x", content: "here", anchorId: "hdr", shot: "/tmp/fr/shots/A.png"};
const view = {kind: "pane", view: "/tmp/fr/shots/S-view.png",
              viewNote: "part is blank"};
const st = {entry: "/p/index.html"};
const cases = {
  all: composeOutgoing("fix this", [a], st, [view]),
  viewAndState: composeOutgoing("fix this", [], st, [view]),
  viewOnly: composeOutgoing("fix this", [], null, [view]),
  viewNoWords: composeOutgoing("", [], null, [view]),
  annAndViewNoWords: composeOutgoing("", [a], null, [view]),
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
    "did my edit land" symbolically. The whole-pane picture and the crops are BOTH
    the user's own act — a button they pressed, a note they wrote — and neither is
    something the model may reach for."""
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
    """D146. Two controls carried an on/off accent — the composer's pane-shot pill
    and the left pane's banner — and both were one class + one qualifier away from
    the same cascade bug: `.pill:hover` is (0,2,0) and so is a pill's own
    `[aria-pressed="true"]`, so the later rule (hover) won and an ARMED toggle
    repainted itself neutral under the cursor, losing the only signal that the next
    send carried a picture.

    The pill that motivated it is gone twice over — deleted, then replaced by a
    button with no pressed state at all (it captures on click, and the chip above
    the composer is the receipt, so there is nothing to keep painted). The RULE
    stays, stated in the selectors rather than left to source order, so the next
    pressable pill in that row inherits the fix rather than rediscovering it.

    `:not(:disabled)` joined it for the same argument on a second state: the row
    now has a pill that goes dead while a scheduled message is pending (.schedbtn,
    see tests/test_claude_composer_block.py), and a dimmed control that still
    lights up under the cursor is this bug with the roles swapped."""
    pairs = [('.pill:hover:not([aria-pressed="true"]):not(:disabled)',
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


def test_a_rolled_back_send_releases_its_thumbnail(html):
    """The receipt's removal detaches the only <img> holding the overview
    thumbnail's blob URL; an unrevoked one pins its Blob for the life of the
    page, and a retried send captures a fresh overview anyway."""
    out = _node(["function shotRevoke("], """
var revoked = [];
var URL = {revokeObjectURL: (u) => revoked.push(u)};
const r = {view: "/tmp/x.png", thumb: "blob:1"};
shotRevoke(r);
shotRevoke(r);   // a second call must be a no-op, not a double revoke
shotRevoke(null);
shotRevoke({view: null});
console.log(JSON.stringify({revoked: revoked, left: r.thumb}));
""", html)
    assert out["revoked"] == ["blob:1"]
    assert out["left"] == ""


def test_the_rollback_releases_the_thumbnails_it_just_removed(html):
    """Wired where the receipt is pulled, not somewhere hopeful: a thumbnail still
    on screen needs its URL alive."""
    start = html.index("    if (!started) {")
    branch = html[start:html.index("\n    }\n", start)]
    assert "receipt.remove()" in branch
    assert "shotRevoke(overview)" in branch


# ------------------ the pane-shot toggle wears the composer's own clothes

def test_the_annotate_control_is_two_buttons(html):
    """TWO separate buttons (Akshil, 2026-08-19; the split pill is gone):
    Comment on the left, Record on the right, each its own bordered control —
    an icon plus a word, the word a .lbl span the strip's container query can
    drop. The wrapper carries the row's auto margin and a gap, no border of
    its own. Armed is the accent filling the Comment button, the same voice as
    the picker's selected seat. The old second switch (#annvis, pin
    visibility) stays gone: pins follow the mode."""
    wrap = _between(html, "#anncta {", "}")
    assert "margin-left: auto" in wrap, "the wrapper anchors the row's right: " + wrap
    assert "gap: 8px" in wrap, wrap
    assert "border" not in wrap, "two buttons, not one pill: " + wrap
    # each button is its own bordered 26px control
    seat = _between(html, "#anncta button {", "}")
    assert "border: 1px solid var(--border)" in seat, seat
    assert "border-radius: 8px" in seat, seat
    assert "height: 26px" in seat, seat
    assert "white-space: nowrap" in seat, seat
    assert "var(--accent)" not in seat, "idle buttons carry no accent: " + seat
    # armed = accent OUTLINE, not a fill (Akshil, 2026-08-19): accent border
    # and ink on a quiet ground — the picker's selected seat keeps its fill
    on = _between(html, "#annbtn.on {", "}")
    assert "border-color: var(--accent)" in on, on
    assert "color: var(--accent)" in on, on
    assert "background: transparent" in on, on
    # the track-and-knob anatomy is gone with the switch
    assert "#annbtn .track {" not in html
    assert "#annbtn .knob {" not in html
    # named, announced, and labelled in the markup. The strip is sliced to the
    # kebab, not the first </div>: the tool picker sits BEFORE the buttons (it
    # unfolds to their left), and its own </div> would end the slice early.
    view = _between(html, '<div id="anntools">', '<div id="kebab">')
    assert 'id="annbtn"' in view and 'aria-label="' in view, view
    # Comment left, Record right — reading order inside the wrapper
    cta_markup = _between(view, '<div id="anncta">', "</div>")
    assert cta_markup.index('id="annbtn"') < cta_markup.index('id="annrec"'), cta_markup
    # both buttons carry an icon AND a word
    annbtn_markup = _between(view, '<button id="annbtn"', "</button>")
    assert "<svg" in annbtn_markup and 'class="lbl cmt-word"' in annbtn_markup, annbtn_markup
    annrec_markup = _between(view, '<button id="annrec"', "</button>")
    assert "<svg" in annrec_markup and 'class="lbl rec-word"' in annrec_markup, annrec_markup
    assert 'annBtn.querySelector(".lbl").textContent' not in html
    # the second switch is gone entirely
    assert "annvis" not in html and "annVisBtn" not in html
    assert "✎" not in html, "the pencil glyph is gone from the template"


def test_annotate_mode_defaults_off_and_owns_pin_visibility(html):
    """The one switch starts OFF: only an explicit "1" — the user sliding it on
    — arms it, and an unset param (a first visit, or a pane the reader just
    opened) leaves the comment layer down, so clicks reach the app. Encoded as
    `=== "1"` rather than `!== "0"` so ABSENT reads as disarmed.
    Pin visibility and auto-send have no params of their own any more: pins
    follow the mode, and Done always sends the pending notes."""
    assert 'annSetMode(fused.params.get("annmode") === "1")' in html
    assert "annshow" not in html
    assert "annautosend" not in html
    # pins gate on the mode itself, and toggling the mode repaints them
    assert 'annPins.style.display = annOn ? "" : "none"' in html
    assert "renderAnn()" in _between(html, "function annSetMode(", "\n}")
    # ...and reading the default back through the one writer must not WRITE it.
    # runtime.js pushes a history entry on the first param write of a pristine
    # entry (so Back reaches the URL as loaded), so a boot-time normalisation of
    # an absent `annmode` burns a second entry: expanding the preview pane to
    # full screen took TWO presses of Back to undo. The DOM and state work still
    # runs; only the write is conditional, and only on a no-op — writing "1"
    # over an absent param is a real arm and still pushes.
    mode = _between(html, "function annSetMode(on) {", "\nannBtn.addEventListener")
    assert 'if ((fused.params.get("annmode") === "1") !== annOn) {' in mode
    assert mode.count('fused.params.set("annmode"') == 1
    # Done's send is unconditional (bar the in-flight guard) — an empty
    # composer sends bare, the annotations carrying the content. Saving a note
    # sends nothing: notes pool until Done (Akshil, 2026-09-04).
    assert "if (pending && (activeRun || !sending)) annAutoSubmit();" in html
    assert "isNew && !sending" not in html
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
    # the buttons themselves are plain flow children — no absolute anchoring left
    btn = _between(html, "#anncta button {", "}")
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
    assert 'id="annbtn"' in _between(html, '<div id="anntools">', '<div id="kebab">')
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
    # the armed tooltip is the shared const (annRecEnd restores it too), and
    # the idle half is the shared builder — one definition each, two writers
    assert "ANN_ARMED_TITLE" in title and "annIdleTitle()" in title, title
    armed = _between(html, "const ANN_ARMED_TITLE =", ";")
    assert "Esc" in armed and "click this button" in armed, armed
    assert html.count('+ ", then send the notes to Claude"') == 1, \
        "the idle tooltip has one definition, annIdleTitle"


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


def _view(html, body):
    return _node(_VIEW_FNS, _CAPTURE_STUBS + _CAPTURE_TAIL + body, html)


def test_the_button_photographs_the_whole_pane_at_its_own_caps(html):
    """The rect is the entire bitmap, not an element's — that is the one question a
    crop cannot answer. And the caps are the pane's own: a whole pane squeezed into
    the crops' 640px edge is unreadable, which defeats the only reason to send one.
    """
    out = _view(html, """
(async () => {
  const r = await shotCapturePane(Date.now() + 5000);
  console.log(JSON.stringify({view: r.view, clean: r.viewNote === undefined,
                              thumb: r.thumb,
                              rect: LAST_ENCODE.rect, limits: LAST_ENCODE.limits}));
})();
""")
    assert out["rect"] == {"left": 0, "top": 0, "width": 800, "height": 600}
    assert out["limits"] == {"maxEdge": 1600, "maxBytes": 900 * 1024}
    # into the crops' own directory, so the agent's one Read(//<shots>/**) rule and
    # the same pruning cover it — no second grant for a second kind of picture
    assert out["view"].startswith("/tmp/fr/shots/")
    assert out["view"].endswith("-view.png")
    assert out["thumb"], "and a thumbnail, because the user has to see it first"
    assert out["clean"] is True, "a clean capture carries no caveat at all"


def test_a_pane_that_cannot_be_read_says_so_instead_of_failing_silently(html):
    """The user pressed a button. Handing back nothing at all would leave them
    wondering whether it worked; the chip and the wire both get a reason."""
    out = _view(html, """
shotPane = async () => null;
(async () => {
  const r = await shotCapturePane(Date.now() + 5000);
  console.log(JSON.stringify({view: r.view, note: r.viewNote,
                              thumb: r.thumb === undefined,
                              uploaded: uploaded}));
})();
""")
    assert out["view"] is None
    assert out["note"].startswith("no pane screenshot: ")
    assert out["thumb"] is True, "nothing to show, so no blob URL to leak"
    assert out["uploaded"] == [], "and nothing written for a picture that is not one"


def test_a_pane_over_budget_is_refused_rather_than_shrunk_past_reading(html):
    """`shotEncode` has already spent every quality step and both halvings by the
    time it answers null. A file past that is not worth the turn it would cost."""
    out = _view(html, """
shotEncode = async () => null;
(async () => {
  const r = await shotCapturePane(Date.now() + 5000);
  console.log(JSON.stringify({view: r.view, note: r.viewNote, uploaded: uploaded}));
})();
""")
    assert out["view"] is None
    assert str(900 * 1024) in out["note"], out["note"]
    assert out["uploaded"] == []


def test_a_blank_webgl_pane_is_annotated_and_never_suppressed(html):
    """The divergence from a crop, and it is deliberate: suppressing a blank crop
    leaves the other crops, while suppressing the pane leaves nothing at all. So
    the picture goes out with prose naming which rectangles show the app's backdrop
    instead of what was drawn — bounded doubt, which keeps its reassurance."""
    out = _view(html, """
const cv = {name: "cv"};
RECTS.cv = {left: 0, top: 0, width: 400, height: 600};
BLANKS.push(cv);
(async () => {
  const r = await shotCapturePane(Date.now() + 5000);
  console.log(JSON.stringify({view: r.view, note: r.viewNote}));
})();
""")
    assert out["view"], "a real picture, not a refusal"
    assert "400x600 at (0,0)" in out["note"], out["note"]
    assert "preserveDrawingBuffer" in out["note"]
    assert "The rest of the image is what the user saw." in out["note"]


def test_an_unfinished_capture_tells_the_reader_not_to_trust_the_picture(html):
    """A style walk that ran out of budget, or that saw the page re-render under
    it, produces a picture that can look perfectly plausible and be a blend of two
    moments. That doubt is UNBOUNDED, so the closing line is corroboration rather
    than reassurance — the same rule the crops follow, through the same two
    functions, so one capture cannot ask for two levels of trust."""
    out = _view(html, """
PANE.incomplete = "mutated";
PANE.styled = 40;
(async () => {
  const r = await shotCapturePane(Date.now() + 5000);
  console.log(JSON.stringify({note: r.viewNote}));
})();
""")
    assert "re-rendered while the capture was running" in out["note"]
    assert "Do not act on this image alone" in out["note"]
    assert "what the user saw." not in out["note"], \
        "an unbounded doubt must not also call the picture trustworthy"


def test_two_pictures_from_one_click_cannot_collide_on_a_filename(html):
    """The crops and the pane shot both mint names in the same directory, so they
    share ONE stamp writer. A second definition is how two files in one second
    come to want the same name."""
    assert html.count("function shotStamp(") == 1
    assert "new Date().toISOString()" in _between(html, "function shotStamp(", "\n}")
    # and neither caller builds its own
    for fn in ("async function annCaptureOverview(", "async function shotCapturePane("):
        body = _between(html, fn, "\n}\n")
        assert "toISOString" not in body, fn
        assert "shotStamp()" in body, fn


def test_the_capture_happens_on_the_click_and_not_during_the_send(html):
    """THE design of this control, in one assertion. The send path must not contain
    a capture for the pane: what it reads is a picture that already exists.

    That is what makes the rest possible — the user sees the picture before it
    goes, the moment photographed is the one they chose, a failure can be reported
    while there is still someone to report it to, and nobody who never presses the
    button pays a rasterise, an encode or a file."""
    click = _between(html, "async function shotAttachPane()", "\n}\n")
    assert "shotCapturePane(" in click
    send = _between(html, "async function sendMessage(message)", "\n}\n")
    assert "shotCapturePane" not in send, \
        "the send reads a picture, it does not take one"
    # the only capture the send still runs is the overview's, which is
    # annotation work
    assert "await annOverview(pending)" in send


def test_the_shot_belongs_to_exactly_one_message(html):
    """Cleared at the TOP of the send, before any await: a second send fired while
    this one is still running must not inherit the picture. And the chip goes at
    the same moment, which is what a user reads as "it went with that one"."""
    send = _between(html, "async function sendMessage(message)", "\n}\n")
    head = send[:send.index("const state = appStatePush();")]
    assert "const pics = shotAttached;" in head
    assert "shotAttached = [];" in head
    assert "renderAnn();" in head
    before = head.split("const pics = shotAttached;")[0]
    code = "\n".join(ln.split("//")[0] for ln in before.split("\n"))
    assert "await" not in code, \
        "nothing may await before the pictures are taken off the composer"
    # a picture alone is a sendable message: there is no rule that a screenshot
    # needs words to go with it
    assert "if (!message && !pending.length && !pics.length) { sending = false; return; }" \
        in send
    assert html.count(
        "if (!message && !annPending().length && !shotAttached.length) return;") == 3, \
        "the home composer, the chat composer, and its activeRun follow-up " \
        "branch all agree"


def test_the_thumbnail_never_reaches_the_wire(html):
    """`thumb` is a blob URL belonging to THIS page — a dead link everywhere else,
    and unreadable to the agent. Only the path and the note go."""
    send = _between(html, "async function sendMessage(message)", "\n}\n")
    compose = _between(send, "const wireViews = pics.map(", "const outgoing = composeOutgoing(")
    assert "s.view" in compose and "s.viewNote" in compose
    assert "thumb" not in compose, compose
    # and `kind` DOES go, because the array made it necessary: two paths with no
    # way to tell them apart would have the agent read a photo the user pasted as
    # a picture of the pane it is being asked about
    assert "s.kind" in compose, compose
    # `name` rides for BOTH kinds the user brought in — the block's own text leans
    # on it ("`name` is what they called it"), and a file whose name was dropped
    # reaches the agent as a bare timestamped path
    assert 's.kind === "image" || s.kind === "file"' in compose, compose
    # and `size` for a file only, absent when unknown rather than sent as 0: the
    # agent is about to Read it, and 3 KB versus 20 MB decides how
    assert 'if ((s.kind === "file" || s.kind === "image")' in compose, compose
    assert "&& typeof s.size === \"number\") wire.size = s.size;" in compose, compose
    # and the refusal's SHORT clause, which is for the screen rather than for the
    # model: a restored turn rebuilds its receipt out of this JSON alone, so
    # without `why` a chip that said the reason while the page was open goes back
    # to saying "no image" and nothing else the moment the session is reopened
    assert "if (s.why) wire.why = s.why;" in compose, compose
    # the overview rides the same block, first, under its own kind
    assert 'kind: "overview"' in compose and "overview.view" in compose


def test_a_failed_send_hands_the_picture_back_instead_of_dropping_it(html):
    """The crops and the pane shot part company here, and the reason is who took
    them. A crop is remade by the retry, so its thumbnail is revoked with the
    receipt. A pane shot is a moment the user chose — one they may not be able to
    photograph again — so it goes back to the composer as a chip, with the very
    thumbnail the receipt was showing, and must NOT be revoked on the way."""
    rollback = _between(html, "    if (!started) {", "\n    }")
    assert "shotRevoke(overview);" in rollback, "the overview is still released"
    assert "shotAttached = pics.concat(shotAttached);" in rollback
    assert "shotRevoke(pic" not in rollback, rollback


def test_a_second_click_replaces_the_picture_and_releases_the_first(html):
    """One shot at a time: "the pane, now" has one answer, and two pictures of the
    same screen is a receipt nobody reads. The first one's blob URL is the only
    handle to a full-pane PNG, so replacing it without revoking pins that Blob for
    the life of the page."""
    click = _between(html, "async function shotAttachPane()", "\n}\n")
    assert "shotRevoke(shotAttached[seat]);" in click
    assert click.index("shotRevoke(shotAttached[seat]);") \
        < click.index("shotAttached[seat] = shot;"), \
        "release the old picture before the binding to it is overwritten"
    # ONE pane seat, found by kind — a pasted picture is the other kind and stacks
    assert 'const seat = shotAttached.findIndex((s) => s.kind === "pane");' in click
    # and a double-click cannot start a second capture at all
    assert "if (shotBusy || !annCapable()) return;" in click
    assert "shotBusy = true;" in click


def test_an_abandoned_pane_capture_leaks_neither_a_rejection_nor_a_blob(html):
    """Same two hazards the crops' race had, and they are hazards of the RACE
    rather than of the capture: once the timeout wins, a late rejection has no
    handler and a late thumbnail has no owner."""
    click = _between(html, "async function shotAttachPane()", "\n}\n")
    assert "SHOT_TIMEOUT_MS" in click, "the capture is bounded"
    handlers = click[click.index("capture.then("):]
    assert "if (abandoned) shotRevoke(late)" in handlers
    assert "console.warn" in handlers
    # and the button always comes back, whichever way the race went
    assert click.count("b.disabled = false") == 1
    assert click.index("b.disabled = false") > click.index("Promise.race")


def test_a_failed_capture_still_becomes_a_chip_the_user_can_read(html):
    """Degrading to "no image" is right; degrading to "no evidence anything was
    asked for" is the silent-failure shape. The user pressed a button and is owed
    an answer, and the agent is owed the news that a picture was meant to be here —
    so a failure is a chip that says so, removable with the same ✕."""
    click = _between(html, "async function shotAttachPane()", "\n}\n")
    assert "shotAttached.push(shot);" in click
    assert "renderAnn();" in click
    chip = _between(html, "function shotChip(shot)", "\n}\n")
    assert 'shot.view\n    ? shotNoun(shot)' in chip
    # ONE label for the refusal, shared with the receipt (shotFailLabel) — the two
    # used to spell it differently ("image failed" against "no image"), which made
    # a receipt look like a different event from the chip it replaced
    assert ": shotFailLabel(shot);" in chip
    label = _between(html, "function shotFailLabel(shot)", "\n}\n")
    for words in ('"no image"', '"no file"', '"no overview screenshot"',
                  '"no pane screenshot"'):
        assert words in label, words
    # the title carries the FULL sentence when there is no path to carry
    assert "shot.viewNote || shot.view" in chip
    # and a pasted picture that could not be saved is the SAME shape: a chip with
    # its own reason, never a file that vanished without an answer
    attach = _between(html, "async function shotAttachFile(file)", "\n}\n")
    assert 'shotAttached.push({ kind, view: null, name, viewNote: note, why: why });' \
        in attach
    assert "renderAnn()" in attach


def test_the_chip_shows_the_picture_and_takes_it_off_again(html):
    """A screenshot the user cannot look at before it goes out is one they cannot
    check, and one they cannot remove is one they cannot decline. Both live in the
    chip, which is the annotation chip's own pill — same row, same ✕ — because both
    are things this message is about to carry."""
    chip = _between(html, "function shotChip(shot)", "\n}\n")
    assert 'chip.className = "annchip shotchip"' in chip, \
        "the same pill, plus the entrance animation's hook"
    assert 'shotThumbBtn("shotthumb", shot, shotAlt(shot))' in chip
    assert 'x.setAttribute("aria-label", "Remove " + shotNoun(shot))' in chip
    assert "x.onclick = () => shotDrop(shot)" in chip
    # dropping releases THAT blob, leaves the other pictures alone, and does NOT
    # write the annotations param
    drop = _between(html, "function shotDrop(shot)", "\n}\n")
    assert "shotRevoke(shot);" in drop
    assert "shotAttached = shotAttached.filter((s) => s !== shot);" in drop
    assert "renderAnn();" in drop
    assert "annSave" not in drop, "no annotation was touched"
    # and they are drawn into BOTH composers' chip rows, from renderAnn's one loop
    render = _between(html, "function renderAnn()", "\n}\n")
    assert "for (const shot of shotAttached) box.appendChild(shotChip(shot));" in render


def test_the_picture_is_not_persisted_anywhere(html):
    """Notes survive a reload because they are the user's words. This is a temp
    file and a blob URL — a restored page would hold dead handles to both, and a
    bookmark that re-attaches yesterday's picture of a screen that has since
    changed is worse than one that attaches nothing."""
    for writer in ("function shotAttachPane", "function shotDrop", "function shotChip",
                   "async function shotAttachFile"):
        body = _between(html, writer, "\n}\n")
        assert "fused.params" not in body, writer
    assert "let shotAttached = [];" in html


def test_the_button_is_absent_wherever_there_is_nothing_to_photograph(html):
    """It asks the annotate switch's question, `annCapable()`, and not `noPane`:
    hosted in the sidebar there is no pane of our own AND there is a target, and
    the answer moves as the host switches what it shows. Both halves are here —
    the poll hides it, and the no-pane target removes it outright."""
    poll = _between(html, "function annPollTarget()", "\n}\n")
    assert "for (const b of shotBtns()) b.hidden = !has;" in poll
    click = _between(html, "async function shotAttachPane()", "\n}\n")
    assert "annCapable()" in click, "and a keyboard click cannot outrun the poll"
    nopane = _between(html, "function enterNoPane()", "\n}\n")
    assert '"anntool", "viewshot", "leftmode"' in nopane
    # hidden means hidden: the strip button's own display would otherwise outrank [hidden]
    assert "#viewshot[hidden] { display: none; }" in html


def test_the_strip_carries_the_one_button_between_comment_and_record(html):
    """ONE button, in the #anncta strip (Akshil, 2026-08-27): it used to be a
    camera pill beside Send in both composers; it acts on the PREVIEW like Comment
    and Record, so it sits with them — first of the three: Screenshot, Comment, Record.
    The strip is the one row the home card and the chat both keep, so one copy
    serves both composers, and the chip still lands above whichever is showing.
    Hidden while a mode is armed, like the mic."""
    assert 'id="hviewshot"' not in html
    assert 'class="pill viewshot"' not in html
    strip = _between(html, '<div id="anncta">', "\n      </div>")
    assert 'id="viewshot"' in strip
    # Screenshot · Comment · Annotate (Akshil, 2026-09-04)
    assert strip.index('id="viewshot"') < strip.index('id="annbtn"') < strip.index('id="annrec"')
    assert "#anncta:has(#annbtn.on) #viewshot { display: none; }" in html
    btns = _between(html, "function shotBtns()", "\n}\n")
    assert '"viewshot"' in btns and "hviewshot" not in btns
    # the spoken strings are the pane-noun writer's, not two hardcoded literals
    noun = _between(html, "function applyPaneNoun()", "\n}\n")
    assert "for (const b of shotBtns())" in noun
    assert 'const say = "Screenshot the " + paneNoun + " and attach it to this message"' \
        in noun
    # ONE sentence for both, so the tooltip and the screen reader cannot drift
    assert "b.title = say" in noun and 'b.setAttribute("aria-label", say)' in noun


# --------------------------------------------- the second pass: seeing the picture
#
# The button shipped and the report was three sentences long: it was not obvious
# that a screenshot had been taken, there was no way to look at one before sending
# it, and a sent one vanished into a four-word marker. All three are the same
# failure — a picture the user could never actually SEE — and these pin the three
# answers: a flash over the photographed pane, a viewer behind every thumbnail,
# and a receipt that survives a reload.


def test_the_click_flashes_the_pane_it_photographed(html):
    """The capture ran in total silence: no motion anywhere on screen between the
    click and a 22px chip appearing in another corner, which reads as a button
    that did nothing. The flash goes over the PANE, because the pane is the
    subject — a control that lights up says only "I was clicked"."""
    flash = _between(html, "function shotFlash()", "\n}\n")
    # the framed viewport in BOTH layouts, found through the binding that already
    # tracks it rather than through a second layout test
    assert "annHl && annHl.parentNode" in flash
    assert "host.ownerDocument.createElement" in flash, \
        "minted in the TARGET's document, which in the sidebar is not ours"
    # inline style + WAAPI: a stylesheet of ours in a document of theirs is the
    # hazard the annotation layer's own comment already names
    assert "el.setAttribute(\"style\"" in flash
    assert "el.animate(" in flash
    assert "opacity" in flash
    # opacity only — the reduced-motion-safe form, so the signal survives the
    # preference instead of being switched off with the movement
    assert "translate" not in flash and "scale" not in flash
    # and it always cleans up, whichever way the animation ends
    assert "anim.onfinish = done" in flash and "anim.oncancel = done" in flash
    assert "if (!el.animate) { done(); return null; }" in flash, \
        "no animation support is a missed flash, never a failed capture"


def test_the_flash_fires_before_the_capture_not_after(html):
    """Immediately, because a capture can take a second on a large page and the
    click has to be answered now. It is safe there: the layer lives behind a
    shadow root, and cloneNode does not clone one, so shotPane cannot photograph
    the flash even when the timing overlaps."""
    click = _between(html, "async function shotAttachPane()", "\n}\n")
    assert "shotFlash();" in click
    assert click.index("shotFlash();") < click.index("shotCapturePane(")


def test_the_chip_animates_in_and_the_animation_is_only_a_hook(html):
    """The second half of the same answer, for the eye that has already left the
    pane: the chip has to still be moving when the user looks at the composer.
    `shotchip` carries the entrance and nothing else — every other rule it wears
    is `.annchip`'s, because it IS one."""
    assert "@keyframes shotchip-in" in html
    assert ".annchip.shotchip { animation: shotchip-in" in html
    # honoured, and reduced to nothing rather than left to jump
    reduced = _between(html, "@media (prefers-reduced-motion: reduce) {\n    .annchip.shotchip",
                       "}")
    assert "animation: none" in reduced


def test_every_thumbnail_is_a_button_that_opens_the_viewer(html):
    """"No way to preview it before sending" — the user was asked to trust a 22px
    smudge and press send. One builder for every place a shot is shown small, so a
    picture that opens while drafting also opens after sending; a control that
    works in one of those and not the other is what makes it feel arbitrary."""
    btn = _between(html, "function shotThumbBtn(", "\n}\n")
    assert 'btn.type = "button"' in btn, \
        "a real button: tabbable, Enter/Space, announced as pressable"
    assert "btn.onclick = () => shotViewOpen(shot);" in btn
    assert "shot.src || shot.thumb" in btn, \
        "src for a restored turn, thumb for this session — one builder, two sources"
    # both callers go through it
    assert 'shotThumbBtn("shotthumb"' in html      # the pending chip
    assert 'shotThumbBtn("annsum-pane"' in html    # a sent turn's receipt
    # and the small ones say they can be opened
    assert "cursor: zoom-in" in html


def test_the_viewer_shows_the_path_and_the_caveat_the_wire_carried(html):
    """`viewNote` — what the picture does NOT show — has ridden the wire since the
    first version of this feature and had nowhere on screen to be said. The viewer
    is where it belongs: the one moment the user is looking at the pixels it is
    about."""
    open_ = _between(html, "function shotViewOpen(shot)", "\n}\n")
    assert 'shotViewImg.src = pic || "";' in open_
    assert "const pic = shot && (shot.src || shot.thumb);" in open_
    assert "shotViewPath.textContent = shot.view" in open_
    assert "shotViewNote.textContent = shot.viewNote" in open_
    assert "shotView.hidden = false;" in open_
    # focus moves to the way OUT, which is the first thing a keyboard reaches for
    assert 'document.getElementById("shotview-close").focus()' in open_


def test_discard_is_offered_only_while_the_picture_is_still_the_users(html):
    """Identity, not a copy: Discard appears only when the shot on screen is the
    very object `paneShot` holds. A sent picture is already in the agent's hands,
    and a control offering to take it back would be a lie."""
    open_ = _between(html, "function shotViewOpen(shot)", "\n}\n")
    assert "shotViewDrop.hidden = shotAttached.indexOf(shot) === -1;" in open_
    # and discarding from in there closes the viewer, since what it showed is gone
    assert "shotViewDrop.onclick = () => { shotDrop(shotViewing); shotViewClose(); };" \
        in html


def test_the_viewer_closes_every_way_a_modal_should(html):
    """Scrim, button, Escape. A modal with one exit is a trap, and the scrim is
    the one people try first."""
    assert 'document.getElementById("shotview-close").onclick = shotViewClose;' in html
    assert 'document.getElementById("shotview-scrim").onclick = shotViewClose;' in html
    esc = _between(html, "function onEscape(e)", "\n}\n")
    assert "escapeAction(!shotView.hidden," in esc
    assert 'if (act === "close-viewer") shotViewClose();' in esc


def test_a_closed_viewer_is_really_closed(html):
    """FOUND IN THE BROWSER, and it is the third time this template has hit the
    same trap: an element with its own `display` rule beats the UA's
    `[hidden] { display: none }` on specificity. #annbtn and .viewshot both spell
    their hiding out; this one shipped without it, so the "hidden" viewer was a
    full-bleed transparent-scrim sheet over the entire pane, swallowing every
    click — invisible in review, because a scrim over the app looks like the app.

    `hidden` stays the ONE answer to "is the viewer open" (escapeAction and
    shotViewOpen/Close all read it, and there is no second flag to disagree); this
    is only the rule that makes the attribute mean what it says."""
    assert '<div id="shotview" hidden>' in html
    style = _between(html, "  #shotview {", "\n  }")
    assert "display: flex" in style, "it centres the picture, hence the collision"
    assert "#shotview[hidden] { display: none; }" in html, \
        "the rule above outranks the UA's, so the hiding has to be spelled out"
    # one source of truth for open/closed: the attribute, never a class
    assert "shotView.hidden" in html
    assert "#shotview.open" not in html


def test_a_sent_turn_keeps_its_picture(html):
    """"It vanished into the wire marker." A shot-only send collapsed to four
    words and the picture was gone from the transcript. The receipt now carries
    the image itself, at a size worth scrolling back to, and it opens the same
    viewer."""
    receipt = _between(html, "function shotReceipt(sum, shot)", "\n}\n")
    assert '"screenshot attached"' in receipt
    assert ": shotFailLabel(shot);" in receipt, "and the refusal, in shared words"
    # a pasted picture names itself instead — the receipt has to describe what
    # actually went, and "screenshot attached" for a file the user brought in is a
    # receipt for the wrong thing
    assert '"image attached"' in receipt and "shot.name" in receipt
    assert 'shotThumbBtn("annsum-pane", shot' in receipt
    # the live send goes through the one builder rather than inlining a second row
    send = _between(html, "async function sendMessage(message)", "\n}\n")
    assert "for (const pic of pics) shotReceipt(sum, pic);" in send


def test_a_restored_turn_renders_its_picture_from_the_path_in_the_wire(html):
    """The path was in the transcript the whole time and the server serves local
    files, so a reopened session showing no picture was a choice nobody made
    deliberately. `paneShotIn` reads the block back, `fused.rawUrl` turns the path
    into a URL — the same way every other template shows a local image."""
    restore = _between(html, "function shotRestoreReceipt(turn, text)", "\n}\n")
    assert "paneShotIn(text)" in restore
    assert "fused.rawUrl(shot.view)" in restore
    assert "shotReceipt(sum, " in restore, "the same row as a live send, not a copy"
    assert "for (const shot of shots)" in restore, \
        "every picture the turn carried, not just the first"
    # wired into the restore loop, on the turn addUser just appended. The end
    # marker is that loop's `else`, and it is a BLOCK since 2026-08-21 — the
    # assistant arm grew the stopped-turn note beside its render — so it is
    # matched as an opening brace rather than as the old one-line
    # `} else addAssistantTurn`.
    load = _between(html, "      if (t.role === \"user\") {", "      } else {")
    assert "addUser(stripBlocks(t.text), t.uuid);" in load
    assert "shotRestoreReceipt(turns[turns.length - 1], t.text);" in load
    # a pruned temp file says so instead of showing a broken-image glyph
    receipt = _between(html, "function shotReceipt(sum, shot)", "\n}\n")
    assert "onerror" in receipt and "no longer on disk" in receipt


def test_the_wire_reader_is_the_exact_counterpart_of_the_writer(html):
    """Round trip, through the real writer and the real reader: what
    `paneShotBlock` puts in is what `paneShotIn` gets out, and `stripBlocks` still
    sees none of it."""
    out = _wire(html, """
const view = {kind: "pane", view: "/tmp/fr/shots/S-view.png",
              viewNote: "part is blank"};
const pasted = {kind: "image", view: "/tmp/fr/shots/S.png", name: "bug.png"};
const wire = composeOutgoing("fix this", [], {entry: "/p"}, [view, pasted]);
console.log(JSON.stringify({back: paneShotIn(wire), stripped: stripBlocks(wire),
                            none: paneShotIn("just words"),
                            broken: paneShotIn("<pane-shot>\\nnot json\\n</pane-shot>"),
                            legacy: paneShotIn("<pane-shot>\\ncap\\n"
                              + JSON.stringify({view: "/tmp/old.png"}) + "\\n</pane-shot>"),
                            junk: paneShotIn("<pane-shot>\\n[1,2]\\n</pane-shot>")}));
""")
    assert out["back"] == [{"kind": "pane", "view": "/tmp/fr/shots/S-view.png",
                            "viewNote": "part is blank"},
                           {"kind": "image", "view": "/tmp/fr/shots/S.png",
                            "name": "bug.png"}]
    assert out["stripped"] == "fix this", "the path still never reaches the reader"
    assert out["none"] == []
    # THE PERMANENT OBLIGATION: a session written before the list existed carries a
    # bare object, and it comes back as a one-element list so no consumer has to
    # branch on the age of the transcript it is drawing
    assert out["legacy"] == [{"view": "/tmp/old.png"}]
    # forgiving, both ways: a transcript that renders without one picture is a
    # small loss, one that throws mid-restore takes the conversation with it
    assert out["broken"] == []
    assert out["junk"] == [], "entries that are not objects are dropped, not drawn"


def test_the_button_left_the_composer_for_the_strip_and_names_its_verb(html):
    """It sat beside Send until 2026-08-27 (Akshil): now it is a seat in #anncta,
    and neither composer row carries a copy — the strip is the one row both keep.
    The glyph is still a camera — the verb — rather than a framed landscape, which
    says "insert an image", and the spoken sentence still leads with the verb."""
    for row in _iter_composer_rows(html):
        assert "id=\"viewshot\"" not in row and "class=\"viewshot\"" not in row, \
            "the button is in the strip, not beside Send"
    # a camera: a body with a lens, not a rectangle with a mountain in it — ONE copy
    assert html.count('<circle cx="8" cy="9" r="2.4"') == 1
    assert '<path d="M2.9 11.4 6 8.3' not in html, "the old photo-frame glyph is gone"
    # one sentence, leading with the verb, in both spoken slots of the one button
    assert html.count(
        'aria-label="Screenshot the preview and attach it to this message"') == 1
    assert html.count(
        'title="Screenshot the preview and attach it to this message"') == 1
    assert "(for layout problems that are not about one element)" not in html, \
        "the explanatory parenthesis is a manual, not a label"


def _iter_composer_rows(html):
    rows = []
    start = 0
    while True:
        i = html.find('<div class="composer-row">', start)
        if i == -1:
            break
        j = html.index("</div>", html.index('class="send"', i))
        rows.append(html[i:j])
        start = j
    assert len(rows) == 2, "one composer row per composer, home and chat"
    return rows


def test_the_viewer_can_show_a_wide_screenshot_at_a_legible_scale(html):
    """FOUND IN THE BROWSER, and it is a constraint of where this template lives:
    `position: fixed` is the TEMPLATE's viewport, which in the sidebar layout is a
    ~440px column the viewer cannot escape (no postMessage — D3/D4). Measured, a
    1600px capture fitted to that lands at 310px: four times the chip, and still
    not enough to read the UI it is a picture OF.

    So the fitted view is the overview and one click on the picture swaps to
    natural size with the box scrolling, which is the only way a narrow column
    shows a wide screenshot at a scale worth opening it for."""
    assert "#shotview-box.zoom { overflow: auto; }" in html
    zoom = _between(html, "  #shotview-box.zoom #shotview-img {", "\n  }")
    assert "max-width: none" in zoom and "max-height: none" in zoom
    # Measured in a browser: lifting the caps alone changed NOTHING. The box is a
    # column flex container, so `align-items: stretch` sized the image to the
    # box's width on the cross axis and the "zoomed" picture rendered identically
    # to the fitted one, under a zoom-out cursor promising otherwise.
    assert "align-self: flex-start" in zoom, \
        "without this the flex cross-axis stretch pins the width and nothing zooms"
    # the cursor is what advertises which click you are about to make
    assert "cursor: zoom-in" in _between(html, "  #shotview-img {", "\n  }")
    assert "cursor: zoom-out" in zoom
    # the way out must not scroll away with a 1600px image
    sticky = _between(html, "  #shotview-box.zoom #shotview-bar {", "\n  }")
    assert "position: sticky" in sticky
    # the picture is the toggle, not a fourth button in the bar
    assert "shotViewImg.onclick = () => {" in html
    assert 'shotViewBox.classList.toggle("zoom")' in html
    # ...and every open starts fitted, because a zoom belongs to one picture
    open_ = _between(html, "function shotViewOpen(shot)", "\n}\n")
    assert 'shotViewBox.classList.remove("zoom")' in open_
    assert "shotViewBox.scrollTop = 0" in open_


def test_the_scheduler_writes_a_block_this_page_can_read(html):
    """D619. THE CROSS-LANGUAGE HALF: `schedule._attachments_block` writes the
    `<pane-shot>` block for a headless task run, and it may not import this
    page (PY-15) — so the shape is hand-duplicated and this is what pins it.
    Python writes, node reads, and the assertions are on what the RECEIPT ROW
    then has to draw.

    The bug it closes, from Akshil's own screenshot: the scheduler used to
    append a plain-text tail, so a fired task's chat showed the user's turn
    ending in a list of `/Users/…/task-shots/…pdf` paths while the same chat
    rendered its own attachments as rows.
    """
    from fused_render import schedule

    block = schedule._attachments_block({"attachments": [
        {"path": "/h/.fused-render/task-shots/a1.pdf",
         "name": "Q3 report.pdf", "kind": "file"},
        {"path": "/h/.fused-render/task-shots/b2.png",
         "name": "chart.png", "kind": "image"}]})
    wire = block + "\n\nsummarise the report"
    out = _node(_WIRE_FNS + ["const BLOCK_OPENERS", "function sessionTitle("],
                "console.log(JSON.stringify({"
                "  back: paneShotIn(WIRE), stripped: stripBlocks(WIRE),"
                "  title: sessionTitle({preview: WIRE.slice(0, 80), id: \"s1\"}),"
                "  wholeTitle: sessionTitle({preview: WIRE, id: \"s2\"})}));",
                html, prelude="const WIRE = " + json.dumps(wire) + ";")
    # READ BACK, in order, with every field the receipt row asks for.
    assert out["back"] == [
        {"kind": "file", "view": "/h/.fused-render/task-shots/a1.pdf",
         "name": "Q3 report.pdf", "viewNote": ""},
        {"kind": "image", "view": "/h/.fused-render/task-shots/b2.png",
         "name": "chart.png", "viewNote": ""}]
    # …and the path NEVER reaches the bubble, which is the whole complaint.
    assert out["stripped"] == "summarise the report"
    assert "task-shots" not in out["stripped"]
    # The chat LIST is the other place the block used to leak. A preview is
    # truncated to 80 chars, so the closing tag is not in the string at all —
    # `BLOCK_OPENERS` is what catches that, and the row is named for what the
    # turn carried instead of for our prose.
    # …and it is a MARKER, not our prose and not a path. Which marker is the
    # chat's own pre-existing answer for a cut preview — `paneShotIn` has no
    # payload to read in 80 chars, so it falls to "pane screenshot" exactly as
    # a truncated chat send does (see the `cutPane` case below). Worth pinning as
    # a marker rather than as that word: the value here is that the row is not
    # titled "<pane-shot> The user attached 2 att…".
    assert out["title"] in ("\U0001f5bc pane screenshot", "\U0001f4c4 files",
                            "\U0001f5bc images"), out["title"]
    assert "pane-shot" not in out["title"]
    assert "The user attached" not in out["title"]
    assert out["wholeTitle"] == "summarise the report"
    # AND THE MARKER IS THE FILE ONE, not the pane's: nothing here is a picture
    # of this app, so `stripBlocks`' three-way choice has to land on what the
    # user brought in.
    only_pics = schedule._attachments_block({"attachments": [
        {"path": "/h/.fused-render/task-shots/b2.png", "name": "chart.png",
         "kind": "image"}]})
    marks = _wire(html, "console.log(JSON.stringify({pics: stripBlocks("
                  + json.dumps(only_pics) + ")}));")
    assert marks["pics"] == "\U0001f5bc images"


def test_a_session_row_is_named_with_what_the_user_typed(html):
    """SEEN IN THE RUNNING APP, in Akshil's own sidebar: the chat list read
    "<pane-shot> The user attached a pi…" and "The user annotated 1 element in the
    l…". The stored preview is the head of the first user message, and a message
    carrying a screenshot, a snapshot or notes BEGINS with a machine-written block
    — so the page's own wire, addressed to the model, was quoted back at the human
    as the name of their conversation. A screenshot that is "visible after send"
    and turns the chat's title into XML is not fixed.

    stripBlocks cannot do this alone and the reason is worth pinning: the preview
    is TRUNCATED, so the closing tag every strip matches on is usually not in the
    string at all."""
    # sessionTitle leans on the whole strip stack plus the two markers, so it runs
    # with the same wire bindings every other strip test uses, plus its own two.
    out = _node(_WIRE_FNS + ["const BLOCK_OPENERS", "function sessionTitle("], """
const cases = {
  plain:     sessionTitle({preview: "fix the header", id: "s1"}),
  // truncated mid-block: no closing tag anywhere, which is the real shape
  cutPane:   sessionTitle({preview: "<pane-shot>\\nThe user attached a pi", id: "s2"}),
  cutAnn:    sessionTitle({preview: "The user annotated 1 element in the l", id: "s3"}),
  cutState:  sessionTitle({preview: "<live-app-state>\\n{\\"entry\\": \\"/p/ind", id: "s4"}),
  // a WHOLE block plus the words after it: stripBlocks handles this half
  whole:     sessionTitle({preview: "<pane-shot>\\ncap\\n{}\\n</pane-shot>\\n\\nwhy is this wrong",
                           id: "s5"}),
  // nothing but a block: name it what the bubble would say, never blank
  empty:     sessionTitle({preview: "", id: "s6"}),
  both:      sessionTitle({preview: "<pane-shot>\\nThe user attached a pi", id: "s7"}),
};
console.log(JSON.stringify(cases));
""", html)
    assert out["plain"] == "fix the header", "an ordinary message is untouched"
    assert out["cutPane"] == "\U0001f5bc pane screenshot"
    assert out["cutAnn"] == "\U0001f4cc annotations"
    assert out["cutState"] == "s4", "a snapshot alone names no turn — fall to the id"
    assert out["whole"] == "why is this wrong"
    assert out["empty"] == "s6", "never blank: a row with no title cannot be picked"
    assert out["both"] == "\U0001f5bc pane screenshot"
    # both consumers read the SAME string, or a heading and its row disagree
    assert 'row.querySelector(".row-title").textContent = sessionTitle(s);' in html
    assert "const title = sessionTitle(s);" in html
    assert "snapNames.set(s.id, title);" in html
    assert "snapNames.set(s.id, s.preview)" not in html, "the raw preview is gone"


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


def test_a_scrolled_box_is_put_back_by_shifting_its_children(html):
    """The fix has to be expressible in MARKUP, because markup is all the
    serialized SVG carries — so the scroll becomes a transform on the children
    rather than an offset on the parent. The parent keeps the `overflow` its
    computed style already gave it, which is what does the clipping."""
    out = _node(["function shotApplyScroll("], _SCROLL_STUBS + """
const a = el("display:block;"), b = el("display:block;");
const box = el("overflow:auto;");
box.children = [a, b];
const shifted = shotApplyScroll([{clone: box, x: 40, y: 3160}]);
console.log(JSON.stringify({shifted: shifted, a: styleOf(a), b: styleOf(b),
                            box: styleOf(box)}));
""", html)
    assert out["shifted"] == 2
    assert out["a"].endswith("transform:translate(-40px,-3160px);")
    assert out["b"].endswith("transform:translate(-40px,-3160px);")
    assert "transform" not in out["box"], \
        "the scroll box itself must not move — it is the window onto the content"


def test_a_child_that_does_not_scroll_with_the_page_is_left_where_it_is(html):
    """A sticky header does not move with the scroll — that is the whole point of
    it — and a fixed element is not in the scroll box's coordinate space at all.
    Shifting either would put it somewhere it has never been on screen."""
    out = _node(["function shotApplyScroll("], _SCROLL_STUBS + """
const stuck = el("position:sticky;top:0px;");
const fixed = el("position: fixed; inset: 0;");
const flow = el("position:static;");
const box = el("overflow:scroll;");
box.children = [stuck, fixed, flow];
const shifted = shotApplyScroll([{clone: box, x: 0, y: 500}]);
console.log(JSON.stringify({shifted: shifted, stuck: styleOf(stuck),
                            fixed: styleOf(fixed), flow: styleOf(flow)}));
""", html)
    assert out["shifted"] == 1
    assert "translate" not in out["stuck"] and "translate" not in out["fixed"]
    assert out["flow"].endswith("transform:translate(0px,-500px);")


def test_the_scroll_shift_composes_with_the_elements_own_transform(html):
    """The style walk has already written the computed `transform` — a matrix, for
    anything the app transformed itself — and replacing it would flatten a rotated
    or scaled element while fixing its scroll. Ours goes FIRST, because leftmost is
    outermost: the shift is applied in the scroll box's space, after the element's
    own transform, which is what scrolling does."""
    out = _node(["function shotApplyScroll("], _SCROLL_STUBS + """
const spun = el("transform:matrix(0, 1, -1, 0, 0, 0);");
const none = el("transform:none;");
const box = el("overflow:auto;");
box.children = [spun, none];
shotApplyScroll([{clone: box, x: 0, y: 100}]);
console.log(JSON.stringify({spun: styleOf(spun), none: styleOf(none)}));
""", html)
    assert out["spun"].endswith(
        "transform:translate(0px,-100px) matrix(0, 1, -1, 0, 0, 0);")
    # "none" is not a transform to preserve — appending it would be a no-op that
    # cancels the shift
    assert out["none"].endswith("transform:translate(0px,-100px);")


def test_the_scroll_offsets_are_collected_by_the_walk_that_already_pairs_safely(html):
    """Every scrolled element has to be matched to its clone, which is exactly the
    problem the style walk's observer/reshaped machinery already solves. A second
    walk would either repeat all of it or pair by index and land one element's
    scroll offset on another."""
    walk = _between(html, "async function shotInlineStyles(src, dst, deadline)", "\n}\n")
    assert "scrolled.push({ clone: d, x: s.scrollLeft || 0, y: s.scrollTop || 0 });" in walk
    # read on the same node, right after its style, inside the guarded descent
    assert walk.index("d.setAttribute(\"style\", css);") < walk.index("scrolled.push(")
    assert walk.index("scrolled.push(") < walk.index("const a = s.children")
    # and NOT for the root: `src` is the app's <body>, whose scroll offset IS the
    # window's, and shotPane already shifts the whole clone by that
    assert "if (s !== src && (s.scrollTop || s.scrollLeft))" in walk
    assert "return { styled, incomplete, scrolled };" in walk


def test_the_capture_puts_the_scroll_back_after_everything_that_moves_nodes(html):
    """Order is load-bearing three times over, and each pair had a way to go
    wrong: the style walk must finish before its `scrolled` clones are shifted (the
    children it shifts are still being styled); images must be inlined BEFORE
    shotRasterise puts data:-URL <img>s of its own into the clone, or the two
    would pair by index and put a chart's pixels where a logo was; and the scroll
    must be applied LAST, so a canvas swapped for an <img> is shifted with the rest
    of its box rather than left at the top of it."""
    pane = _between(html, "async function shotPane(deadline)", "\n}\n")
    walk = pane.index("await shotInlineStyles(")
    imgs = pane.index("await shotInlineImages(")
    raster = pane.index("shotRasterise(body, clone)")
    scroll = pane.index("shotApplyScroll(styles.scrolled)")
    assert walk < imgs < raster < scroll, pane
    # the walk stops SHORT of the deadline so the fetches have a tail to run in
    assert "Math.max(Date.now(), deadline - SHOT_IMG_MS)" in pane
    assert "shotInlineImages(body, clone, deadline)" in pane
    # the window's own scroll is still the other half
    assert "win.scrollX" in pane and "win.scrollY" in pane


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


def _imgs(html, body):
    return _node(_IMG_FNS, _IMG_STUBS + body, html)


def test_every_image_is_fetched_and_written_into_the_markup_as_data(html):
    """THE FIX. An SVG rasterised through an <img> cannot load a URL — no network,
    at any origin — so the one src an image in the capture can follow is a `data:`
    one. Everything else in the clone was already inline; the pictures were the
    hole."""
    out = _imgs(html, """
const src = mkEl("body"), dst = mkEl("body");
const [s1, d1] = pair("http://127.0.0.1:8877/api/fs/raw?path=/a/hero.gif");
const [s2, d2] = pair("https://example.com/logo.png");
src.append(s1); src.append(s2); dst.append(d1); dst.append(d2);
(async () => {
  const r = await shotInlineImages(src, dst, Date.now() + 5000);
  console.log(JSON.stringify({missing: r.missing, fetched: FETCHED,
                              d1: d1.getAttribute("src"), d2: d2.getAttribute("src")}));
})();
""")
    assert out["missing"] == 0
    assert out["fetched"] == ["http://127.0.0.1:8877/api/fs/raw?path=/a/hero.gif",
                              "https://example.com/logo.png"]
    assert out["d1"].startswith("data:image/png;base64,")
    assert out["d2"].startswith("data:image/png;base64,")


def test_an_image_that_genuinely_cannot_be_fetched_says_so_in_the_picture(html):
    """A placeholder ONLY when the fetch really failed, and a placeholder rather
    than nothing: a broken-image glyph reads as a bug in the page being
    photographed, and removing the element would silently redraw the layout around
    a hole the user's screen did not have. The alt text goes in because it is the
    page's own description of what is missing."""
    out = _imgs(html, """
FAIL["https://offline.example/x.png"] = true;
const src = mkEl("body"), dst = mkEl("body");
const [s, d] = pair("https://offline.example/x.png");
s.alt = "the deploy graph";
d.attrs.style = "width:400px;height:200px;";
src.append(s); dst.append(d);
(async () => {
  const r = await shotInlineImages(src, dst, Date.now() + 5000);
  const box = dst.children[0];
  console.log(JSON.stringify({missing: r.missing, tag: box.tagName,
                              text: box.textContent, style: box.getAttribute("style")}));
})();
""")
    assert out["missing"] == 1
    assert out["tag"] == "DIV", "the <img> is gone: it could only draw a broken glyph"
    assert out["text"] == "image not captured — the deploy graph"
    # it keeps the box the picture had, so nothing around it moves
    assert out["style"].startswith("width:400px;height:200px;")
    assert "dashed" in out["style"]


def test_a_data_url_image_is_left_alone_and_costs_no_fetch(html):
    """It is already local — that is the whole property being aimed for."""
    out = _imgs(html, """
const src = mkEl("body"), dst = mkEl("body");
const [s, d] = pair("data:image/png;base64,AAAA");
src.append(s); dst.append(d);
(async () => {
  const r = await shotInlineImages(src, dst, Date.now() + 5000);
  console.log(JSON.stringify({missing: r.missing, fetched: FETCHED,
                              d: d.getAttribute("src")}));
})();
""")
    assert out == {"missing": 0, "fetched": [], "d": "data:image/png;base64,AAAA"}


def test_the_same_url_ten_times_is_one_fetch(html):
    """The cap is on DISTINCT urls, because that is what the cost is: an icon
    repeated down a list is one request and one base64 string."""
    out = _imgs(html, """
const src = mkEl("body"), dst = mkEl("body");
for (let i = 0; i < 10; i++) {
  const [s, d] = pair("http://h/icon.svg");
  src.append(s); dst.append(d);
}
(async () => {
  const r = await shotInlineImages(src, dst, Date.now() + 5000);
  console.log(JSON.stringify({missing: r.missing, fetches: FETCHED.length,
                              last: dst.children[9].getAttribute("src")}));
})();
""")
    assert out["fetches"] == 1
    assert out["missing"] == 0
    assert out["last"].startswith("data:")


def test_past_the_budget_the_rest_become_placeholders_rather_than_a_slow_capture(html):
    """The capture is racing a timer the user is watching. Over the URL cap, or
    past the deadline, the remaining pictures are marked missing — which the note
    then reports — rather than held onto while the whole shot times out."""
    over = _imgs(html, """
const src = mkEl("body"), dst = mkEl("body");
for (let i = 0; i < SHOT_IMG_MAX + 3; i++) {
  const [s, d] = pair("http://h/" + i + ".png");
  src.append(s); dst.append(d);
}
(async () => {
  const r = await shotInlineImages(src, dst, Date.now() + 5000);
  console.log(JSON.stringify({missing: r.missing, fetches: FETCHED.length}));
})();
""")
    assert over["fetches"] == 30, "the cap, exactly"
    assert over["missing"] == 3
    late = _imgs(html, """
const src = mkEl("body"), dst = mkEl("body");
const [s, d] = pair("http://h/late.png");
src.append(s); dst.append(d);
(async () => {
  const r = await shotInlineImages(src, dst, Date.now() - 1);
  console.log(JSON.stringify({missing: r.missing, fetches: FETCHED.length}));
})();
""")
    assert late == {"missing": 1, "fetches": 0}, "a passed deadline fetches nothing"


def test_a_picture_elements_sources_are_removed_before_its_src_is_rewritten(html):
    """A `<source>` still pointing at an http URL would have the browser re-resolve
    the child over the `src` just written, and lose the image again inside the very
    element that was fixed."""
    out = _imgs(html, """
const src = mkEl("body"), dst = mkEl("body");
const sPic = mkEl("picture"), dPic = mkEl("picture");
const sSrc = mkEl("source"), dSrc = mkEl("source");
sSrc.attrs.srcset = "http://h/big.png"; dSrc.attrs.srcset = "http://h/big.png";
const [s, d] = pair("http://h/small.png");
d.attrs.srcset = "http://h/small.png 1x"; d.attrs.sizes = "50vw";
sPic.append(sSrc); sPic.append(s); dPic.append(dSrc); dPic.append(d);
src.append(sPic); dst.append(dPic);
(async () => {
  await shotInlineImages(src, dst, Date.now() + 5000);
  console.log(JSON.stringify({kids: dPic.children.map((c) => c.tagName),
                              src: d.getAttribute("src"),
                              srcset: d.getAttribute("srcset"),
                              sizes: d.getAttribute("sizes")}));
})();
""")
    assert out["kids"] == ["IMG"], "the <source> is gone"
    assert out["src"].startswith("data:")
    # both of these would re-select a URL over the src just written
    assert out["srcset"] is None and out["sizes"] is None


def test_background_images_are_inlined_out_of_the_styles_the_walk_wrote(html):
    """Same rule, same reason, and the style walk has already put the computed
    value on the clone — so this reads it back off the clone rather than making a
    second pass over the live tree. A background that cannot be fetched keeps its
    colour and its size and is counted: a dashed box in place of a texture would be
    a bigger lie than leaving it plain."""
    out = _imgs(html, """
FAIL["http://h/tile.png"] = true;
const src = mkEl("body"), dst = mkEl("body");
const ok = mkEl("div"), bad = mkEl("div"), plain = mkEl("div");
ok.attrs.style = "background-image:url(\\"http://h/hero.jpg\\");color:red;";
bad.attrs.style = "background-image:url(http://h/tile.png);";
plain.attrs.style = "background-image:none;color:blue;";
dst.append(ok); dst.append(bad); dst.append(plain);
(async () => {
  const r = await shotInlineImages(src, dst, Date.now() + 5000);
  console.log(JSON.stringify({missing: r.missing, ok: ok.getAttribute("style"),
                              bad: bad.getAttribute("style"),
                              plain: plain.getAttribute("style")}));
})();
""")
    assert out["missing"] == 1
    assert 'url("data:image/png;base64,http://h/hero.jpg")' in out["ok"]
    assert out["ok"].endswith("color:red;"), "the rest of the declaration is untouched"
    assert out["bad"] == "background-image:url(http://h/tile.png);"
    assert out["plain"] == "background-image:none;color:blue;"


def test_the_two_local_url_shapes_are_never_fetched(html):
    """`data:` is already inline, and `url(#id)` is an SVG fragment reference — a
    filter or a clip path in the same document, which travels with the clone. A
    fetch of either would spend the budget on nothing."""
    out = _node(["function shotStyleUrls("], """
console.log(JSON.stringify({
  found: shotStyleUrls("background-image:url('http://h/a.png');" +
                       "mask-image:url(#clip);border-image-source:url(data:image/png,x);" +
                       "cursor:url(\\"http://h/c.cur\\"), auto;"),
  none: shotStyleUrls("color:red;background-image:none;"),
}));
""", html)
    assert out["found"] == ["http://h/a.png", "http://h/c.cur"]
    assert out["none"] == []


def test_a_capture_missing_a_picture_says_so_without_condemning_the_rest(html):
    """The caveat is BOUNDED and visible — every missing picture is a dashed box
    saying so in the image itself — which is why it is deliberately not one of
    `shotPaneNote`'s causes: those are "some of this may not be what you think it
    is", spread over an unknown set of elements. Folding one missing logo into
    `incomplete` would tell the agent to distrust the whole crop."""
    out = _node(["function shotImageNote(", "function shotPaneNote(",
                 "function shotTrustLine("], """
console.log(JSON.stringify({
  none: shotImageNote({imagesMissing: 0}),
  one: shotImageNote({imagesMissing: 1}),
  many: shotImageNote({imagesMissing: 4}),
  // an otherwise clean capture is still trusted
  paneNote: shotPaneNote({imagesMissing: 3, incomplete: ""}),
  trust: shotTrustLine(""),
}));
""", html)
    assert out["none"] == ""
    assert out["one"].startswith("1 image could not be embedded")
    assert out["many"].startswith("4 images could not be embedded")
    for note in (out["one"], out["many"]):
        assert "image not captured" in note
        assert "The app is very likely showing the image fine" in note
    assert out["paneNote"] == "", "a missing picture is not a styling failure"
    assert out["trust"] == "The rest of the image is what the user saw."


def test_the_image_caveat_rides_the_pane_shot_and_the_overview_alike(html):
    """The overview is cut from the same kind of bitmap, so a picture the capture
    could not embed is as true of it as it is of the pane shot."""
    out = _view(html, """
PANE.imagesMissing = 2;
(async () => {
  const r = await shotCapturePane(Date.now() + 5000);
  console.log(JSON.stringify({viewNote: r.viewNote}));
})();
""")
    assert "2 images could not be embedded" in out["viewNote"]
    assert out["viewNote"].endswith("The rest of the image is what the user saw.")
    over = _capture(html, """
PANE.imagesMissing = 1;
const a = {id: "a", label: "A", el: {name: "a", contains: () => false}};
annotations = [a];
(async () => {
  const r = await annCaptureOverview([a]);
  console.log(JSON.stringify({note: r.viewNote}));
})();
""")
    assert "1 image could not be embedded" in over["note"]


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

def test_a_pasted_blob_is_named_for_what_it_HOLDS_not_for_what_it_was_called(html):
    """The same rule `shotExt` applies to an encoded crop, and for the same reason:
    the agent Reads that path, and a name its bytes do not match is a file it
    cannot open. A clipboard image arrives with no filename at all, so the MIME
    type is the only evidence there is."""
    out = _node(["function shotFileExt(", "function shotIsImage("], """
console.log(JSON.stringify({
  clip: shotFileExt("image/png", ""),
  jpeg: shotFileExt("image/jpeg", "photo.jpeg"),
  webp: shotFileExt("image/webp", "x"),
  // an unknown type falls back to the name, then to png
  odd: shotFileExt("application/octet-stream", "screen.HEIC"),
  nothing: shotFileExt("", ""),
  images: [{type: "image/png"}, {type: "", name: "a.JPG"}].map(shotIsImage),
  not: [{type: "text/plain", name: "notes.txt"}, {type: "", name: "x.pdf"}, null]
        .map(shotIsImage),
}));
""", html)
    assert out["clip"] == ".png"
    assert out["jpeg"] == ".jpg"
    assert out["webp"] == ".webp"
    assert out["odd"] == ".heic"
    assert out["nothing"] == ".png"
    assert out["images"] == [True, True]
    assert out["not"] == [False, False, False]


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


def _attach(html, body):
    return _node(_ATTACH_FNS, _ATTACH_STUBS + body, html)


def test_a_pasted_picture_lands_in_the_shots_directory_and_becomes_a_chip(html):
    """The shots directory and nowhere else: it is the one path the spawn line
    pre-approves a `Read` of, it is pruned on the same schedule, and /api/fs/raw
    already serves it back for a restored turn. A second directory would have meant
    a second grant, a second pruner and a second reader.

    AND THE GATE IS GONE. `shotIsImage` used to decide whether the attachment
    happened at all, so a chat that could be handed a screenshot OF a CSV could not
    be handed the CSV — the thing users kept reporting. It now decides only which
    cap applies, whether a thumbnail is made and which word the wire uses: a
    non-picture rides the identical pipeline as `kind: "file"`, keeps its own
    extension (never the `.png` fallback that is right for a nameless blob), and
    carries its `size`, which is the one thing a doc chip can say that a name
    cannot.

    And none of this asks the server: the transcode is the undecodable branch's
    answer and nothing else's. An ordinary PNG has pixels here, so a round trip
    for it would buy a slower chip and a second copy on disk."""
    out = _attach(html, """
CONVERT = (p) => ({path: "/tmp/fr/shots/conv.png", width: 8, height: 8,
                   bytes: 10, source_w: 8, source_h: 8});
(async () => {
  await shotAttachFiles([file("bug.png", "image/png", 0, [800, 600]),
                         file("notes.txt", "text/plain", 4096),
                         file("shot.jpg", "image/jpeg", 0, [200, 200]),
                         file("Makefile", "", 300)]);
  console.log(JSON.stringify({uploaded: uploaded, attached: shotAttached,
                              renders: renders, pythons: pythons}));
})();
""")
    assert out["pythons"] == [], "no bridge call for pixels this engine has"
    assert len(out["uploaded"]) == 4, "every type rides now"
    assert out["uploaded"][0].startswith("/tmp/fr/shots/")
    assert out["uploaded"][0].endswith(".png")
    assert out["uploaded"][1].endswith(".txt"), "a file keeps its own extension"
    assert out["uploaded"][2].endswith(".jpg")
    assert not os.path.splitext(out["uploaded"][3])[1], \
        "and keeps NONE when it has none — a Makefile is not a .png"
    assert [s["kind"] for s in out["attached"]] == ["image", "file", "image", "file"]
    assert [s["name"] for s in out["attached"]] == ["bug.png", "notes.txt",
                                                    "shot.jpg", "Makefile"]
    assert out["attached"][0]["view"] == out["uploaded"][0]
    assert out["attached"][0]["thumb"].startswith("blob:")
    # a picture carries a thumbnail and no size; a file, the reverse
    assert "size" not in out["attached"][0]
    assert out["attached"][1]["size"] == 4096
    assert "thumb" not in out["attached"][1], "there is no 22px view of a .txt"
    # a chip per attachment, as it lands — that IS the feedback
    assert out["renders"] == 4


def test_an_attachment_that_cannot_be_saved_becomes_a_chip_that_says_why(html):
    """Nothing the user gestured at may disappear without an answer: a file they
    dropped and cannot see anywhere is indistinguishable from a bug. The refusal
    wears the same shape a failed capture does — `view: null` plus the reason —
    so the chip, the viewer and the wire all already know how to carry it.

    THERE IS EXACTLY ONE SIZE-INDEPENDENT REFUSAL LEFT, and no size-dependent one
    at all (D615). An oversize picture is downscaled (below); the 25 MB ceiling
    that used to catch everything else — a non-picture, and a picture no engine
    here can decode — is GONE, because it protected nothing: the shots directory
    prunes itself, `Read` truncates at the agent's end, and `/api/fs/upload`
    enforces no limit of its own. So a 50 MB undecodable TIFF now attaches, and
    only an upload that actually failed still refuses. `why` rides with it, a
    clause short enough to sit on the chip itself: "🖼 no image" with the cause
    only in a `title` was reported as a picture that vanished for no stated
    reason. Since D617 the per-MESSAGE count cap is gone too, so a dropped folder
    arrives whole — see the no-count-cap test below."""
    out = _attach(html, """
(async () => {
  // 50 MB, and undecodable: twice what the old file cap allowed, and there is no
  // cap left to turn it away
  await shotAttachFiles([file("scan.tiff", "image/tiff", 50 * 1024 * 1024)]);
  const huge = shotAttached[0];
  shotAttached = [];
  // the one refusal left: the save itself did not happen
  const ok = fused.uploadFile;
  fused.uploadFile = async () => { throw new Error("disk full"); };
  await shotAttachFiles([file("rows.csv", "text/csv", 4096)]);
  const bad = shotAttached[0];
  fused.uploadFile = ok;
  shotAttached = [];
  // a folder's worth, dropped in one gesture — every one of them attaches
  const many = [];
  for (let i = 0; i < 30; i++) {
    many.push(file("a" + i + ".png", "image/png", 0, [40, 40]));
  }
  await shotAttachFiles(many);
  console.log(JSON.stringify({huge: huge, bad: bad,
                              count: shotAttached.length,
                              whys: shotAttached.filter((s) => s.why).length,
                              uploads: uploaded.length}));
})();
""")
    assert out["huge"]["view"], "50 MB is not a refusal any more"
    assert out["huge"]["view"].endswith(".tiff"), "its own bytes, its own extension"
    assert out["huge"]["size"] == 50 * 1024 * 1024
    assert "why" not in out["huge"], "nothing was refused, so nothing to explain"
    assert out["bad"]["view"] is None
    assert out["bad"]["name"] == "rows.csv"
    assert "it could not be saved (disk full)" in out["bad"]["viewNote"]
    # the clause the chip and the receipt print ON THE ROW — the whole complaint
    # was an unexplained refusal
    assert out["bad"]["why"] == "could not be saved"
    # a chip per file and NOTHING left out (D617): the overflow chip that used to
    # stand in for the rest of the folder has nothing left to stand in for
    assert out["count"] == 30, out["count"]
    assert out["whys"] == 0, "nothing was refused, so no row explains itself"
    assert out["uploads"] == 31, "the tiff and all thirty of the folder"


def test_no_byte_cap_can_refuse_an_attachment_at_all(html):
    """D615, asserted as an ABSENCE, because that is the shape of the decision: the
    number and the branch that read it are both gone, not merely raised. A cap
    left in place at a larger value would be the same bug one drop bigger.

    `SHOT_ATTACH_MAX_BYTES` stays, and stays at 4 MB, but it is now a DOWNSCALE
    TRIGGER (D613) and nothing else — `shotShrink` is its only reader."""
    assert "const SHOT_ATTACH_MAX_FILE_BYTES" not in html, \
        "the 25 MB non-image cap is gone, not renamed"
    assert "function shotCapFor" not in html, \
        "and so is the function that chose between the two"
    assert "shotCapFor(" not in html, "with no reader left anywhere"
    attach = _between(html, "async function shotAttachFile(file)", "\n}\n")
    assert "const SHOT_ATTACH_MAX_BYTES = 4 * 1024 * 1024;" in html
    # the one place the number is still read, and what it does there
    assert "file.size > SHOT_ATTACH_MAX_BYTES" in attach
    assert "shotShrink(pix, SHOT_ATTACH_MAX_BYTES)" in attach
    # and no refusal anywhere in this function names a limit
    assert "the limit is" not in attach
    assert "blob.size >" not in attach
    # the only CALL to `fail` left is the upload's catch
    assert attach.count("fail(") == 1, attach.count("fail(")
    assert "could not be saved" in attach


def test_no_count_cap_can_refuse_an_attachment_either(html):
    """D617, and asserted as an ABSENCE for the same reason D615's is: the number
    and the branch that read it are gone, not raised. A cap at 8 would be the same
    bug one drop bigger.

    What it cost was the one gesture it was written for — dropping a folder. This
    is a local tool reading local files off the same disk the agent already has a
    Read of, so "how many can ride" has no answer to enforce: every file the user
    brought attaches, in order, each with its own chip."""
    assert "SHOT_ATTACH_MAX =" not in html, "the count cap is gone, not renamed"
    assert "-attachment cap" not in html, "and so is every chip that named it"
    assert "which is the cap" not in html
    for fn in ("async function shotAttachFiles(files)",
               "function shotAttachPaths(paths)"):
        body = _between(html, fn, "\n}\n")
        assert "room" not in body, fn + " still rations attachments"
        assert ".slice(" not in body, fn + " still drops the tail of a drop"
        assert "more " not in body, fn + " still writes an overflow chip"
    # and the PATH shape carries a whole multi-selection too
    out = _node(_PATH_FNS, """
var renders = 0;
var fused = {rawUrl: (p) => p};
function renderAnn() { renders++; }
const paths = [];
for (let i = 0; i < 30; i++) paths.push("/home/a/proj/row" + i + ".csv");
shotAttachPaths(paths);
console.log(JSON.stringify({count: shotAttached.length,
                            views: shotAttached.map((s) => s.view),
                            renders: renders}));
""", html)
    assert out["count"] == 30, "thirty dragged rows are thirty attachments"
    assert out["views"] == ["/home/a/proj/row%d.csv" % i for i in range(30)]
    assert out["renders"] == 1, "one repaint for the whole drop"


def test_an_oversize_picture_is_downscaled_instead_of_refused(html):
    """The bug as reported: a dropped PNG became "🖼 no image" and the composer
    gave no visible reason. The cap it hit was never about the disk — it is about
    the AGENT's read of the pixels — and a picture too big for it is one downscale
    away from being exactly as useful, which makes refusing it the wrong answer to
    a question we can simply solve.

    The ladder is `shotEncode`'s and NOT a second copy: same webp quality steps,
    same silent-PNG guard, same halve-and-retry, capped at SHOT_VIEW_EDGE — which
    is what a capture of the whole pane is capped at, so a photo the user brought
    in is never carried at a size this app's own screenshots are not."""
    out = _attach(html, """
(async () => {
  // 4200x2800 at 9 MB: over the picture cap, and readable pixels
  await shotAttachFiles([file("photo.jpg", "image/jpeg",
                              9 * 1024 * 1024, [4200, 2800])]);
  console.log(JSON.stringify({shot: shotAttached[0], path: uploaded[0],
                              blob: wrote[0], encodes: encodes, closed: closed,
                              edge: SHOT_VIEW_EDGE, cap: SHOT_ATTACH_MAX_BYTES}));
})();
""")
    shot = out["shot"]
    assert shot["view"], "attached, not refused"
    assert shot["kind"] == "image"
    assert shot["name"] == "photo.jpg", "the name is the ORIGINAL's, not the copy's"
    # fitted to the same longest edge a whole-pane shot is
    assert out["encodes"] == [{"type": "image/webp", "q": 0.8,
                               "w": 1600, "h": 1067}], out["encodes"]
    assert out["blob"]["size"] <= out["cap"]
    # the chip and the wire both say what was done to the pixels, in the sentence
    # the viewer shows: an agent reading a resized copy is owed that fact
    assert shot["viewNote"] == ("downscaled from 4200×2800 to 1600×1067 for the "
                               "agent's read (9.0 MB → 208 KB)"), shot["viewNote"]
    # THE BYTES THAT WENT UP are the re-encoded ones, and the path is named for
    # them: a `.jpg` holding webp bytes is the exact lie shotExt exists to prevent
    assert out["blob"]["type"] == "image/webp"
    assert out["path"].endswith(".webp"), out["path"]
    # a thumbnail of the copy that was actually sent, and no `size` beside it —
    # they are alternatives, which is what makes "a picture OR a glyph" decidable
    assert shot["thumb"].startswith("blob:")
    assert "size" not in shot
    assert out["closed"] == 1, "the decoded bitmap is released, not leaked"


def test_an_image_the_browser_cannot_decode_travels_with_no_thumbnail(html):
    """The other half of the reported screenshot: a receipt showing BOTH the 🖼
    glyph and a thumbnail. For a .tiff it was worse than redundant —
    `URL.createObjectURL` on one renders an empty box in Chrome (no error, no
    glyph, nothing), so the row showed a drawing of a photo next to a hole.

    So decodability is probed for EVERY image, and it decides two things: whether
    the downscale is available at all (there is no resize for bytes nothing here
    can read, so those travel whole — and since D615 there is no second cap left
    to catch them either) and whether a thumbnail would be a picture at all. It
    still travels, and still as an `image` — the agent may well know a format this
    browser does not.

    Both ways the server can decline are here — a `{error}` answer and a bridge
    that REJECTS — because `shotServerPng` swallows everything: the only thing
    riding on that call is how GOOD the attachment is, never whether there is one.
    A picture that disappeared because the converter was unhappy would be a worse
    bug than the one this fixes."""
    out = _attach(html, """
(async () => {
  // 6 MB: over the picture threshold, unshrinkable, and answering to no cap
  await shotAttachFiles([file("scan.tiff", "image/tiff", 6 * 1024 * 1024)]);
  const shot = shotAttached[0], path = uploaded[0], calls = pythons.slice();
  // and the same file with the bridge itself down
  shotAttached = [];
  CONVERT = (p) => { throw new Error("the bridge is down"); };
  await shotAttachFiles([file("scan.tif", "image/tiff", 1024)]);
  console.log(JSON.stringify({shot: shot, path: path, encodes: encodes,
                              pythons: calls,
                              thrown: shotAttached[0], thrownPath: uploaded[1]}));
})();
""")
    thrown = out["thrown"]
    assert thrown["view"] == out["thrownPath"], "the raw copy, exactly as before"
    assert "thumb" not in thrown and thrown["size"] == 1024
    assert "could not convert it either" in thrown["viewNote"]
    assert "the agent probably cannot read this format" in thrown["viewNote"]
    shot = out["shot"]
    assert shot["view"], "attached at 6 MB: unshrinkable, and nothing caps it"
    assert shot["kind"] == "image", "the agent may know a format this browser lacks"
    assert out["path"].endswith(".tiff"), "its own extension, verbatim"
    assert out["encodes"] == [], "nothing to re-encode: there were no pixels"
    # no thumbnail, and therefore a size — the pair the chip and the receipt read
    # to decide between a picture and a glyph
    assert "thumb" not in shot
    assert shot["size"] == 6 * 1024 * 1024
    assert "cannot decode" in shot["viewNote"]
    # D614: the SERVER was asked first (this harness answers with an error), and
    # only because it also declined does the note stand. What it says changed:
    # D613 offered the hope that "the agent may know a format this browser does
    # not", which for .tif and .heic is false.
    assert [p["params"]["action"] for p in out["pythons"]] == ["image_to_png"]
    assert out["pythons"][0]["params"]["path"] == out["path"]
    assert "the agent probably cannot" in shot["viewNote"]


def test_a_picture_no_engine_here_can_decode_is_transcoded_by_the_server(html):
    """The bug D613 left behind, reported with a screenshot of `RGB.byte.tif` and
    `IMG_4031.HEIC`: both attached, both wearing the 🖼 glyph, and both useless —
    because the agent's `Read` opens neither format either, so the attachment was
    a path to bytes nobody in the conversation could look at.

    So the page asks the SERVER, which has Pillow and (on a Mac) the OS's own
    decoders, for a PNG of the file it has just uploaded. On success the
    attachment becomes an ordinary picture: `view` is the converted path (that is
    what the agent must Read), the thumbnail is that file through /api/fs/raw
    rather than a blob: URL — a URL that survives the reload the blob one does
    not — and it carries NO `size`, for the same reason no other drawable picture
    does. Which is also what keeps the restore path's `bare` test honest: an image
    with a `size` is one with no pixels anyone can draw."""
    out = _attach(html, """
CONVERT = (p) => ({path: "/tmp/fr/shots/conv.png", width: 1600, height: 1200,
                   bytes: 900000, source_w: 4032, source_h: 3024});
(async () => {
  await shotAttachFiles([file("IMG_4031.HEIC", "image/heic", 2 * 1024 * 1024)]);
  console.log(JSON.stringify({shot: shotAttached[0], uploaded: uploaded,
                              pythons: pythons}));
})();
""")
    shot = out["shot"]
    # the ORIGINAL still went up: that is the file the conversion reads, and it is
    # what the user actually attached
    assert out["uploaded"][0].endswith(".heic")
    assert out["pythons"][0]["params"] == {"action": "image_to_png",
                                          "path": out["uploaded"][0]}
    # and the CONVERTED copy is what travels
    assert shot["view"] == "/tmp/fr/shots/conv.png"
    assert shot["thumb"] == "/api/fs/raw?path=/tmp/fr/shots/conv.png"
    assert "size" not in shot, "a drawable picture carries a thumbnail, not a size"
    assert shot["kind"] == "image"
    assert shot["name"] == "IMG_4031.HEIC", "still called what the user called it"
    assert shot["viewNote"] == (
        "converted from HEIC to PNG on the server (4032×3024 → 1600×1200) because "
        "neither the browser nor the agent can read that format"), shot["viewNote"]


def test_the_format_names_in_that_sentence_are_the_ones_a_person_says(html):
    """The note has to be checkable against the chip beside it: the user dropped
    `IMG_4031.HEIC`, so it says HEIC. `.jpg` and `.tif` are folded onto their long
    names because that is what made them."""
    out = _node(["function shotFormatLabel("], """
console.log(JSON.stringify(["a.HEIC", "b.heif", "c.tif", "d.tiff", "e.jpg",
                            "f.jpeg", "/tmp/x/g.png", "Makefile", ""]
                           .map(shotFormatLabel)));
""", html)
    assert out == ["HEIC", "HEIF", "TIFF", "TIFF", "JPEG", "JPEG", "PNG",
                   "that format", "that format"], out


def test_a_refusal_names_its_cause_on_the_row_in_both_places(html):
    """ONE label for the chip and the receipt, and the reason inline rather than in
    a `title` nobody hovers. Kept to a single clause on purpose: the row is a
    receipt beside a composer, and a wrapped sentence there pushes the text box
    down for something the viewer already says in full."""
    out = _node(["function shotFailLabel("], """
console.log(JSON.stringify([
  {kind: "image", why: "could not be saved"},
  {kind: "file", why: "could not be saved (disk full)"},
  {kind: "image"}, {kind: "overview"}, {kind: "pane"},
].map(shotFailLabel)));
""", html)
    assert out == ["no image — could not be saved",
                   "no file — could not be saved (disk full)",
                   "no image", "no overview screenshot", "no pane screenshot"], out
    for fn in ("function shotChip(shot)", "function shotReceipt(sum, shot)"):
        assert ": shotFailLabel(shot);" in _between(html, fn, "\n}\n"), fn


def test_a_thumbnail_replaces_the_glyph_rather_than_joining_it(html):
    """Reported off a screenshot of a sent turn: the image receipt showed the 🖼
    glyph AND the thumbnail. The picture IS the icon, so a drawing of a photo in
    front of a photo is a label for something already visible — and beside an
    undecodable format's empty box it was actively misleading.

    Both surfaces branch, and neither keeps a placeholder: `.annchip` and
    `.annsum-row` are flex rows aligned to one edge, so a row without a glyph
    slides its words against that same edge rather than moving."""
    chip = _between(html, "function shotChip(shot)", "\n}\n")
    assert "const pic = !!shot.thumb;" in chip
    assert "if (pic) {" in chip
    assert chip.index('shotThumbBtn("shotthumb"') < chip.index("lbl.textContent"),         "the picture branch first, and the glyph only in the else"
    assert chip.count("chip.appendChild(lbl);") == 1
    receipt = _between(html, "function shotReceipt(sum, shot)", "\n}\n")
    # never for a file, whatever it carries: an <img> at a .csv is a broken-image
    # glyph, which reads as a bug in the chat
    assert 'const src = shot.kind === "file" ? "" : (shot.src || shot.thumb || "");' \
        in receipt
    assert receipt.index("if (!src) {") < receipt.index('lbl.className = "annsum-lbl"')
    assert "row.appendChild(txt);" in receipt and "row.append(lbl, txt)" not in receipt
    # the alignment the branch relies on, in the CSS it relies on it from
    row = _between(html, ".annsum .annsum-row {", "}")
    assert "justify-content: flex-end;" in row, row
    assert "display: flex;" in row


def test_a_paste_of_words_still_reaches_the_textarea(html):
    """The listener sits on a box the user types in all day. Stealing an ordinary
    paste would be a far worse bug than never having had the feature — so the
    default is only prevented once a picture has actually been found."""
    paste = _between(html, "function shotPasteHandler(e)", "\n}\n")
    assert "const picks = [...(data.files || [])];" in paste
    assert paste.index("if (!picks.length) return;") < paste.index("e.preventDefault();")
    # both composers, one handler
    assert 'box.addEventListener("paste", shotPasteHandler);' in html
    assert 'homebox.addEventListener("paste", shotPasteHandler);' in html


def test_a_drag_of_text_keeps_the_browsers_own_behaviour(html):
    """Dragging text out of the log and into the composer is the browser's job and
    was working before this existed. Every one of the four listeners asks the same
    question first, so a text drag is never touched."""
    drag = _between(html, "function shotDragHasAttachment(e)",
                    "else shotAttachFiles([...(e.dataTransfer.files || [])]);")
    assert 'types.indexOf("Files") !== -1 || types.indexOf(SHOT_PATH_TYPE) !== -1' in drag
    assert drag.count("if (!shotDragHasAttachment(e)) return;") == 4, \
        "dragenter, dragover, dragleave and drop"
    # dragover MUST preventDefault or the drop never fires, and dropEffect is what
    # makes the cursor promise a copy rather than show the forbidden sign
    over = drag[drag.index('addEventListener("dragover"'):drag.index('addEventListener("dragleave"')]
    assert "e.preventDefault();" in over
    assert 'e.dataTransfer.dropEffect = "copy";' in over


def test_the_drop_target_is_the_whole_column_and_its_highlight_is_counted(html):
    """A target the size of a one-line input is a target people miss, and
    everything in the column is part of the same message. dragenter/dragleave fire
    for every child element the pointer crosses, so a plain toggle flickers the
    highlight off the moment the cursor moves over a chip — hence a counter, reset
    (not decremented) on drop, which delivers no leave for the enters before it."""
    assert 'const chatCol = document.getElementById("chat");' in html
    assert "let shotDragDepth = 0;" in html
    assert "shotDragDepth = Math.max(0, shotDragDepth - 1);" in html
    assert "if (!shotDragDepth) chatCol.classList.remove(\"dropping\");" in html
    drop = html[html.index('chatCol.addEventListener("drop"'):]
    assert "shotDragDepth = 0;" in drop[:400]
    # an inset ring, because a border would resize the column under a pointer that
    # is holding something
    assert "#chat.dropping {" in html
    assert "box-shadow: inset 0 0 0 2px var(--accent);" in html


def test_a_pasted_picture_and_a_capture_ride_the_same_message_together(html):
    """One list, not two: everything downstream treats them identically, and the
    only thing `kind` decides is the words. The pane keeps its ONE seat inside that
    list — "the pane, now" still has exactly one answer — while pictures the user
    brought in have no such uniqueness and stack."""
    out = _attach(html, """
(async () => {
  await shotAttachFile(file("a.png", "image/png"));
  shotAttached.push({kind: "pane", view: "/tmp/fr/shots/1-view.webp"});
  await shotAttachFile(file("b.png", "image/png"));
  console.log(JSON.stringify({kinds: shotAttached.map((s) => s.kind)}));
})();
""")
    assert out["kinds"] == ["image", "pane", "image"]
    # and the composer's own guard counts the list rather than one binding —
    # the home composer, the chat composer, and its activeRun follow-up branch
    assert html.count(
        "if (!message && !annPending().length && !shotAttached.length) return;") == 3


def test_the_block_tells_the_model_which_picture_is_of_this_app(html):
    """The one thing the array made necessary. Two paths with no way to tell them
    apart would have the agent read a photo the user pasted in as a picture of the
    pane it is being asked about — and act on it."""
    out = _wire(html, """
const pane = {kind: "pane", view: "/tmp/fr/shots/S-view.png"};
const img = {kind: "image", view: "/tmp/fr/shots/S.png", name: "from-slack.png"};
console.log(JSON.stringify({block: composeOutgoing("", [], null, [pane, img]),
                            one: composeOutgoing("", [], null, [pane]),
                            none: composeOutgoing("hi", [], null, [])}));
""")
    block = out["block"]
    assert "The user attached 2 pictures" in block
    assert '"kind": "pane"' in block.replace('"kind":"pane"', '"kind": "pane"')
    assert "from-slack.png" in block
    # what each kind MEANS, said once, where the model reads it
    assert 'a picture of the WHOLE visible' in block
    assert 'so it is NOT a picture of this pane' in block
    assert "The user attached a picture" in out["one"], "singular, for one"
    assert out["none"] == "hi", "no pictures, no block"


# ============================================================================
# ANY FILE, and a PATH where there is one. Two halves of the same complaint: a
# chat that could be handed a screenshot OF a spreadsheet but not the
# spreadsheet, and a drag from the explorer beside it that could only ever
# deliver a COPY of a file the user already had. The first is a gate that came
# out; the second is a payload (`application/x-fused-path`) the chat now prefers
# over `files` whenever a drag carries it.
# ============================================================================

def test_a_file_has_no_size_cap_at_all(html):
    """The two caps became one THRESHOLD (D615), and the threshold refuses nothing.

    A picture's 4 MB was always about the agent's read of the PIXELS, so the
    number survives as the downscale trigger (the oversize-picture test above).
    A log, a CSV or a database dump has no such ceiling at all: the extra bytes
    ARE the content. The 25 MB that used to stand there protected nothing — the
    shots directory prunes itself, the agent's `Read` truncates a long file at
    its own end, and `/api/fs/upload` enforces no limit — so its only effect was
    to turn the one gesture carrying a 30 MB log into a chip saying no."""
    out = _attach(html, """
(async () => {
  // forty megabytes, well past the cap that used to be here
  await shotAttachFiles([file("dump.sql", "", 40 * 1024 * 1024)]);
  console.log(JSON.stringify({trigger: SHOT_ATTACH_MAX_BYTES,
                              huge: shotAttached[0], hugeBlob: wrote[0].size,
                              uploads: uploaded.length}));
})();
""")
    assert out["trigger"] == 4 * 1024 * 1024, "the one number left, and it downscales"
    # the row the old 25 MB cap turned away, now attached whole
    assert out["huge"]["view"], "forty megabytes of SQL is an attachment, not a refusal"
    assert out["huge"]["kind"] == "file"
    assert out["huge"]["size"] == 40 * 1024 * 1024
    assert out["hugeBlob"] == 40 * 1024 * 1024, "unread, unresized, uncapped: it is the content"
    assert "why" not in out["huge"], "nothing to explain: nothing was refused"
    assert out["uploads"] == 1


def test_the_block_names_the_kind_that_is_not_a_picture_at_all(html):
    """The wire's whole job here. An agent handed a path under a heading that says
    "the user attached a picture" will look at a .xlsx as if it were one — so the
    OPENER counts the kinds (a list of files is not announced as pictures, and a
    mixed one has no honest singular noun), and the per-kind paragraph says in
    words that a "file" is not a picture, that it is read as TEXT, and that a
    binary format will not parse rather than leaving the model to guess."""
    out = _wire(html, """
const pane = {kind: "pane", view: "/tmp/fr/shots/S-view.png"};
const doc = {kind: "file", view: "/tmp/fr/shots/S.csv", name: "rows.csv"};
const zip = {kind: "file", view: "/home/a/x.zip", name: "x.zip"};
console.log(JSON.stringify({files: composeOutgoing("", [], null, [doc, zip]),
                            one: composeOutgoing("", [], null, [doc]),
                            mixed: composeOutgoing("", [], null, [pane, doc])}));
""")
    assert "The user attached 2 files" in out["files"]
    assert "The user attached a file" in out["one"], "singular, for one"
    assert "The user attached 2 attachments" in out["mixed"], \
        "no honest singular noun covers a capture AND a spreadsheet"
    said = out["one"]
    assert '"kind": "file"' in said.replace('"kind":"file"', '"kind": "file"')
    assert "it is NOT a picture at all" in said
    assert "read it as text" in said
    assert "will not parse" in said and "xlsx" in said
    assert "`name` is what they called it" in said


def test_a_file_only_send_is_not_described_as_images_or_a_screenshot(html):
    """The bubble is the ONLY record a wordless send leaves, and there are three
    facts it can be reporting: a capture of this pane, pictures the user brought
    in, files they brought in. One marker each, all of them in MARKERS, so the
    re-attach predicate learns the new one at the same moment the strip can
    produce it (the drift that broke this once already)."""
    out = _wire(html, """
const doc = {kind: "file", view: "/tmp/fr/shots/S.csv", name: "rows.csv"};
const img = {kind: "image", view: "/tmp/fr/shots/S.png", name: "a.png"};
const pane = {kind: "pane", view: "/tmp/fr/shots/S-view.png"};
const strip = (list) => stripBlocks(composeOutgoing("", [], null, list));
console.log(JSON.stringify({file: strip([doc]), both: strip([doc, img]),
                            imgs: strip([img]), pane: strip([pane, doc]),
                            known: [strip([doc]), strip([doc, img])]
                              .map(isMarkerOnly)}));
""")
    assert out["file"] == "\U0001f4c4 files"
    assert out["both"] == "\U0001f4c4 files", "one file in the list decides it"
    assert out["imgs"] == "\U0001f5bc images", "unchanged where nothing is a file"
    assert out["pane"] == "\U0001f5bc pane screenshot", \
        "a capture of this pane in the list still wins the marker"
    assert out["known"] == [True, True], \
        "every marker a strip can produce is in MARKERS, or resumeRun mis-matches"


def test_a_drop_that_carries_a_real_path_is_attached_without_a_copy(html):
    """The one shape that skips the upload. A browser `File` carries bytes and
    never a filesystem path, so this is unreachable from a Finder drop and always
    will be — it is for a drag that ORIGINATES inside fused-render and can say
    where the file lives in a dataTransfer type of its own.

    Copying would work and is still the wrong answer for those: the file is one the
    user already has, at a path they can name, and the agent is about to be asked to
    Read it — likely to EDIT it next, which a copy in a directory pruned every 12
    hours cannot survive. So the real path goes on the wire, no bytes move, and
    `size` is absent because the page never opened the file."""
    out = _node(_PATH_FNS, """
var renders = 0;
var raws = [];
var fused = {rawUrl: (p) => { raws.push(p); return "/api/fs/raw?p=" + p; }};
function renderAnn() { renders++; }
const dt = (types, data) => ({types: types, getData: (t) => data[t] || ""});
const both = dt(["Files", "application/x-fused-path"],
                {"application/x-fused-path": "/home/a/proj/rows.csv\\n/home/a/pics/shot.png"});
const paths = shotDropPaths(both);
shotAttachPaths(paths);
console.log(JSON.stringify({
  type: SHOT_PATH_TYPE,
  paths: paths,
  // a drag with no path payload is not this shape at all
  none: shotDropPaths(dt(["Files"], {})),
  attached: shotAttached,
  raws: raws,
  renders: renders,
}));
""", html)
    assert out["type"] == "application/x-fused-path"
    assert out["paths"] == ["/home/a/proj/rows.csv", "/home/a/pics/shot.png"], \
        "newline-joined: a multi-row selection is ONE drag"
    assert out["none"] == []
    kinds = [a["kind"] for a in out["attached"]]
    assert kinds == ["file", "image"], "the extension decides which word"
    assert [a["view"] for a in out["attached"]] == out["paths"], \
        "the REAL path, never a copy"
    assert [a["name"] for a in out["attached"]] == ["rows.csv", "shot.png"]
    assert "size" not in out["attached"][0], \
        "the page never opened it, and 0 KB would read as an empty file"
    assert "thumb" not in out["attached"][0], "no 22px view of a .csv"
    assert out["attached"][1]["thumb"].startswith("/api/fs/raw"), \
        "a picture the server can serve still gets its thumbnail"
    assert out["raws"] == ["/home/a/pics/shot.png"], "one raw URL, for the picture"
    assert out["renders"] == 1, "one repaint for the whole drop"


def test_a_real_path_attachment_asks_for_its_own_directory_and_nothing_else(html):
    """What `read_dirs` carries on the send. The spawn line already allows a Read
    of the shots directory unconditionally, so a copied attachment contributes
    NOTHING — filtering it out here is what stops the `--allowed-tools` line
    growing a duplicate rule every turn. Only a real path does, one rule per
    directory, deduplicated, because dropping six rows out of one folder is one
    grant."""
    out = _node(_PATH_FNS, """
var renders = 0;
var fused = {rawUrl: (p) => p};
function renderAnn() { renders++; }
shotDirSeen = "/tmp/fr/shots";
console.log(JSON.stringify({
  // a copy in the shots dir, two real paths out of one folder, one out of another
  dirs: shotReadDirs([
    {kind: "image", view: "/tmp/fr/shots/1.png"},
    {kind: "file", view: "/home/a/proj/rows.csv"},
    {kind: "file", view: "/home/a/proj/notes.md"},
    {kind: "image", view: "/home/a/pics/shot.png"},
    {kind: "image", view: null},
  ]),
  win: shotReadDirs([{kind: "file", view: "C:\\\\Users\\\\a\\\\rows.csv"}]),
  none: shotReadDirs([]),
}));
""", html)
    assert out["dirs"] == ["/home/a/proj", "/home/a/pics"], \
        "the shots dir is already granted; a failed attachment has no path at all"
    assert out["win"] == ["C:/Users/a"], \
        "forward slashes, the one spelling the Read rule is matched as"
    assert out["none"] == []
    # and the send actually sends it, per turn, because the spawn is per turn
    assert "read_dirs: JSON.stringify(shotReadDirs(pics))," in html


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


def test_a_file_chip_says_what_a_thumbnail_cannot_and_still_opens(html):
    """A file has no pixels, so the two things the picture chip gets for free have
    to be replaced: the thumbnail's job of BEING the door into the viewer goes to
    the glyph (a real <button>, tabbable and announced, not a span with an
    onclick), and the picture's job of answering "which one did I just attach"
    goes to the name and the size, which is the only thing a doc chip can add."""
    chip = _between(html, "function shotChip(shot)", "\n}\n")
    assert 'const isFile = shot.kind === "file";' in chip
    assert 'lbl.textContent = isFile ? "\U0001f4c4" : "\U0001f5bc";' in chip
    assert "lbl.onclick = () => shotViewOpen(shot);" in chip
    # the door exists only where there is something behind it: a REFUSED file's
    # glyph is the same plain span a refused picture gets, and a picture-less
    # IMAGE (one this engine cannot decode) is a door for the same reason a file is
    assert 'const isDoor = !pic && !!shot.view && (isFile || shot.kind === "image");' \
        in chip
    assert 'lbl.className = isDoor ? "pinlbl shotdoc" : "pinlbl";' in chip
    # keyed off the PICTURE and not off the kind, so an undecodable image gets the
    # size that is then the only thing telling it apart from another one
    assert 'const size = pic ? "" : shotSizeLabel(shot.size);' in chip
    # and the viewer serves both kinds out of one dialog: no <img> for a file,
    # name and size where the picture would have been
    open_ = _between(html, "function shotViewOpen(shot)", "\n}\n")
    assert "if (!shot || !(pic || bare)) return;" in open_
    assert 'const bare = !!shot && !pic' in open_
    assert "shotViewImg.hidden = !pic;" in open_
    assert "shotViewName.textContent = bare" in open_
    assert '#shotview-name:empty { display: none; }' in html
    # the size label itself: `undefined` is a real answer (a real-path attachment
    # was never opened), and it must not print as "0 KB"
    out = _node(["function shotSizeLabel("], """
console.log(JSON.stringify([300, 4096, 8 * 1024 * 1024, undefined, -1]
  .map(shotSizeLabel)));
""", html)
    assert out == ["300 B", "4 KB", "8.0 MB", "", ""]


def test_a_restored_file_receipt_says_so_when_its_copy_has_been_pruned(html):
    """A picture's pruned copy announces itself: the <img> 404s and `onerror`
    replaces the thumbnail with words. A file has no <img>, so nothing would ever
    notice, and the row would go on claiming a path that is gone. `probe` is set
    ONLY by the restore — a live send's file was written seconds ago, so a fresh
    turn costs no request at all — and it is never `src`, because an <img> pointed
    at a .csv is a broken-image glyph, which reads as a bug in the chat."""
    receipt = _between(html, "function shotReceipt(sum, shot)", "\n}\n")
    assert 'lbl.textContent = shot.kind === "file" ? "\U0001f4c4" : "\U0001f5bc";' in receipt
    assert '"file attached"' in receipt
    assert 'fetch(shot.probe, { method: "HEAD" })' in receipt
    assert 'gone.textContent = " \u2014 " + (shot.kind === "image" ? "image" : "file")' \
        in receipt, "and an undecodable IMAGE is pruned the same way, in its own word"
    restore = _between(html, "function shotRestoreReceipt(turn, text)", "\n}\n")
    assert "bare ? { ...shot, probe: fused.rawUrl(shot.view) }" in restore
    # what makes a restored image `bare`: shotAttachFile writes a thumbnail OR a
    # size and never both, so an image carrying a size is one the sending browser
    # could not decode — and this one cannot draw it either
    assert 'shot.kind === "image" && typeof shot.size === "number"' in restore


# ============================================================================
# THE ATTACHMENT'S OWN PREVIEW (D616). A name and a size answer "which file is
# this"; they do not answer "is this the right file". fused-render already owns a
# template for a .csv, a .md, a .parquet — so the viewer frames the attachment in
# it. Icon by default, preview on the CLICK: the chip and the receipt stay one
# glyph wide, because a running template is not a 22px ornament.
# ============================================================================

def test_a_file_gets_its_own_template_framed_inside_the_viewer(html):
    """The decision is the LEFT PANE's decision, taken by the left pane's own two
    functions about a different file: `/api/fs/stat`, drop the `conditional`
    entries (their verdict lives behind /api/fs/conditions and is deliberately not
    fetched — an unresolved gate reads as "not offered") and drop the chat mode
    itself, first one wins (`paneOfferable`), then `paneSrcFor` for the URL.

    Reused rather than re-derived: a per-extension table here would drift from the
    registry on the next rebinding and ignore a user's own override (§16), and a
    preview that works in the pane must not be subtly different in the viewer."""
    out = _node(["const PANE_SKIP_MODES", "const paneOfferable",
                 "function paneSrcFor(", "async function shotPreviewSrc("], """
(async () => {
  const out = {};
  // the shape stat actually returns: the chat's own mode, a gated entry, then two
  // ordinary ones — the first OFFERABLE entry is the preview
  STAT = {is_dir: false, remote: false, templates: [
    {mode: "claude", path: "/t/claude/template.html"},
    {mode: "table", path: "/t/table/template.html", conditional: true},
    {mode: "csv", path: "/t/csv/template.html"},
    {mode: "code", path: "/t/code/template.html"}]};
  out.csv = await shotPreviewSrc("/tmp/fr/shots/rows.csv");
  // the `_render` sentinel: the file renders itself
  STAT = {is_dir: false, remote: false, templates: [{mode: "_render", path: null}]};
  out.self = await shotPreviewSrc("/tmp/fr/shots/page.html");
  // a remote-backed file forwards the hint the shell's own iframe does
  STAT = {is_dir: false, remote: true, templates: [
    {mode: "csv", path: "/t/csv/template.html"}]};
  out.remote = await shotPreviewSrc("/tmp/fr/shots/rows.csv");
  // nothing offerable: the chat mode is the only binding this extension has
  STAT = {is_dir: false, templates: [{mode: "claude", path: "/t/c/template.html"}]};
  out.chatOnly = await shotPreviewSrc("/tmp/fr/shots/x.md");
  STAT = {is_dir: false, templates: []};
  out.none = await shotPreviewSrc("/tmp/fr/shots/x.bin");
  // the pruner got there first
  STAT = {error: "no such file"};
  out.gone = await shotPreviewSrc("/tmp/fr/shots/pruned.csv");
  out.empty = await shotPreviewSrc("");
  out.fetched = fetched;
  console.log(JSON.stringify(out));
})();
""", html, prelude="""
var STAT = null;
var fetched = [];
var fetch = async (url) => { fetched.push(url); return {json: async () => STAT}; };
var FILE = "/x/target.md";
var paneRemote = false;
""")
    # the pane's URL shape, verbatim, plus the two stamps the shell puts on a
    # display-only frame
    assert out["csv"] == ("/render?path=%2Ft%2Fcsv%2Ftemplate.html"
                          "&_file=%2Ftmp%2Ffr%2Fshots%2Frows.csv"
                          "&_preview=1&_nofocus=1"), out["csv"]
    assert out["self"] == ("/render?path=%2Ftmp%2Ffr%2Fshots%2Fpage.html"
                           "&_preview=1&_nofocus=1"), out["self"]
    assert "&_remote=1" in out["remote"], "the hint the pane's own frame forwards"
    # every "no preview" answer is null, not an error: the viewer then stays
    # exactly what it was before this existed
    assert out["chatOnly"] is None, "a pane framing the chat again is a mirror"
    assert out["none"] is None
    assert out["gone"] is None
    assert out["empty"] is None
    assert len(out["fetched"]) == 6, "one stat per open, and none for no path"
    assert out["fetched"][0] == ("/api/fs/stat?path="
                                 "%2Ftmp%2Ffr%2Fshots%2Frows.csv")
    # /render and never /embed, for the reason the pane comment gives: /embed
    # serves the React shell, which nests the target one iframe deeper
    src = _between(html, "function paneSrcFor(t, file, remote)", "\n}\n")
    assert "/render?path=" in src
    assert "/embed" not in src
    prev = _between(html, "async function shotPreviewSrc(path)", "\n}\n")
    assert "/embed" not in prev


def test_the_viewers_frame_is_sealed_the_way_the_shell_seals_a_thumbnail(html):
    """The shell has ONE description of a frame that is a display-only picture of
    a page (`THUMB_SEAL`), and a vanilla-JS template cannot import it — so it is
    mirrored, and this test is what keeps the copy honest. Read out of the TS
    rather than written down twice: a sandbox that drifts looser here than there
    is exactly the kind of divergence nobody notices."""
    seal = open(os.path.join("frontend", "src", "platform", "lib",
                             "frame-focus.ts"), encoding="utf-8").read()
    want = re.search(r'THUMB_SEAL = \{\s*sandbox: "([^"]*)"', seal)
    assert want, "THUMB_SEAL moved; the mirror below has to move with it"
    frame = _between(html, '<iframe id="shotview-frame"', "></iframe>")
    assert 'sandbox="' + want.group(1) + '"' in frame, frame
    assert 'allow=""' in frame, "no delegated permissions, as THUMB_SEAL says"
    # and the rest of a frame that is not a place the keyboard goes: the viewer is
    # modal and Escape has to keep belonging to it
    assert 'tabindex="-1"' in frame
    assert 'aria-hidden="true"' in frame
    assert 'title=""' in frame
    # mounted empty. `hidden` needs its own display rule here for the same reason
    # #shotview does — a flex/`display` rule of its own outranks the UA's
    assert "hidden" in frame
    assert "#shotview-frame[hidden] { display: none; }" in html
    # and it fills the same footprint the picture gets, in the same fixed viewport
    style = _between(html, "  #shotview-frame {", "\n  }")
    assert "min-height: 0" in style, "or it pushes Discard and Close off the box"
    assert "100vw" in style and "100vh" in style, \
        "the box is sized by its content, so the frame is sized by the viewport"


def test_the_preview_frame_is_unmounted_on_every_way_out_of_the_viewer(html):
    """The one thing an <img> does not need and a template does: a preview left
    mounted behind a hidden modal keeps RUNNING — a warm python worker, a poll, a
    map redrawing — for a picture nobody is looking at. So `src` is written back
    to about:blank, not merely hidden, and it happens in `shotViewClose`, which is
    the single function the scrim, the button, Discard and Escape all reach."""
    unframe = _between(html, "function shotViewUnframe()", "\n}\n")
    assert 'shotViewFrame.src = "about:blank";' in unframe, \
        "hiding it is not unmounting it"
    assert "shotViewFrame.hidden = true;" in unframe
    assert "shotViewLoad.hidden = true;" in unframe
    close = _between(html, "function shotViewClose()", "\n}\n")
    assert "shotViewUnframe();" in close
    # every exit goes through that one function: the modal test above pins the
    # scrim, the button and Escape to it, and Discard is the fourth
    assert "shotViewDrop.onclick = () => { shotDrop(shotViewing); shotViewClose(); };" \
        in html
    # and a second open takes the previous file's template down BEFORE this one's
    # stat comes back, so the viewer never shows the wrong preview
    open_ = _between(html, "function shotViewOpen(shot)", "\n}\n")
    assert open_.index("shotViewUnframe();") < open_.index("shotPreviewSrc(")


def test_only_a_file_asks_for_a_preview_and_it_says_it_is_loading(html):
    """Three rules in one branch. A PICTURE keeps the picture viewer — a template
    is a worse view of pixels than the pixels. A REFUSAL (`view` null) has no path
    to frame. And a first render can take seconds (a folder venv, a big parquet),
    so the line that says so is on screen from the click and comes off on the
    frame's own `load` — not a timer, because how long it takes is exactly what is
    unknowable here and is the reason the line exists."""
    open_ = _between(html, "function shotViewOpen(shot)", "\n}\n")
    assert 'if (bare && shot.kind === "file" && shot.view) {' in open_
    assert "shotViewLoad.hidden = false;" in open_
    assert "shotPreviewSrc(shot.view).then((src) => {" in open_
    # the open may be stale by the time stat answers: the user closed it, or opened
    # another attachment. Identity, the same test Discard uses.
    assert "if (shotViewing !== shot || shotView.hidden) return;" in open_
    # no template for this extension: the line goes, and the viewer is what it was
    assert "if (!src) { shotViewLoad.hidden = true; return; }" in open_
    assert "shotViewFrame.src = src;" in open_
    assert "shotViewFrame.onload = () => { shotViewLoad.hidden = true; };" in html
    assert ">loading preview…</div>" in html


def test_a_sent_files_receipt_glyph_opens_the_same_viewer(html):
    """A file that was previewable while it sat in the composer and unopenable the
    moment it was sent is the exact inconsistency that made the picture chip's
    thumbnail a button in the first place. So the receipt's glyph is the same door
    the chip's is — a real <button>, tabbable and announced — and only where there
    is a room behind it: a refused attachment keeps the plain span."""
    receipt = _between(html, "function shotReceipt(sum, shot)", "\n}\n")
    assert 'const isDoor = !!shot.view' in receipt
    assert 'lbl = document.createElement(isDoor ? "button" : "span");' in receipt
    assert "lbl.onclick = () => shotViewOpen(shot);" in receipt
    assert 'lbl.setAttribute("aria-label", shotAlt(shot) + " — open details");' \
        in receipt
    # a <button> arrives wearing the UA's chrome, and the row must not shift by a
    # pixel depending on whether the attachment happens to be openable
    assert ".annsum button.annsum-lbl {" in html
    style = _between(html, "  .annsum button.annsum-lbl {", "\n  }")
    assert "background: none" in style and "border: 0" in style
    assert "font: inherit" in style


def test_no_chip_and_no_receipt_ever_frames_a_preview(html):
    """Owner call, and the shape of the whole feature: the GLYPH by default, the
    preview on the click. A composer holding four attachments would otherwise boot
    four templates — four python workers, four first renders — to draw four
    icon-sized pictures nobody can read at that size. The iframe exists in exactly
    one place in this template."""
    assert html.count("<iframe") == 2, \
        "the left pane's and the viewer's, and nothing else"
    for fn in ("function shotChip(shot)", "function shotReceipt(sum, shot)",
               "function shotThumbBtn(cls, shot, alt)"):
        body = _between(html, fn, "\n}\n")
        assert "iframe" not in body, fn
        assert "shotPreviewSrc" not in body, fn


def test_a_pasted_picture_still_gets_a_chip_where_there_is_no_pane(html):
    """A folder with no app entry (D239) has no preview, no annotate layer and no
    camera — and is exactly where a user is most likely to paste a screenshot in,
    because there is nothing on screen for the agent to look at. `renderAnn`'s
    no-pane guard used to be a bare return, which would have swallowed the chip and
    left the picture attached invisibly: the silent-failure shape this whole
    feature is built to avoid."""
    assert "if (noPane && !CHAT_ONLY) { renderShotChips(); return; }" in html
    solo = _between(html, "function renderShotChips()", "\n}\n")
    assert "annChipsEls.forEach" in solo, "both composers, as ever"
    assert 'box.innerHTML = "";' in solo, \
        "clearing is what makes it an answer rather than an append"
    assert "for (const shot of shotAttached) box.appendChild(shotChip(shot));" in solo


# ------------------------------------------------------- what shotPane answers


def test_the_clone_is_the_first_answer_for_a_readable_pane(html):
    """D634 put the capture back where D285-D287 had it. `shotPane` has exactly
    two answers: the tab share for a cross-origin pane, and the clone-and-
    rasterise for a readable one — and the clone is the one a same-origin pane
    gets, with nothing tried ahead of it. The native screen shot D611 wired in
    first (`POST /api/capture/shot-region`, `shotNativePane`) is GONE from this
    template: the owner reverted it, so the endpoint's only caller here is the
    shell's own `fused.capture.screenshot()`, not the chat.

    The cost is deliberate and is what the test also pins by NOT asserting it
    away: a WebGL or canvas pane rasterises blank, and the blank is reported
    (shotNote / blanks) rather than shipped."""
    assert "shotNativePane" not in html, "the native pane shot is out (D634)"
    assert "shot-region" not in html, \
        "no chat capture may call /api/capture/shot-region any more"
    assert "shotNativeOff" not in html and "shotScreenRect" not in html \
        and "shotFrameOffset" not in html and "shotOverlayEls" not in html, \
        "and none of its helpers linger unused"
    # the two answers, in order: cross-origin first (there is no document to
    # clone), then the clone off the app's own live tree.
    pane = _between(html, "async function shotPane(deadline)", "const clone")
    assert "if (annXO) return shotXOPane();" in pane
    assert pane.index("if (annXO) return shotXOPane();") < pane.index("appWindow()"), \
        "the cross-origin refusal is the guard, not a fallback"
    body = _between(html, "async function shotPane(deadline)", "\n}\n")
    # the clone path itself, still whole: cloneNode -> inline styles/scroll/
    # images -> rasterise canvases -> foreignObject -> canvas
    assert "body.cloneNode(true)" in body
    assert "await shotInlineStyles(" in body
    assert "await shotInlineImages(" in body
    assert "shotRasterise(body, clone)" in body
    assert "shotApplyScroll(styles.scrolled)" in body
    assert "<foreignObject" in body and "data:image/svg+xml" in body
    # and the boot probe no longer pins a capture flag off `screenshot`
    probe = _between(html, "const src = await fused.capture.sources();", "\n  } catch")
    assert "src.screenshot" not in probe, "the probe is about the MIC now, only"
