"""The reference canvases' background runner (canvas_mirror.py, D1477).

`fused workbench canvas mirror` is run as a child, its stderr read for
progress and its stdout for one JSON document; the outcome is classified from
the exit. The CLI is test_canvases.py's stub, which speaks the contract's
output (v1.4). What these pin:

* a `fused` from before the command: `unsupported`, never spawned
  again, and every status route still answers.
* the child never outlives the app, and nothing spawns unless a test
  (or the server's startup hook) starts it.
* exit 1 with a document is a PARTIAL result, counts kept.
* no `fused` at all: the module imports and answers `no_cli`.
* `hold` stops the child and starts one again after.
"""
import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

import fused_render.canvases as canvases_mod
import fused_render.fusedcli as fusedcli_mod
from fused_render import canvas_layout, canvas_marker, canvas_mirror
from fused_render.server import create_app
from test_canvases import (  # noqa: F401 — fixtures are used by name
    ENV,
    GUARD,
    OTHER_ENV,
    _reset_module_state,
    _wait_for,
    harness,
)

MIRROR_ARGV = ["workbench", "--format", "json", "canvas", "mirror", "--output"]


@pytest.fixture(autouse=True)
def _default_env_and_fresh_layout(monkeypatch):
    """ENV is the default environment (so it is "in use"), and no layout
    answer from another test's root is remembered."""
    monkeypatch.setattr(canvas_mirror, "workbench_env", lambda: ENV)
    canvas_layout.reset()
    yield
    canvas_layout.reset()


def _mirror_calls(harness):
    return [c for c in harness.calls() if c[:6] == MIRROR_ARGV]


def _scope(ok=True, listed=0, error=None, unavailable=False):
    return {"ok": ok, "unavailable": unavailable, "listed": listed, "error": error}


def _result(counts=None, scopes=None, canvases=None):
    return {
        "root": "/somewhere", "dry_run": False,
        "scopes": {"user": _scope(listed=2), "team": _scope(listed=1),
                   "public": _scope(listed=3), **(scopes or {})},
        "counts": {"pulled": 0, "updated": 0, "unchanged": 0, "removed": 0,
                   "live": 0, "unmanaged": 0, "failed": 0, **(counts or {})},
        "canvases": canvases or [],
    }


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A killed child of ours is a zombie until reaped; the runner reaps it.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def _slow(harness, tmp_path, **extra):
    """A scenario whose mirror run sleeps for a minute, holding the lock."""
    started = tmp_path / "mirror-started"
    harness.set_scenario({"mirror_delay": 60, "mirror_lock": True,
                          "mirror_started_file": str(started), **extra})
    return started


def _wait_started(started) -> int:
    assert _wait_for(lambda: started.exists() and started.read_text().strip())
    return int(started.read_text())


# -- a run and what it records -------------------------------------------------


def test_a_clean_run_is_ok_and_keeps_what_the_command_said(harness):
    harness.log_in()
    harness.set_scenario({"mirror_result": _result(
        counts={"pulled": 4, "updated": 1, "unchanged": 7, "removed": 2, "live": 1})})
    status = canvas_mirror.run_once(ENV, "manual")
    assert status.state == "ok" and status.error is None and status.trigger == "manual"
    assert status.counts == {"pulled": 4, "updated": 1, "unchanged": 7, "removed": 2,
                             "live": 1, "unmanaged": 0, "failed": 0}
    assert status.scopes["public"] == _scope(listed=3)
    assert status.mirrored() == 12
    assert status.finished_at >= status.started_at
    # The command ran once, on the environment's ROOT, against ITS Fused.
    assert _mirror_calls(harness) == [[*MIRROR_ARGV, str(harness.root)]]
    assert [fused_env for fused_env, argv in harness.env_calls()
            if argv[:6] == MIRROR_ARGV] == [ENV]
    # All three scope folders exist afterwards, whatever the command wrote.
    assert sorted(p.name for p in harness.root.iterdir()) == [".sync", "public", "team", "user"]


def test_the_last_result_is_kept_on_disk(harness):
    harness.log_in()
    harness.set_scenario({"mirror_result": _result(counts={"unchanged": 9})})
    canvas_mirror.run_once(ENV)
    saved = json.loads((harness.root / ".sync" / ".mirror.json").read_text())
    assert saved["state"] == "ok" and saved["counts"]["unchanged"] == 9 and saved["env"] == ENV
    assert "canvases" not in saved
    # A new process (nothing in memory) reads it back.
    canvas_mirror._last.clear()
    status = canvas_mirror.status(ENV)
    assert status["state"] == "ok" and status["mirrored"] == 9
    # A file that is not ours to read is a blank status, never an error.
    (harness.root / ".sync" / ".mirror.json").write_text("{not json")
    canvas_mirror._last.clear()
    assert canvas_mirror.status(ENV)["state"] == "idle"


def test_the_status_has_the_shape_the_page_reads(harness):
    harness.log_in()
    status = canvas_mirror.status(ENV)
    assert set(status) == {
        "env", "state", "last_state", "trigger", "started_at", "finished_at", "done",
        "total", "last_line", "counts", "scopes", "error", "live_orphans", "mirrored",
        "root", "held", "eligible", "skip_reason", "layout"}
    assert status["layout"] == {"deferred": {}, "conflicts": {}, "blocked": "", "error": ""}
    assert status["state"] == "idle" and status["finished_at"] is None
    assert status["counts"] == {k: 0 for k in canvas_mirror.COUNT_KEYS}
    assert status["scopes"] == {} and status["live_orphans"] == []
    assert status["root"] == str(harness.root) and status["held"] is False
    assert status["eligible"] is True and status["skip_reason"] is None
    assert set(canvas_mirror.summary(ENV)) == {
        "state", "last_state", "done", "total", "finished_at", "mirrored", "error",
        "eligible", "skip_reason"}
    # Reading the status creates nothing and runs nothing.
    assert not harness.base.exists() and _mirror_calls(harness) == []


