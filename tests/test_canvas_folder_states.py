"""One answer to "is this folder a clone?", everywhere it is asked.

A folder under `<env>/user/` is a clone, a reference copy the mirror command
owns, a copy being turned into a clone whose pull has not landed, or nothing.
`canvas_marker.folder_state` is the one reading; the server's routes, the
push interception, the desk and the agent each ask it (or, where a module
cannot import the package, hold a copy of it). These drive ALL of them over
the same folders and assert they agree.
"""
import importlib.util
import json
import os

import pytest

import fused_render.canvases as canvases_mod
from fused_render import _canvas_push, canvas_marker, current_apps
from test_canvases import ENV, _reset_module_state, harness  # noqa: F401 — fixtures by name

MARKER = canvas_marker.MARKER_NAME


def _marker(sync):
    return json.dumps({"version": 1, "sync": sync, "scope": "user", "id": "col-1",
                       "name": "x", "owner": None, "revision": 1,
                       "last_updated": None, "pulled_at": "t"})


#: name -> (marker text or None, adoption flag?, canvas.toml?, folder exists?)
FOLDERS = {
    "live": (_marker("live"), False, True, True),
    "unmarked": (None, False, True, True),
    "broken_marker": ("{not json", False, True, True),
    "mirror": (_marker("mirror"), False, True, True),
    "broken_mirror": ('{"sync": "mirror"}', False, True, True),
    "mirror_flagged": (_marker("mirror"), True, True, True),       # crash before the flip
    "live_flagged": (_marker("live"), True, True, True),           # pull not landed
    "unmarked_flagged": (None, True, True, True),                  # same, external CLI
    "no_toml": (_marker("live"), False, False, True),
    "missing": (None, False, False, False),
    "missing_flagged": (None, True, False, False),                 # a flag left behind
}
#: What each is, and whether it is a clone / holds a reference copy's files.
EXPECTED = {
    "live": ("live", True, False),
    "unmarked": ("unmarked", True, False),
    "broken_marker": ("unmarked", True, False),
    "mirror": ("mirror", False, True),
    "broken_mirror": ("mirror", False, True),
    "mirror_flagged": ("mirror", False, True),
    "live_flagged": ("adopting", False, True),
    "unmarked_flagged": ("adopting", False, True),
    "no_toml": ("live", False, False),
    "missing": ("missing", False, False),
    "missing_flagged": ("missing", False, False),
}


@pytest.fixture()
def folders(harness):
    sync = harness.root / ".sync"
    sync.mkdir(parents=True)
    harness.clones.mkdir()
    for name, (marker, flagged, toml, exists) in FOLDERS.items():
        folder = harness.clones / name
        if exists:
            folder.mkdir()
            (folder / "a.py").write_text("x = 1\n")
            if toml:
                (folder / "canvas.toml").write_text('type = "canvas"\n')
            if marker is not None:
                (folder / MARKER).write_text(marker)
        if flagged:
            (sync / f"{name}.adopting").write_text("1")
    return harness


@pytest.fixture()
def agent(folders, monkeypatch):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "fused_render", "claude_agent", "agent.py")
    spec = importlib.util.spec_from_file_location("agent_for_folder_states", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_canvases_root", lambda: str(folders.base))
    return module


def test_the_matrix_covers_every_state():
    assert set(FOLDERS) == set(EXPECTED)
    assert {state for state, _c, _r in EXPECTED.values()} == {
        canvas_marker.STATE_LIVE, canvas_marker.STATE_UNMARKED, canvas_marker.STATE_MIRROR,
        canvas_marker.STATE_ADOPTING, canvas_marker.STATE_MISSING}


@pytest.mark.parametrize("name", sorted(FOLDERS))
def test_every_reader_agrees_on_what_a_folder_is(folders, agent, name):
    harness = folders
    folder = harness.clones / name
    state, is_clone, is_reference = EXPECTED[name]

    # The one reading.
    assert canvas_marker.folder_state(str(folder)) == state
    assert canvas_marker.is_clone(str(folder)) is is_clone
    assert canvas_marker.is_reference(str(folder)) is is_reference
    assert canvas_marker.adopting_path(str(folder)) == canvases_mod._adopting_path(ENV, name)

    # The server: listing, sync start, the hook's revival.
    assert canvases_mod._is_live_clone(ENV, name) is is_clone
    assert (name in canvases_mod._cloned_names(ENV)) is is_clone
    assert (name in [n for _e, n, _d in canvases_mod.cloned_canvases()]) is is_clone
    assert canvases_mod.in_reference_copy(str(folder / "a.py")) is is_reference

    # The push interception: a clone's push goes to the server, a reference
    # copy's is refused. (A folder that is neither, under `user/`, is asked
    # about like a clone: the server then says it is not synced.)
    located = _canvas_push._locate(str(folder))
    assert located["kind"] == ("reference" if is_reference else "clone")
    assert (_canvas_push._reference_of(str(folder)) is not None) is is_reference
    assert (_canvas_push._clone_name(str(folder)) is None) is is_reference

    # The desk: a reference copy's row is not a canvas row; a clone's is, and
    # so is the row of a clone that is gone.
    row = current_apps._canvas_of(str(folder))
    assert (row is None) is is_reference
    if not is_reference:
        assert row == {"env": ENV, "name": name}

    # The agent's copies (it cannot import the package).
    assert agent._canvas_is_reference(str(folder)) is is_reference
    assert agent._canvas_is_mirror(str(folder)) is (state == "mirror")
    assert agent._in_canvas_reference(str(folder / "a.py")) is is_reference
    assert agent._in_synced_clone(str(folder / "a.py")) is (not is_reference)
    assert (agent._canvas_of(str(folder / "a.py")) is None) is is_reference
    if is_clone:
        assert agent._canvas_of(str(folder / "a.py"))["name"] == name


def test_only_a_clone_gets_a_watcher(folders):
    harness = folders
    harness.log_in()
    from test_canvases import GUARD

    for name, (_state, is_clone, _ref) in EXPECTED.items():
        res = harness.client.post("/api/canvases/sync/start", headers=GUARD,
                                  json={"env": ENV, "name": name})
        assert (res.status_code == 200) is is_clone, (name, res.text)
        assert ((ENV, name) in canvases_mod._syncs) is is_clone, name
