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

This file is the PYTHON half: where the crops are allowed to land, how that
directory is prepared and pruned, the transcode of a picture neither side can
decode, and the read rules the spawn line grows for an attachment's own
directory. The capture itself — the crop arithmetic, the style walk, the image
inlining, the chips, the viewer — is the native chat's
(`frontend/src/apps/claude`) and is tested there under vitest, where the DOM is
real.
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

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


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


def test_the_rule_and_the_wire_spell_a_windows_shots_path_the_same_way(agent):
    """D146, and the reason `_wire_path` exists at all.

    The CLI matches an allow-rule as TEXT, not as a resolved path (see
    `_read_rule`), so the path inside `Read(//…/**)` and the path the chat puts
    in the annotation JSON have to be the SAME STRING or every crop raises a card
    and the whole pre-approval is defeated. On POSIX they agreed by accident; on
    Windows `SHOTS` comes off `os.path.join`, so the rule said
    `C:/Users/a/shots` while the chat joined `C:\\Users\\a\\shots\\x.png`.
    One normalisation on the python side is what makes them agree — and it is
    what the chat's own join is handed, so a crop path sits under the rule's
    prefix textually, which is the only way the CLI compares them."""
    win = r"C:\Users\a\AppData\Local\Temp\fr\shots"
    handed = agent._wire_path(win)          # what the chat is given
    rule = agent._read_rule(win)            # what the spawn line pre-approves
    assert handed == "C:/Users/a/AppData/Local/Temp/fr/shots"
    assert rule == "Read(//C:/Users/a/AppData/Local/Temp/fr/shots/**)"
    assert "\\" not in handed
    # A crop path under that directory sits under the rule's prefix textually.
    assert (handed + "/x.png").startswith(rule[len("Read(//"):-len("**)")])


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


def test_the_shots_dir_action_answers_with_the_wire_spelling(
        agent, tmp_path, monkeypatch):
    """D146, the two-sided wire: the chat names the action, agent.py routes it."""
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


def test_the_transcode_action_the_chat_calls_is_one_main_routes(agent):
    """D146, the two-sided wire, for the second action this feature added."""
    assert 'if action == "image_to_png"' in open(
        os.path.join(TEMPLATE_DIR, "agent.py"), encoding="utf-8").read()


# ------------------------------ the read rules an attachment asks the spawn for

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