def test_progress_lines_show_while_the_run_is_in_flight(harness, tmp_path):
    harness.log_in()
    harness.set_scenario({"mirror_result": _result(counts={"unchanged": 5})})
    canvas_mirror.run_once(ENV)
    started = _slow(harness, tmp_path, mirror_progress=[
        "warning: something odd", "[1/340] pulled public/Some_Canvas",
        "[2/340] unchanged user/alpha"])
    assert canvas_mirror.kick(ENV, "manual", force=True) == {"started": True, "reason": None}
    _wait_started(started)
    assert _wait_for(lambda: canvas_mirror.status(ENV)["done"] == 2)
    status = canvas_mirror.status(ENV)
    assert status["state"] == "running" and status["last_state"] == "ok"
    assert (status["done"], status["total"]) == (2, 340)
    assert status["last_line"] == "[2/340] unchanged user/alpha"
    assert status["trigger"] == "manual"
    # The last finished run's numbers stay on show under the running one.
    assert status["mirrored"] == 5 and status["counts"]["unchanged"] == 5
    # One run at a time.
    assert canvas_mirror.kick(ENV, "manual", force=True) == {
        "started": False, "reason": "running"}
    assert len(_mirror_calls(harness)) == 2


def test_exit_1_with_a_document_is_a_partial_result_with_its_counts(harness):
    """A scope whose listing failed is skipped by the command — no pulls
    and NO removals — and the others ran: not a failed run."""
    harness.log_in()
    harness.set_scenario({"mirror_exit": 1, "mirror_result": _result(
        counts={"pulled": 3, "unchanged": 8, "failed": 1},
        scopes={"team": _scope(ok=False, error="HTTP 500 from the server")})})
    status = canvas_mirror.run_once(ENV)
    assert status.state == "partial"
    assert status.counts["pulled"] == 3 and status.counts["unchanged"] == 8
    assert status.counts["failed"] == 1 and status.counts["removed"] == 0
    assert status.scopes["team"] == _scope(ok=False, error="HTTP 500 from the server")
    assert status.error == "team: HTTP 500 from the server"
    assert status.mirrored() == 11


def test_a_failed_canvas_alone_is_partial_too(harness):
    harness.log_in()
    harness.set_scenario({"mirror_exit": 1, "mirror_result": _result(
        counts={"pulled": 3, "failed": 2})})
    status = canvas_mirror.run_once(ENV)
    assert status.state == "partial" and status.error == "2 canvases could not be pulled"


def test_unconfirmed_removals_are_partial_with_the_scope_still_ok(harness):
    """Contract v1.4: the second listing failed, so nothing was deleted; the
    scope keeps `ok: true`, carries the message, and the exit is 1."""
    harness.log_in()
    message = "Removals were not confirmed: the second listing failed"
    harness.set_scenario({"mirror_exit": 1, "mirror_result": _result(
        counts={"unchanged": 4}, scopes={"public": _scope(listed=3, error=message)})})
    status = canvas_mirror.run_once(ENV)
    assert status.state == "partial" and status.error == f"public: {message}"
    assert status.scopes["public"]["ok"] is True


def test_a_user_with_no_team_is_a_clean_run(harness):
    """Contract v1.2: `unavailable` is normal, exit 0, and not the status's
    error."""
    harness.log_in()
    harness.set_scenario({"mirror_result": _result(scopes={"team": _scope(
        ok=False, unavailable=True, error="403: you are not in a team")})})
    status = canvas_mirror.run_once(ENV)
    assert status.state == "ok" and status.error is None
    assert status.scopes["team"]["unavailable"] is True


def test_a_cli_without_the_command_is_unsupported_and_never_spawned_again(harness):
    """Exit 2 with click's "No such command": remembered for this CLI,
    and everything else keeps answering."""
    harness.log_in()
    harness.set_scenario({"mirror_unsupported": True})
    assert canvas_mirror.kick(ENV, "startup")["started"] is True
    assert _wait_for(lambda: canvas_mirror.status(ENV)["state"] == "unsupported")
    assert len(_mirror_calls(harness)) == 1
    status = canvas_mirror.status(ENV)
    assert status["eligible"] is False and status["skip_reason"] == "unsupported"
    assert "newer" in status["error"]
    # No kick of any kind spawns it again, on any environment.
    for env in (ENV, OTHER_ENV):
        for force in (False, True):
            assert canvas_mirror.kick(env, "manual", force=force) == {
                "started": False, "reason": "unsupported"}
    assert canvas_mirror.run_once(ENV).state == "unsupported"
    assert canvas_mirror.eligible_envs() == [] and canvas_mirror._timer_done() is True
    assert len(_mirror_calls(harness)) == 1
    # Every route still answers, and says so.
    res = harness.client.get(f"/api/canvases/mirror/status?env={ENV}")
    assert res.status_code == 200 and res.json()["state"] == "unsupported"
    res = harness.client.post("/api/canvases/mirror/run", json={"env": ENV}, headers=GUARD)
    assert res.status_code == 200 and res.json()["started"] is False
    body = harness.client.get("/api/canvases/status").json()
    assert body["envs"][ENV]["mirror"]["state"] == "unsupported"
    # ...and the rest of the Canvases page works as before.
    assert harness.client.get("/api/canvases/list?env=unstable", headers=GUARD).status_code == 200
    assert harness.client.post("/api/canvases/clone", headers=GUARD,
                               json={"env": ENV, "name": "alpha"}).status_code == 200


def test_another_usage_error_is_not_mistaken_for_an_old_cli(harness):
    harness.log_in()
    harness.set_scenario({"mirror_stdout": "", "mirror_exit": 2})
    status = canvas_mirror.run_once(ENV)
    assert status.state == "failed"
    harness.set_scenario({})
    assert canvas_mirror.run_once(ENV).state == "ok"


