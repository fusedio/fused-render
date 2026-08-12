"""OS drag-and-drop staging for the Map Viewer.

An ad-hoc drop (no folder browsed yet) used to resolve `dir=""` to the user's
real home folder, so a stray drag out of Explorer landed there permanently and
repeated drops of the same name piled up as `name (1).gpkg`, `name (2).gpkg`
forever. Drops now stage in a pruned per-user directory under the temp root
(`discover.py main(action="drops_dir")`), built on the same private-directory
rules claude/agent.py uses for its screenshots (`shared/private_dir.py`).
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import pytest

MAP = os.path.join("fused_render", "templates", "map")
TEMPLATE = os.path.join(MAP, "template.html")


@pytest.fixture
def discover():
    if os.path.abspath(MAP) not in sys.path:
        sys.path.insert(0, os.path.abspath(MAP))
    spec = importlib.util.spec_from_file_location(
        "map_drops_discover", os.path.join(MAP, "discover.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def html():
    return open(TEMPLATE, encoding="utf-8").read()


# ---------------------------------------------------- preparing the directory

def test_drops_stage_under_the_shared_temp_root(discover):
    """The bug this exists to fix: `dir=""` resolved to Path.home(), so an
    ad-hoc drop landed permanently in the user's home folder. Staged drops
    live under the temp root instead (which DesktopPaths redirects to the
    app's own dotdir for every child process)."""
    assert discover.DROPS.startswith(tempfile.gettempdir() + os.sep)
    assert os.path.basename(discover.DROPS) == "drops"


def test_the_drops_dir_is_created_private_and_adopted_on_a_second_call(
        discover, tmp_path, monkeypatch):
    """Two drops in one session: the second call adopts the directory the
    first one made, rather than refusing or recreating it."""
    drops = tmp_path / "fused_render_map" / "drops"
    monkeypatch.setattr(discover, "DROPS", str(drops))
    assert discover.main(action="drops_dir") == {"dir": str(drops)}
    assert os.path.isdir(drops)
    if hasattr(os, "geteuid"):
        assert stat.S_IMODE(os.lstat(drops).st_mode) == 0o700
    assert discover.main(action="drops_dir") == {"dir": str(drops)}


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX mode bits")
def test_a_drops_dir_anyone_can_write_to_is_refused(discover, tmp_path,
                                                    monkeypatch):
    """The temp root is world-writable and our path under it is predictable,
    so another account can pre-create this directory. Adopting theirs would
    hand them every file this user drops."""
    drops = tmp_path / "fused_render_map" / "drops"
    drops.mkdir(parents=True)
    os.chmod(drops, 0o777)
    monkeypatch.setattr(discover, "DROPS", str(drops))
    with pytest.raises(PermissionError):
        discover._drops_dir()


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX mode bits")
def test_an_adopted_drops_dir_is_tightened_to_owner_only(discover, tmp_path,
                                                         monkeypatch):
    drops = tmp_path / "fused_render_map" / "drops"
    drops.mkdir(parents=True)
    os.chmod(drops, 0o755)
    monkeypatch.setattr(discover, "DROPS", str(drops))
    assert discover._drops_dir() == {"dir": str(drops)}
    assert stat.S_IMODE(os.lstat(drops).st_mode) == 0o700


def test_a_drops_dir_that_cannot_be_made_is_an_error_not_a_crash(
        discover, tmp_path, monkeypatch):
    """No directory means no drop, which the page surfaces as a toast. It must
    never be an unhandled traceback."""
    blocker = tmp_path / "fused_render_map"
    blocker.write_text("not a directory")
    monkeypatch.setattr(discover, "DROPS", str(blocker / "drops"))
    out = discover._drops_dir()
    assert out.get("error") and "dir" not in out


# ------------------------------------------------------------------- pruning

def test_a_fresh_drop_survives_pruning(discover, tmp_path, monkeypatch):
    """Two drops in a row must not eat each other: only TTL-expired files and
    the oldest past the count cap go."""
    drops = tmp_path / "drops"
    drops.mkdir()
    monkeypatch.setattr(discover, "DROPS", str(drops))
    monkeypatch.setattr(discover, "DROPS_KEEP", 3)
    fresh = drops / "parcels.gpkg"
    fresh.write_bytes(b"x" * 32)
    stale = drops / "ancient.tif"
    stale.write_bytes(b"x")
    os.utime(stale, (0, time.time() - discover.DROPS_TTL - 60))
    for i in range(4):
        p = drops / ("old%d.tif" % i)
        p.write_bytes(b"x")
        os.utime(p, (0, time.time() - 3600 - i))
    discover._prune_drops()
    assert fresh.exists(), "a file dropped a moment ago must survive"
    assert not stale.exists(), "a TTL-expired file must not"
    assert len(list(drops.iterdir())) <= 3


def test_pruning_never_fails_the_action(discover, tmp_path, monkeypatch):
    monkeypatch.setattr(discover, "DROPS", str(tmp_path / "never-made"))
    discover._prune_drops()  # must not raise


def test_a_dropped_file_can_outlive_the_session_that_made_it(discover):
    """The staged path is what the layer references, so it has to survive at
    least a long working session — days, not the hours a screenshot gets."""
    assert discover.DROPS_TTL >= 24 * 3600


# ------------------------------------------------------------------- the wire

def test_the_page_asks_for_the_directory_by_the_action_the_backend_serves(
        discover, html, tmp_path, monkeypatch):
    assert 'action: "drops_dir"' in html
    drops = tmp_path / "fused_render_map" / "drops"
    monkeypatch.setattr(discover, "DROPS", str(drops))
    assert discover.main(action="drops_dir") == {"dir": str(drops)}


def test_the_browse_listing_is_untouched_by_the_new_action_default(discover,
                                                                   tmp_path):
    (tmp_path / "scene.tif").write_bytes(b"x")
    out = discover.main(dir=str(tmp_path))
    assert out["dir"] == str(tmp_path)
    assert [e["name"] for e in out["entries"]] == ["scene.tif"]


# ---------------------------------------------- the page's own JS, under node

def _node(html, body):
    """Run resolveUploadDir out of template.html under node, with the fused
    bridge stubbed. Same extraction idea as tests/test_claude_shots.py's
    `_node`: the function ends at the first closing brace at column 0."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own upload-dir helper")
    chunks = []
    for name in ("let dropsDirPromise", "async function resolveUploadDir("):
        start = html.index(name)
        if name.startswith("let "):
            chunks.append(html[start:html.index("\n", start)])
        else:
            chunks.append(html[start:html.index("\n}\n", start) + 3])
    script = body + "\n" + "\n".join(chunks) + "\nmain();"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_browsed_folder_still_wins_over_the_staging_dir(html):
    """Once the user has picked a folder in the file browser, drops keep
    landing there — the staging dir is only the no-folder fallback."""
    out = _node(html, """
var state = {dir: "C:/data"};
var calls = [];
var fused = {runPython: async (s, p) => { calls.push(p); return {dir: "/tmp/d"}; }};
async function main() {
  const dir = await resolveUploadDir();
  console.log(JSON.stringify({dir: dir, calls: calls}));
}
""")
    assert out == {"dir": "C:/data", "calls": []}


def test_two_drops_share_one_backend_call(html):
    out = _node(html, """
var state = {dir: ""};
var calls = [];
var fused = {runPython: async (s, p) => { calls.push([s, p]); return {dir: "/tmp/drops"}; }};
async function main() {
  const a = await resolveUploadDir();
  const b = await resolveUploadDir();
  console.log(JSON.stringify({a: a, b: b, calls: calls}));
}
""")
    assert out["a"] == out["b"] == "/tmp/drops"
    assert out["calls"] == [["./discover.py", {"action": "drops_dir"}]]


def test_a_failed_resolution_is_retried_not_cached(html):
    """The reset-on-failure that was itself a recent bug fix: a rejected
    promise must not poison every later drop of the session."""
    out = _node(html, """
var state = {dir: ""};
var n = 0;
var fused = {runPython: async () => {
  if (++n === 1) throw new Error("engine cold");
  return {dir: "/tmp/drops"};
}};
async function main() {
  let firstError = "";
  try { await resolveUploadDir(); } catch (e) { firstError = e.message; }
  const dir = await resolveUploadDir();
  console.log(JSON.stringify({firstError: firstError, dir: dir, n: n}));
}
""")
    assert out == {"firstError": "engine cold", "dir": "/tmp/drops", "n": 2}


def test_a_backend_error_dict_fails_the_drop_with_its_message(html):
    """`_drops_dir` degrades OS trouble to `{"error": ...}`; the page has to
    turn that into a thrown (and toasted) failure, not upload into ''."""
    out = _node(html, """
var state = {dir: ""};
var results = [{error: "Could not prepare the drop directory: disk full"},
               {dir: "/tmp/drops"}];
var fused = {runPython: async () => results.shift()};
async function main() {
  let msg = "";
  try { await resolveUploadDir(); } catch (e) { msg = e.message; }
  const dir = await resolveUploadDir();
  console.log(JSON.stringify({msg: msg, dir: dir}));
}
""")
    assert "disk full" in out["msg"]
    assert out["dir"] == "/tmp/drops", "an error dict must also reset the memo"