def test_a_root_another_run_holds_is_busy_not_an_error(harness):
    harness.log_in()
    harness.set_scenario({"mirror_result": _result(counts={"unchanged": 6})})
    canvas_mirror.run_once(ENV)
    harness.set_scenario({"mirror_busy": True})
    status = canvas_mirror.run_once(ENV)
    assert status.state == "busy" and status.error is None
    assert status.mirrored() == 6           # what the last good run found stands
    # Not a "fresh" result: the next scheduled kick tries again.
    assert canvas_mirror.kick(ENV, "timer")["started"] is True


@pytest.mark.parametrize("scenario, state, needle", [
    ({"mirror_stdout": "Traceback (most recent call last):\n  boom"}, "failed", "no result"),
    ({"mirror_stdout": "[1, 2, 3]"}, "failed", "no result"),
    ({"mirror_stdout": '{"scopes": "nope"}', "mirror_exit": 1}, "failed", "no result"),
    ({"fail": "the server is on fire"}, "failed", "the server is on fire"),
    ({"fail": "Please re-authenticate with `fused login`"}, "auth", "re-authenticate"),
])
def test_anything_else_is_failed_or_a_sign_in(harness, scenario, state, needle):
    harness.log_in()
    harness.set_scenario(scenario)
    status = canvas_mirror.run_once(ENV)
    assert status.state == state and needle in status.error
    assert status.counts == {k: 0 for k in canvas_mirror.COUNT_KEYS}


def test_a_document_among_other_output_is_still_read(harness):
    harness.log_in()
    harness.set_scenario({"mirror_stdout": "some import warned\n"
                          + json.dumps(_result(counts={"pulled": 2})) + "\n"})
    assert canvas_mirror.run_once(ENV).counts["pulled"] == 2


def test_every_listing_refused_for_the_sign_in_is_auth(harness):
    harness.log_in()
    stale = "Could not refresh your Fused credentials; re-authenticate"
    harness.set_scenario({"mirror_exit": 1, "mirror_result": _result(scopes={
        s: _scope(ok=False, error=stale) for s in ("user", "team", "public")})})
    status = canvas_mirror.run_once(ENV)
    assert status.state == "auth" and status.error == stale


def test_a_run_past_its_time_is_stopped_and_its_lock_removed(harness, tmp_path, monkeypatch):
    harness.log_in()
    started = _slow(harness, tmp_path)
    monkeypatch.setattr(canvas_mirror, "RUN_TIMEOUT_S", 1.0)
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 0.5)
    status = canvas_mirror.run_once(ENV)
    assert status.state == "failed" and "stopped after" in status.error
    assert not _alive(int(started.read_text()))
    assert not (harness.root / ".mirror.lock").exists()


def test_a_lock_that_is_not_our_childs_is_left_alone(harness):
    harness.root.mkdir(parents=True)
    lock = harness.root / ".mirror.lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "t", "refreshed_at": "t"}))
    canvas_mirror._clear_own_lock(ENV, os.getpid() + 1)
    assert lock.exists()
    lock.write_text("not json")
    canvas_mirror._clear_own_lock(ENV, os.getpid())
    assert lock.exists()


def test_live_orphans_are_clones_in_no_listing(harness):
    """Report only: a clone whose canvas was deleted on the server (or is no
    longer this account's) is listed by folder name, and left alone."""
    harness.log_in()

    def folder(scope, name, sync, cid):
        path = harness.root / scope / name
        path.mkdir(parents=True)
        (path / "canvas.toml").write_text("")
        if sync:
            (path / canvas_marker.MARKER_NAME).write_text(json.dumps({
                "version": 1, "sync": sync, "scope": scope, "id": cid, "name": name}))
        return path

    folder("user", "kept", "live", "id-kept")
    gone = folder("user", "gone", "live", "id-gone")
    folder("user", "copy", "mirror", "id-not-listed")      # the command's, not a clone
    folder("user", "unmarked", None, None)                 # no id to judge by
    folder("team", "theirs", "live", "id-team-live")       # only `user/` holds clones
    listed = [{"id": "id-kept", "scope": "user", "name": "kept", "path": "user/kept",
               "action": "live", "error": None}]
    harness.set_scenario({"mirror_result": _result(canvases=listed)})
    assert canvas_mirror.run_once(ENV).live_orphans == ["gone"]
    assert canvas_mirror.status(ENV)["live_orphans"] == ["gone"]
    assert (gone / "canvas.toml").is_file()
    # A failed `user` listing says nothing about what is gone.
    harness.set_scenario({"mirror_exit": 1, "mirror_result": _result(
        canvases=[], scopes={"user": _scope(ok=False, error="HTTP 500")})})
    assert canvas_mirror.run_once(ENV).live_orphans == []


def test_clones_at_the_old_path_move_before_the_command_runs(harness):
    harness.log_in()
    legacy = harness.root / "alpha"
    legacy.mkdir(parents=True)
    (legacy / "canvas.toml").write_text("")
    assert canvas_mirror.run_once(ENV).state == "ok"
    assert (harness.clones / "alpha" / "canvas.toml").is_file() and not legacy.exists()


def test_a_clone_named_user_in_the_way_stops_the_run_before_it_starts(harness, monkeypatch):
    """The command would write its `user/` copies INTO that clone's folder."""
    harness.log_in()
    (harness.root / "user").mkdir(parents=True)
    (harness.root / "user" / "canvas.toml").write_text("")
    (harness.root / "user" / "inner").mkdir()
    (harness.root / "user" / "inner" / "canvas.toml").write_text("")   # so it cannot move
    status = canvas_mirror.run_once(ENV)
    assert status.state == "failed" and "must move out" in status.error
    assert _mirror_calls(harness) == []
    assert not (harness.root / "team").exists()


# -- no CLI at all --------------------------------------------------------


def test_no_cli_is_a_state_not_an_error(harness, monkeypatch):
    harness.log_in()
    monkeypatch.setattr(canvases_mod, "fused_cli", lambda: None)
    assert canvas_mirror.kick(ENV, "manual", force=True) == {
        "started": False, "reason": "no_cli"}
    assert canvas_mirror.run_once(ENV).state == "idle"
    status = canvas_mirror.status(ENV)
    assert status["state"] == "no_cli" and status["eligible"] is False
    assert canvas_mirror.eligible_envs() == []
    res = harness.client.get("/api/canvases/mirror/status")
    assert res.status_code == 200
    assert {s["state"] for s in res.json()["envs"].values()} == {"no_cli"}
    assert harness.client.get("/api/canvases/status").json()["envs"][ENV]["mirror"][
        "state"] == "no_cli"
    with canvas_mirror.hold(ENV):
        pass
    canvas_mirror.start()
    canvas_mirror.shutdown(latch=False)


def test_the_module_imports_and_answers_without_the_fused_package(tmp_path):
    """The `minimal-install` CI job: no `fused` extra. `fused` is made
    unimportable in a fresh interpreter, with no CLI override."""
    code = (
        "import sys\n"
        "sys.modules['fused'] = None\n"
        "from fused_render import canvas_mirror\n"
        "print(canvas_mirror.kick('prod', 'manual', force=True))\n"
        "print(canvas_mirror.status('prod')['state'])\n"
        "print(canvas_mirror.run_once('prod').state)\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("FUSED_RENDER_FUSED_BIN", "FUSED_RENDER_WORKBENCH_ENV")}
    env["FUSED_RENDER_CANVASES_DIR"] = str(tmp_path / "canvases")
    env["FUSED_RENDER_HOME"] = str(tmp_path / "home")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "{'started': False, 'reason': 'no_cli'}", "no_cli", "idle"]
    assert not (tmp_path / "canvases").exists()


# -- which environments, and when ----------------------------------------------


def test_only_environments_in_use_are_mirrored_on_a_schedule(harness, monkeypatch):
    """prod and unstable share one sign-in: without the rule every user
    would mirror both."""
    assert canvas_mirror.eligible(ENV) == (False, "signed_out")
    harness.log_in()
    assert canvas_mirror.eligible(ENV) == (True, None)             # the default one
    assert canvas_mirror.eligible(OTHER_ENV) == (False, "not_in_use")
    assert canvas_mirror.eligible(OTHER_ENV, scheduled=False) == (True, None)
    assert canvas_mirror.eligible_envs() == [ENV]
    assert canvas_mirror.kick(OTHER_ENV, "timer") == {"started": False, "reason": "not_in_use"}
    # A CLONE under its `user/`. Not any folder: the command itself fills
    # `user/` with reference copies of the account's own canvases, and one
    # run would then keep the environment mirrored for good.
    beta = harness.base / OTHER_ENV / "user" / "beta"
    beta.mkdir(parents=True)
    (beta / "canvas.toml").write_text("")
    (beta / canvas_marker.MARKER_NAME).write_text(json.dumps({"sync": "mirror"}))
    assert canvas_mirror.eligible(OTHER_ENV) == (False, "not_in_use")
    (beta / canvas_marker.MARKER_NAME).unlink()
    assert canvas_mirror.eligible(OTHER_ENV) == (True, None)
    (beta / "canvas.toml").unlink()
    assert canvas_mirror.eligible(OTHER_ENV) == (False, "not_in_use")
    # ...or its catalog listed this session.
    assert harness.client.get(f"/api/canvases/list?env={OTHER_ENV}",
                              headers=GUARD).status_code == 200
    assert canvas_mirror.eligible(OTHER_ENV) == (True, None)
    assert canvas_mirror.eligible("staging") == (False, "unknown_env")
    # custom needs its settings file.
    canvas_mirror.note_listed("custom")
    monkeypatch.setattr(canvases_mod, "fused_settings_configured", lambda: False)
    assert canvas_mirror.eligible("custom") == (False, "not_configured")
    # A pin locks every other environment out, as it does every canvas call.
    monkeypatch.setattr(canvas_mirror, "workbench_env_locked", lambda: True)
    assert canvas_mirror.eligible(OTHER_ENV) == (False, "env_locked")
    assert canvas_mirror.eligible(ENV) == (True, None)
    assert _mirror_calls(harness) == []


def test_a_scheduled_kick_skips_a_fresh_result_and_a_manual_one_does_not(harness, monkeypatch):
    harness.log_in()
    assert canvas_mirror.run_once(ENV, "startup").state == "ok"
    assert canvas_mirror.kick(ENV, "timer") == {"started": False, "reason": "fresh"}
    assert canvas_mirror.kick(ENV, "startup") == {"started": False, "reason": "fresh"}
    assert len(_mirror_calls(harness)) == 1
    assert canvas_mirror.kick(ENV, "manual", force=True)["started"] is True
    assert _wait_for(lambda: len(_mirror_calls(harness)) == 2
                     and canvas_mirror.status(ENV)["state"] == "ok")
    # Older than the interval: due again.
    monkeypatch.setattr(canvas_mirror, "INTERVAL_S", 0.0)
    assert canvas_mirror.kick(ENV, "timer")["started"] is True
    assert _wait_for(lambda: len(_mirror_calls(harness)) == 3)


def test_the_run_route_refreshes_now(harness):
    harness.log_in()
    canvas_mirror.run_once(ENV)
    assert harness.client.post("/api/canvases/mirror/run",
                               json={"env": ENV}).status_code == 403
    res = harness.client.post("/api/canvases/mirror/run", json={"env": ENV}, headers=GUARD)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["started"] is True and body["reason"] is None
    assert body["status"]["env"] == ENV
    assert _wait_for(lambda: len(_mirror_calls(harness)) == 2)
    assert harness.client.post("/api/canvases/mirror/run", json={"env": "nope"},
                               headers=GUARD).status_code == 400
    assert harness.client.get("/api/canvases/mirror/status?env=nope").status_code == 400
    everything = harness.client.get("/api/canvases/mirror/status").json()
    assert set(everything["envs"]) == set(fusedcli_mod.WORKBENCH_ENV_CHOICES)
    # Signed out: refused, and said why.
    harness.creds.unlink()
    res = harness.client.post("/api/canvases/mirror/run", json={"env": ENV}, headers=GUARD)
    assert res.json()["started"] is False and res.json()["reason"] == "signed_out"


def test_the_canvases_status_carries_the_root_and_a_compact_mirror(harness, monkeypatch):
    harness.log_in()
    harness.set_scenario({"mirror_result": _result(counts={"pulled": 2, "unchanged": 1})})
    canvas_mirror.run_once(ENV)
    body = harness.client.get("/api/canvases/status").json()
    for env in (ENV, OTHER_ENV):
        assert body["envs"][env]["mirror_root"] == str(harness.base / env)
        assert body["envs"][env]["canvases_dir"] == str(harness.base / env / "user")
    mirror = body["envs"][ENV]["mirror"]
    assert mirror["state"] == "ok" and mirror["mirrored"] == 3 and mirror["eligible"] is True
    assert body["envs"][OTHER_ENV]["mirror"]["state"] == "idle"
    # The status route never fails for the mirror's sake.
    monkeypatch.setattr(canvas_mirror, "summary", None)
    assert harness.client.get("/api/canvases/status").json()["envs"][ENV]["mirror"] is None


# -- nothing spawns unless started, nothing outlives the app --------------


def test_nothing_runs_the_command_unless_something_starts_it(harness):
    """A bare create_app() (every test, and a lean `open` process) arms no
    timer; the Canvases routes and a completed sign-in spawn no mirror."""
    harness.log_in()
    assert canvas_mirror.started() is False
    harness.client.get("/api/canvases/status")
    harness.client.get("/api/canvases/list?env=unstable&fresh=1", headers=GUARD)
    harness.client.post("/api/canvases/clone", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    harness.client.post("/api/canvases/sync/start", json={"env": ENV, "name": "alpha"},
                        headers=GUARD)
    harness.client.post("/api/canvases/delete", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    canvas_mirror.signed_in(str(harness.creds))
    assert canvas_mirror.kick_if_started(ENV, "login") == {
        "started": False, "reason": "not_started"}
    time.sleep(0.2)
    assert _mirror_calls(harness) == []
    assert not (harness.root / ".sync" / ".mirror.json").exists()


def test_a_sign_in_fetches_the_reference_canvases_once_the_runner_is_started(
        harness, monkeypatch):
    harness.log_in()
    monkeypatch.setattr(canvas_mirror, "STARTUP_DELAY_S", 3600.0)
    canvas_mirror.start()
    assert canvas_mirror.started() is True
    time.sleep(0.1)
    assert _mirror_calls(harness) == []                 # the timer has not fired
    # A sign-in into another store is not this environment's.
    canvas_mirror.signed_in(str(harness.creds) + ".other")
    assert _mirror_calls(harness) == []
    canvas_mirror.signed_in(str(harness.creds))
    assert _wait_for(lambda: canvas_mirror.status(ENV)["state"] == "ok")
    # prod and unstable read one store, but only the one in use is mirrored.
    assert _mirror_calls(harness) == [[*MIRROR_ARGV, str(harness.root)]]


def test_the_login_watcher_kicks_only_for_a_sign_in_that_landed(harness, monkeypatch):
    seen = []
    monkeypatch.setattr(canvas_mirror, "signed_in", seen.append)

    def login():
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        return canvases_mod._ActiveLogin(proc=proc, started_at=time.time(),
                                         store=str(harness.creds))

    canvases_mod._watch_login(login())                  # cancelled: no credentials
    assert seen == []
    harness.log_in()
    canvases_mod._watch_login(login())
    assert seen == [str(harness.creds)]


def test_the_timer_runs_the_environments_in_use_after_its_delay(harness, monkeypatch):
    harness.log_in()
    monkeypatch.setattr(canvas_mirror, "STARTUP_DELAY_S", 0.05)
    canvas_mirror.start()
    canvas_mirror.start()                               # idempotent: one timer
    assert _wait_for(lambda: canvas_mirror.status(ENV)["state"] == "ok")
    status = canvas_mirror.status(ENV)
    assert status["trigger"] == "startup"
    time.sleep(0.2)
    assert _mirror_calls(harness) == [[*MIRROR_ARGV, str(harness.root)]]
    canvas_mirror.shutdown(latch=False)
    assert canvas_mirror.started() is False


def test_the_timer_stops_for_a_cli_without_the_command(harness, monkeypatch):
    harness.log_in()
    harness.set_scenario({"mirror_unsupported": True})
    monkeypatch.setattr(canvas_mirror, "STARTUP_DELAY_S", 0.01)
    monkeypatch.setattr(canvas_mirror, "INTERVAL_S", 0.05)
    canvas_mirror.start()
    assert _wait_for(lambda: canvas_mirror.status(ENV)["state"] == "unsupported")
    stop = canvas_mirror._timer_stop
    time.sleep(0.5)                                      # many intervals
    assert len(_mirror_calls(harness)) == 1
    import threading
    assert not [t for t in threading.enumerate() if t.name == "canvas-mirror-timer"]
    assert stop is not None and not stop.is_set()       # it ended itself


def test_shutdown_kills_the_child_and_removes_its_lock(harness, tmp_path, monkeypatch):
    harness.log_in()
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 0.5)
    started = _slow(harness, tmp_path)
    assert canvas_mirror.kick(ENV, "manual", force=True)["started"] is True
    pid = _wait_started(started)
    assert _alive(pid) and (harness.root / ".mirror.lock").exists()
    canvas_mirror.shutdown()
    assert not _alive(pid)
    assert not (harness.root / ".mirror.lock").exists()
    status = canvas_mirror.status(ENV)
    assert status["state"] == "idle"                    # a stopped run is not a result
    # The quit rung latched: nothing starts after it.
    assert canvas_mirror.kick(ENV, "manual", force=True) == {
        "started": False, "reason": "shutting_down"}
    assert canvas_mirror.run_once(ENV).state == "idle"
    assert len(_mirror_calls(harness)) == 1


def test_the_quit_latch_alone_refuses_new_runs(harness):
    harness.log_in()
    canvas_mirror.refuse_new_runs()
    assert canvas_mirror.kick(ENV, "manual", force=True)["reason"] == "shutting_down"
    assert _mirror_calls(harness) == []


def test_the_servers_lifespan_arms_the_timer_and_stops_it(harness, tmp_path, monkeypatch):
    """`start()` is the startup hook's, and the shutdown hook stops a child a
    request started — without latching, so a later app can start again."""
    harness.log_in()
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 0.5)
    started = _slow(harness, tmp_path)
    from test_app_lifespan import EXPECTED_SHUTDOWN, EXPECTED_STARTUP

    app = create_app(start_dir=str(tmp_path))
    handlers = {f.__name__: f for f in
                [*app.state.startup_handlers, *app.state.shutdown_handlers]}
    assert "_startup_canvas_mirror" in EXPECTED_STARTUP
    assert "_shutdown_canvas_mirror" in EXPECTED_SHUTDOWN
    import asyncio

    asyncio.run(handlers["_startup_canvas_mirror"]())
    assert canvas_mirror.started() is True
    time.sleep(0.1)
    assert _mirror_calls(harness) == []                 # armed, 30 s away
    assert TestClient(app).post("/api/canvases/mirror/run", json={"env": ENV},
                                headers=GUARD).json()["started"] is True
    pid = _wait_started(started)
    asyncio.run(handlers["_shutdown_canvas_mirror"]())
    assert not _alive(pid) and canvas_mirror.started() is False
    harness.set_scenario({})
    assert canvas_mirror.kick(ENV, "manual", force=True)["started"] is True


def test_logout_stops_a_run_on_that_sign_in(harness, tmp_path, monkeypatch):
    harness.log_in()
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 0.5)
    started = _slow(harness, tmp_path)
    canvas_mirror.kick(ENV, "manual", force=True)
    pid = _wait_started(started)
    res = harness.client.post("/api/canvases/logout", headers=GUARD)
    assert res.status_code == 200, res.text
    assert not _alive(pid)
    time.sleep(0.2)
    assert len(_mirror_calls(harness)) == 1             # stopped, not started again
    assert (harness.root / "user").is_dir()             # the folders stay


# -- holds -----------------------------------------------------------------


def test_hold_stops_the_run_and_starts_one_again_afterwards(harness, tmp_path, monkeypatch):
    harness.log_in()
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 0.5)
    started = _slow(harness, tmp_path)
    canvas_mirror.kick(ENV, "timer", force=True)
    pid = _wait_started(started)
    with canvas_mirror.hold(ENV):
        # Entered only once the child is gone, with its lock.
        assert not _alive(pid)
        assert not (harness.root / ".mirror.lock").exists()
        status = canvas_mirror.status(ENV)
        assert status["state"] == "idle" and status["held"] is True
        # Nothing starts inside, by any door.
        assert canvas_mirror.kick(ENV, "manual", force=True) == {
            "started": False, "reason": "held"}
        assert canvas_mirror.run_once(ENV).state == "idle"
        with canvas_mirror.hold(ENV):                   # re-entrant
            pass
        assert canvas_mirror.kick(ENV, "manual", force=True)["reason"] == "held"
        # Another environment is not held.
        assert canvas_mirror.status(OTHER_ENV)["held"] is False
        assert len(_mirror_calls(harness)) == 1
        harness.set_scenario({"mirror_result": _result(counts={"unchanged": 3})})
    # The stopped run is made up for.
    assert _wait_for(lambda: canvas_mirror.status(ENV)["state"] == "ok")
    status = canvas_mirror.status(ENV)
    assert status["trigger"] == "hold" and status["held"] is False
    assert len(_mirror_calls(harness)) == 2


def test_a_hold_with_nothing_running_starts_nothing(harness):
    harness.log_in()
    with canvas_mirror.hold(ENV):
        pass
    time.sleep(0.2)
    assert _mirror_calls(harness) == []


def test_a_hold_releases_when_its_block_raises(harness):
    harness.log_in()
    with pytest.raises(ZeroDivisionError):
        with canvas_mirror.hold(ENV):
            1 / 0
    assert canvas_mirror.status(ENV)["held"] is False
    assert canvas_mirror.run_once(ENV).state == "ok"


def test_no_run_starts_while_a_moved_clone_named_like_a_scope_is_owed_its_carry(
        harness, monkeypatch):
    """The run would create `<env>/team`; the carry owed FROM that path
    replays only while it does not exist, and would be dropped — the
    clone's chat history with it."""
    harness.log_in()
    monkeypatch.setattr(canvas_layout, "owed_scope_names", lambda env: ["team"])
    status = canvas_mirror.run_once(ENV)
    assert status.state == "failed" and "'team'" in status.error
    assert _mirror_calls(harness) == [] and not (harness.root / "team").exists()
    monkeypatch.setattr(canvas_layout, "owed_scope_names", lambda env: [])
    assert canvas_mirror.run_once(ENV).state == "ok"


# -- a clone still where the reference canvases go ------------------------------


@pytest.mark.parametrize("name", ["team", "public"])
@pytest.mark.parametrize("why", ["deferred", "conflict"])
def test_no_run_while_a_clone_named_like_a_scope_folder_is_still_at_its_old_path(
        harness, monkeypatch, name, why):
    """A clone of a canvas literally named `team` still at `<env>/team` (a
    session is in it, or something else sits at `user/team`): the command
    would write every team canvas INTO that clone, and its sync would
    publish them as the user's edits."""
    from fused_render import claude_session_move

    harness.log_in()
    clone = harness.root / name
    clone.mkdir(parents=True)
    (clone / "canvas.toml").write_text("")
    if why == "deferred":
        monkeypatch.setattr(claude_session_move, "held_sessions", lambda root: ["sid-live"])
    else:
        other = harness.clones / name
        other.mkdir(parents=True)
        (other / "canvas.toml").write_text("")
    status = canvas_mirror.run_once(ENV)
    assert status.state == "failed" and repr(name) in status.error
    assert str(clone) in status.error and "user/" in status.error
    assert _mirror_calls(harness) == []
    assert sorted(os.listdir(clone)) == ["canvas.toml"]
    key = "deferred" if why == "deferred" else "conflicts"
    assert name in canvas_layout.pending(ENV)[key]
    # Once it has moved, the run goes ahead.
    if why == "deferred":
        monkeypatch.setattr(claude_session_move, "held_sessions", lambda root: [])
        canvas_layout.reset()
        assert canvas_mirror.run_once(ENV).state == "ok"
        assert (harness.clones / name / "canvas.toml").is_file()


# -- a CLI that is a launcher --------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_stopping_a_run_reaches_the_command_behind_a_launcher(harness, tmp_path, monkeypatch):
    """FUSED_RENDER_FUSED_BIN may be a compound command (`uv run fused`): the
    child is a launcher and the command is ITS child. A hold must stop the
    command, not just the launcher, and clear the lock the command held
    (which names the command's pid, not the launcher's)."""
    import stat

    harness.log_in()
    launcher = tmp_path / "launcher.sh"
    launcher.write_text(f'#!/bin/sh\n"{sys.executable}" "{tmp_path / "fused_stub.py"}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FUSED_RENDER_FUSED_BIN", f'"{launcher}"')
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 0.3)
    started = _slow(harness, tmp_path, mirror_ignore_signals=["INT", "TERM"])
    assert canvas_mirror.kick(ENV, "manual", force=True)["started"] is True
    command = _wait_started(started)
    assert json.loads((harness.root / ".mirror.lock").read_text())["pid"] == command
    try:
        with canvas_mirror.hold(ENV):
            assert not _alive(command)
            assert not (harness.root / ".mirror.lock").exists()
            harness.set_scenario({})
        assert _wait_for(lambda: canvas_mirror.status(ENV)["state"] == "ok")   # not "busy"
    finally:
        if _alive(command):
            os.kill(command, 9)


def test_a_dead_holders_lock_is_cleared_and_a_live_ones_is_not(harness):
    harness.root.mkdir(parents=True)
    lock = harness.root / ".mirror.lock"
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    lock.write_text(json.dumps({"pid": gone.pid, "started_at": "t", "refreshed_at": "t"}))
    canvas_mirror._clear_own_lock(ENV, 1)                # not our child's pid, but dead
    assert not lock.exists()
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at": "t", "refreshed_at": "t"}))
    canvas_mirror._clear_own_lock(ENV, 1)                # alive: someone's run
    assert lock.exists()
    lock.write_text(json.dumps({"pid": "12", "started_at": "t"}))
    canvas_mirror._clear_own_lock(ENV, 12)
    assert lock.exists()


# -- the stop ladder -----------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_an_interrupt_comes_first_so_the_command_can_unwind(harness, tmp_path, monkeypatch):
    """The command releases its lock and cleans its work folders on an
    interrupt. A terminate first would never give it the chance."""
    harness.log_in()
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 2.0)
    interrupted = tmp_path / "interrupted"
    started = _slow(harness, tmp_path, mirror_interrupted_file=str(interrupted))
    canvas_mirror.kick(ENV, "manual", force=True)
    pid = _wait_started(started)
    began = time.monotonic()
    with canvas_mirror.hold(ENV):
        assert not _alive(pid)
        assert interrupted.read_text() == "interrupted"
        assert time.monotonic() - began < 2.0            # no escalation was needed
        harness.set_scenario({})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("ignored", [["INT"], ["INT", "TERM"]])
def test_a_command_that_does_not_stop_is_terminated_then_killed(
        harness, tmp_path, monkeypatch, ignored):
    """With downloads in flight the real command can sit through the
    interrupt's grace. Each step that is ignored is followed by the next."""
    harness.log_in()
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 0.4)
    started = _slow(harness, tmp_path, mirror_ignore_signals=ignored)
    canvas_mirror.kick(ENV, "manual", force=True)
    pid = _wait_started(started)
    began = time.monotonic()
    with canvas_mirror.hold(ENV):
        waited = time.monotonic() - began
        assert not _alive(pid)
        assert not (harness.root / ".mirror.lock").exists()
        assert waited >= 0.4 * len(ignored) - 0.1        # it took the escalation
        harness.set_scenario({})


def test_a_hold_that_arrives_before_the_command_is_spawned_prevents_the_spawn(
        harness, monkeypatch):
    """The run is still moving old clones (no child yet) when a clone, rename
    or delete takes the hold. The child must never be started after that:
    it would run THROUGH the hold."""
    import threading

    harness.log_in()
    in_layout, release = threading.Event(), threading.Event()
    real = canvas_layout.ensure_layout

    def slow_layout(env, **kw):
        in_layout.set()
        release.wait(10)
        return real(env, **kw)

    monkeypatch.setattr(canvas_layout, "ensure_layout", slow_layout)
    monkeypatch.setattr(canvas_mirror, "HOLD_WAIT_S", 0.3)
    assert canvas_mirror.kick(ENV, "timer", force=True)["started"] is True
    assert in_layout.wait(5)
    with canvas_mirror.hold(ENV):                        # gives up waiting after 0.3 s
        release.set()                                    # ...and only then the run goes on
        assert _wait_for(lambda: ENV not in canvas_mirror._runs)
        assert _mirror_calls(harness) == []
    # The run that was stopped is made up for after the hold.
    assert _wait_for(lambda: len(_mirror_calls(harness)) == 1)


# -- shutdown ------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_shutdown_signals_every_child_before_waiting_and_keeps_to_its_budget(
        harness, tmp_path, monkeypatch):
    """Two environments running, neither stopping on an interrupt or a
    terminate: the quit rung has a few seconds for everything. Both are dead
    and both locks gone within the budget — not one ladder per child."""
    harness.log_in()
    canvas_mirror.note_listed(OTHER_ENV)
    monkeypatch.setattr(canvas_mirror, "STOP_GRACE_S", 5.0)
    harness.set_scenario({"mirror_delay": 60, "mirror_lock": True,
                          "mirror_ignore_signals": ["INT", "TERM"]})
    for env in (ENV, OTHER_ENV):
        assert canvas_mirror.kick(env, "manual", force=True)["started"] is True
    locks = [harness.base / env / ".mirror.lock" for env in (ENV, OTHER_ENV)]
    assert _wait_for(lambda: all(lock.exists() for lock in locks))
    pids = [json.loads(lock.read_text())["pid"] for lock in locks]
    began = time.monotonic()
    canvas_mirror.shutdown(budget_s=1.0)
    assert time.monotonic() - began < 3.0
    assert not any(_alive(pid) for pid in pids)
    assert not any(lock.exists() for lock in locks)


def test_a_timer_tick_caught_by_shutdown_starts_nothing(harness, monkeypatch):
    """The lifespan's shutdown does not latch (a later app may start again).
    A tick that was already past its own stop check must still not register
    a run after it: the check is made again under the lock shutdown sets
    the stop flag under."""
    import threading

    harness.log_in()
    stop = threading.Event()
    assert canvas_mirror.kick(ENV, "timer", guard=stop)["started"] is True
    assert _wait_for(lambda: canvas_mirror.status(ENV)["state"] == "ok")
    monkeypatch.setattr(canvas_mirror, "INTERVAL_S", 0.0)
    stop.set()
    assert canvas_mirror.kick(ENV, "timer", guard=stop) == {
        "started": False, "reason": "shutting_down"}
    assert len(_mirror_calls(harness)) == 1
    # shutdown sets the timer's own flag, and without a latch a later kick works.
    monkeypatch.setattr(canvas_mirror, "STARTUP_DELAY_S", 3600.0)
    canvas_mirror.start()
    timer_stop = canvas_mirror._timer_stop
    canvas_mirror.shutdown(latch=False)
    assert timer_stop.is_set()
    assert canvas_mirror.kick(ENV, "timer", guard=timer_stop)["reason"] == "shutting_down"
    assert canvas_mirror.kick(ENV, "manual", force=True)["started"] is True


# -- smaller rules -------------------------------------------------------------


def test_a_sign_in_refreshes_whatever_the_age_of_the_last_result(harness, monkeypatch):
    """The last result may be the account that just signed out's."""
    harness.log_in()
    assert canvas_mirror.run_once(ENV).state == "ok"
    monkeypatch.setattr(canvas_mirror, "STARTUP_DELAY_S", 3600.0)
    canvas_mirror.start()
    assert canvas_mirror.kick(ENV, "timer") == {"started": False, "reason": "fresh"}
    canvas_mirror.signed_in(str(harness.creds))
    assert _wait_for(lambda: len(_mirror_calls(harness)) == 2)
    # Still only the environments in use (prod and unstable share the store).
    time.sleep(0.2)
    assert _mirror_calls(harness) == [[*MIRROR_ARGV, str(harness.root)]] * 2


def test_a_cli_without_the_command_leaves_no_folders_and_no_status_file(harness):
    harness.log_in()
    harness.set_scenario({"mirror_unsupported": True})
    assert canvas_mirror.run_once(ENV).state == "unsupported"
    assert not harness.root.exists()
    assert canvas_mirror.status(ENV)["state"] == "unsupported"
    # Folders that were there before the run are not removed with the rest.
    canvas_mirror.reset()
    (harness.root / "user" / "alpha").mkdir(parents=True)
    assert canvas_mirror.run_once(ENV).state == "unsupported"
    assert sorted(p.name for p in harness.root.iterdir()) == ["user"]


def test_the_command_swapping_a_folder_is_not_a_listing_error(harness, tmp_path):
    """The real command replaces a copy by renaming the old one aside and the
    new one in: for a moment `user/<name>` is not there. Every reader here
    takes that as "no such folder", never as an error."""
    harness.log_in()
    folder = harness.clones / "alpha"
    folder.mkdir(parents=True)
    (folder / "canvas.toml").write_text("")
    (folder / "a.py").write_text("old\n")
    (folder / canvas_marker.MARKER_NAME).write_text(json.dumps(
        {"version": 1, "sync": "mirror", "scope": "user", "id": "c", "name": "alpha"}))
    started = tmp_path / "started"
    harness.set_scenario({"mirror_started_file": str(started), "mirror_swap_gap": 1.0,
                          "mirror_swap": [{"path": "user/alpha", "files": {"a.py": "new\n"}}]})
    canvas_mirror.kick(ENV, "manual", force=True)
    _wait_started(started)
    assert _wait_for(lambda: not folder.exists())
    assert canvases_mod._cloned_names(ENV) == set()
    assert canvases_mod._is_live_clone(ENV, "alpha") is False
    assert canvas_marker.folder_state(str(folder)) == "missing"
    assert harness.client.get("/api/canvases/status").status_code == 200
    assert harness.client.get("/api/canvases/list?env=unstable", headers=GUARD).status_code == 200
    assert _wait_for(lambda: canvas_mirror.status(ENV)["state"] == "ok")
    assert (folder / "a.py").read_text() == "new\n"
    assert [p.name for p in harness.clones.iterdir()] == ["alpha"]
