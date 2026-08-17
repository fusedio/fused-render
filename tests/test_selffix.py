"""Self-fix — a Claude session on the app's own installation (SPEC §42, D308).

What is actually at stake here is a promise the UI makes on the app's behalf:
*this copy has been changed, and reinstalling gets you a clean one.* Both halves
can be broken silently. A mark that fails to appear leaves a user running
somebody's patch with a confident version number on it; a mark that fails to
CLEAR turns a permanent badge onto an installation that is, by then, pristine.

So the tests below are mostly about the three ways an installation stops being
modified — an upgrade, a same-version reinstall, and the user saying so — and
about the one thing the digest must not notice: the app merely being run.
"""
import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

from fused_render import __version__, selffix
from fused_render.server import create_app
from fused_render.server.routers import selffix as selffix_routes


@pytest.fixture()
def install(tmp_path, monkeypatch):
    """A fake installation. Every location in the module resolves through
    `install_root`, so redirecting that one function moves the whole feature
    into a tmp dir — no test may write into the developer's real package."""
    root = tmp_path / "site-packages" / "fused_render"
    (root / "server").mkdir(parents=True)
    (root / "__init__.py").write_text('__version__ = "9.9.9"\n')
    (root / "jobs.py").write_text("RUNNING = 'running'\n")
    (root / "server" / "app.py").write_text("def create_app(): ...\n")
    monkeypatch.setattr(selffix, "install_root", lambda: str(root))
    return root


@pytest.fixture(autouse=True)
def free_slot():
    """The one-session-at-a-time slot is module-global — release it per test."""
    selffix_routes._release_active()
    yield
    selffix_routes._release_active()


@pytest.fixture()
def client(tmp_path, install):
    return TestClient(create_app(start_dir=str(tmp_path)))


def post(client, url, body=None):
    return client.post(url, json=body if body is not None else {},
                       headers={"X-Fused": "1"})


# The digest the current test's session starts from. `settle` measures against
# THIS, not against the release — see its docstring — so a test that stamps has
# to open a session first, exactly as the start route does.
BEFORE = [""]


def _pristine():
    """Begin a session on the install as it currently stands."""
    _, BEFORE[0] = selffix.begin_session()
    return BEFORE[0]


# ---------------------------------------------------------------- the digest


def test_running_the_app_is_not_a_modification(install):
    """Byte-caches are written by the act of importing. If they counted, every
    installation would be 'modified' the first time it started."""
    before = selffix.tree_digest()
    cache = install / "__pycache__"
    cache.mkdir()
    (cache / "jobs.cpython-312.pyc").write_bytes(b"\x00\x01")
    (install / "server" / "app.pyc").write_bytes(b"\x00")
    assert selffix.tree_digest() == before


def test_the_state_dir_does_not_modify_the_installation_it_describes(install):
    """The incident file a fix session reads lives inside the install tree —
    so writing it must not be a change the same run then reports."""
    before = selffix.tree_digest()
    selffix.record_incident({"title": "boom", "message": "Traceback…"})
    assert selffix.tree_digest() == before


def test_an_edit_moves_the_digest(install):
    before = selffix.tree_digest()
    (install / "jobs.py").write_text("RUNNING = 'running'  # patched\n")
    assert selffix.tree_digest() != before


def test_a_rename_is_as_visible_as_an_edit(install):
    before = selffix.tree_digest()
    os.rename(install / "jobs.py", install / "jobs2.py")
    assert selffix.tree_digest() != before


# ----------------------------------------------------------------- the mark


def test_settle_marks_only_when_the_tree_actually_moved(install):
    _pristine()
    assert selffix.settle(before=BEFORE[0], run_id="r1") is False
    assert selffix.status() is None

    (install / "jobs.py").write_text("RUNNING = 'running'  # patched\n")
    assert selffix.settle(before=BEFORE[0], run_id="r1", report=str(install / ".x" / "r.md")) is True
    state = selffix.status()
    assert state is not None
    assert state["modified"] is True
    assert state["version"] == __version__
    assert len(state["fixes"]) == 1


def test_repeated_stamps_from_one_session_stay_one_fix(install):
    """The watcher re-checks every few ticks so the badge appears while the user
    watches — appending per check would show one conversation as a column."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    for _ in range(4):
        selffix.settle(before=BEFORE[0], run_id="r1", title="download failed")
    state = selffix.status()
    assert [f["run_id"] for f in state["fixes"]] == ["r1"]

    selffix.settle(before=BEFORE[0], run_id="r2")
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1", "r2"]


def test_report_paths_survive_the_installation_being_moved(install, tmp_path,
                                                           monkeypatch):
    """Stored relative to the state dir, absolutised on read — a bundle dragged
    from the DMG to /Applications must not lose its own report."""
    _pristine()
    incident, report = selffix.record_incident({"title": "boom"})
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1", report=report, incident=incident)

    moved = tmp_path / "Applications" / "fused_render"
    moved.parent.mkdir(parents=True, exist_ok=True)
    os.rename(install, moved)
    monkeypatch.setattr(selffix, "install_root", lambda: str(moved))

    latest = selffix.status()["latest_report"]
    assert latest is not None
    assert latest.startswith(str(moved))
    assert os.path.exists(latest)


def test_a_session_that_changed_nothing_is_not_recorded_as_a_fix(install):
    """`settle` measures against the tree as THIS session found it, not against
    the release. On an install an earlier session already changed, the two are
    different before the new session has done anything — so measuring against
    the release would record a do-nothing session as a fix, and make its own
    empty report the one the badge points at."""
    _pristine()
    (install / "jobs.py").write_text("patched by the first session\n")
    selffix.settle(before=BEFORE[0], run_id="r1", title="first")
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1"]

    # A second session opens on the ALREADY-MODIFIED tree and edits nothing.
    _, before2 = selffix.begin_session()
    assert selffix.settle(before=before2, run_id="r2", title="second") is False
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1"]


def test_a_no_op_session_does_not_re_light_a_dismissed_badge(install):
    """Dismissing is a decision the user made about this machine. A later
    session that changed nothing must not overturn it."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.clear() is True

    _, before2 = selffix.begin_session()
    assert selffix.settle(before=before2, run_id="r2") is False
    assert selffix.status() is None


def test_a_second_session_that_does_change_something_is_recorded(install):
    """The other half — the guard above must not swallow a real second fix."""
    _pristine()
    (install / "jobs.py").write_text("patched once\n")
    selffix.settle(before=BEFORE[0], run_id="r1")

    _, before2 = selffix.begin_session()
    (install / "server" / "app.py").write_text("patched twice\n")
    assert selffix.settle(before=before2, run_id="r2") is True
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1", "r2"]


def test_the_pristine_baseline_survives_a_session_on_a_modified_tree(install):
    """`begin_session` must not re-baseline against an already-patched tree, or
    `reconcile` would lose its only picture of what the release shipped."""
    pristine_file = (install / "jobs.py").read_text()
    baseline, _ = selffix.begin_session()
    (install / "jobs.py").write_text("patched\n")

    baseline2, before2 = selffix.begin_session()
    assert baseline2 == baseline          # still the release
    assert before2 != baseline            # ...and this session knows it differs


# ------------------------------------------------- ...and the three ways out


def test_an_upgrade_clears_the_mark_on_sight(install):
    """`pip uninstall` only removes what its RECORD lists, so a marker can
    outlive the install it described. The version stamp is what catches that,
    and it has to be caught on the READ path — the badge must be gone the
    moment the new version serves a request, not after the next restart."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is not None

    marker = json.loads(open(selffix.marker_path()).read())
    marker["version"] = "0.0.1"
    with open(selffix.marker_path(), "w") as f:
        json.dump(marker, f)

    assert selffix.status() is None
    # ...and it is gone, not merely hidden: a later downgrade must not find it.
    assert not os.path.exists(selffix.marker_path())


def test_a_same_version_reinstall_clears_the_mark(install):
    """The case the version stamp cannot see — and the obvious thing a user
    does when the app misbehaves. `reconcile` is the only thing that catches
    it, which is why it hashes the tree at every start where a marker exists."""
    pristine = (install / "jobs.py").read_text()
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is not None

    (install / "jobs.py").write_text(pristine)  # the reinstall
    selffix.reconcile()
    assert selffix.status() is None


def test_reconcile_leaves_a_still_modified_install_alone(install):
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    selffix.reconcile()
    assert selffix.status() is not None


def test_reconcile_refreshes_a_digest_that_drifted_further(install):
    """A resumed conversation can change more files after the watcher gave up.
    The record has to follow, or the 'restored' test above compares against a
    tree that no longer exists anywhere."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    first = json.loads(open(selffix.marker_path()).read())["digest"]

    (install / "jobs.py").write_text("patched twice\n")
    selffix.reconcile()
    assert json.loads(open(selffix.marker_path()).read())["digest"] != first
    assert selffix.status() is not None


def test_clear_forgets_the_mark_but_keeps_the_report(install):
    """The user's own override. The badge is a claim about this machine and the
    person at it may have settled it by hand — but the record of what was
    changed is not theirs to lose by dismissing a badge."""
    _pristine()
    incident, report = selffix.record_incident({"title": "boom"})
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1", report=report, incident=incident)

    assert selffix.clear() is True
    assert selffix.status() is None
    assert os.path.exists(report)
    assert selffix.clear() is False


def test_dismissing_mid_session_is_not_undone_by_the_next_stamp(install):
    """The likeliest moment to dismiss is while watching the session that raised
    the badge — and the watcher re-stamps every few ticks and once more when the
    turn ends. Without remembering WHAT was dismissed, the user's click was
    undone seconds later by the next stamp of the very same change."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is not None

    assert selffix.clear() is True
    # ...the watcher keeps going, and the tree still differs from `before`.
    selffix.settle(before=BEFORE[0], run_id="r1")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is None


def test_a_dismissal_covers_only_the_state_it_was_made_for(install):
    """"I have seen this and do not want a badge for it" — not "never badge me
    again". A session that goes on to change something ELSE moves the digest
    past the dismissed one, and the badge legitimately comes back."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    selffix.clear()
    assert selffix.status() is None

    (install / "server" / "app.py").write_text("and this too\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert selffix.status() is not None
    # ...and the spent dismissal is retired, not left to silence a later one.
    assert not os.path.exists(selffix.dismissed_path())


def test_a_dismissal_expires_with_the_version_it_was_made_on(install, monkeypatch):
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    selffix.clear()

    stale = json.loads(open(selffix.dismissed_path()).read())
    stale["version"] = "0.0.1"
    with open(selffix.dismissed_path(), "w") as f:
        json.dump(stale, f)
    assert selffix.dismissed_digest() == ""


def test_reconcile_does_not_resurrect_a_marker_cleared_while_it_walked(install,
                                                                      monkeypatch):
    """`reconcile` hashes the whole tree before it writes, which is long enough
    for the user to dismiss the badge. Writing back the object read BEFORE the
    walk would silently undo that."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")

    real_digest = selffix.tree_digest

    def digest_then_dismiss(*args, **kwargs):
        # The user clicks Dismiss while the walk is in flight.
        out = real_digest(*args, **kwargs)
        selffix.clear()
        monkeypatch.setattr(selffix, "tree_digest", real_digest)
        return out

    monkeypatch.setattr(selffix, "tree_digest", digest_then_dismiss)
    selffix.reconcile()
    assert selffix.status() is None


def test_reconcile_does_not_drop_a_fix_recorded_while_it_walked(install,
                                                               monkeypatch):
    """The other side of the same race: a watcher's stamp landing mid-walk must
    not be replaced by the pre-walk snapshot, losing its report."""
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")

    real_digest = selffix.tree_digest

    def digest_then_stamp(*args, **kwargs):
        out = real_digest(*args, **kwargs)
        monkeypatch.setattr(selffix, "tree_digest", real_digest)
        # A second session's watcher records its own fix while we walk.
        selffix.mark_modified(run_id="r2", title="second", digest="drifted")
        return out

    monkeypatch.setattr(selffix, "tree_digest", digest_then_stamp)
    selffix.reconcile()
    assert [f["run_id"] for f in selffix.status()["fixes"]] == ["r1", "r2"]


def test_a_new_version_starts_a_fresh_baseline(install, monkeypatch):
    """An upgrade legitimately replaced the tree the old baseline described;
    trusting it would report every upgrade as a modification."""
    first, _ = selffix.begin_session()
    (install / "jobs.py").write_text("the next release\n")
    # ensure_baseline compares against the module's own __version__ binding.
    stale = json.loads(open(selffix.baseline_path()).read())
    stale["version"] = "0.0.1"
    with open(selffix.baseline_path(), "w") as f:
        json.dump(stale, f)
    second, _ = selffix.begin_session()
    assert second != first


# ------------------------------------------------------ incidents & reports


def test_the_report_exists_before_the_session_writes_a_word(install):
    """A version chip that promises a report must always have a file to open —
    and what a developer most needs is known now, not after a model has
    summarised it."""
    incident, report = selffix.record_incident({
        "title": "FLUX.2-klein-4B", "message": "Traceback: OSError(28)",
        "page": "/models.html", "job_id": "sys:ai-model:x", "state": "error",
    })
    assert os.path.exists(incident) and os.path.exists(report)
    text = open(report, encoding="utf-8").read()
    assert "Not written yet" in text
    assert "Traceback: OSError(28)" in text
    assert "FLUX.2-klein-4B" in text
    assert __version__ in text


# ------------------------------------- ...when nothing actually went wrong


def test_a_described_problem_needs_no_error_and_says_so(install):
    """The Preferences way in (SF-14). A great deal of what is wrong with an app
    never raises anything, and a session told to trace a failure that does not
    exist will guess — which is the one thing a patch to somebody's install must
    not be."""
    incident, report = selffix.record_incident({
        "note": "Opening a big folder takes ten seconds and the window freezes.",
        "source": "preferences",
    })
    text = open(incident, encoding="utf-8").read()
    # The user's own words outrank the machinery: with no traceback the
    # description is the whole of what is known, so it leads the body rather
    # than sitting under the surface that sent it. (The five-line preamble —
    # when, version, platform — still comes first; that is context, not burial.)
    assert text.index("What the user asked for") < text.index("What the app was doing")
    assert "takes ten seconds" in text
    assert "No error was raised" in text
    assert "Not written yet" in open(report, encoding="utf-8").read()


def test_a_described_problem_gets_the_reproduce_first_brief(install):
    described = selffix.fix_prompt("/i.md", "/r.md", reported_error=False)
    assert "NOTHING CRASHED" in described
    assert "REPRODUCE WHAT THEY DESCRIBE" in described
    # ...and the failure brief keeps its own opening.
    failed = selffix.fix_prompt("/i.md", "/r.md", reported_error=True)
    assert "NOTHING CRASHED" not in failed
    assert "trace the failure" in failed
    # Both still fence the agent into the install.
    for prompt in (described, failed):
        assert "Only edit files under" in prompt


def test_the_incident_carries_the_app_log_and_names_the_call_log(install, tmp_path,
                                                                 monkeypatch):
    """With no traceback the log is frequently the only evidence there is, and a
    path the session has to go and find is a step it may not take."""
    log = tmp_path / "fused-render-1.log"
    log.write_text("ERROR listing /big took 9.8s\n" * 3)
    monkeypatch.setenv("FUSED_RENDER_LOG_DIR", str(tmp_path))
    monkeypatch.setattr("fused_render.logs.log_path", lambda: str(log))

    incident, _ = selffix.record_incident({"note": "slow"})
    text = open(incident, encoding="utf-8").read()
    assert "Recent app log" in text
    assert "listing /big took 9.8s" in text
    assert "Call log" in text


def test_the_log_tail_is_bounded(install, tmp_path, monkeypatch):
    """An incident file is meant to be READ. A multi-megabyte log pasted whole
    buries the description it is supposed to support."""
    log = tmp_path / "big.log"
    log.write_text("x" * 500_000 + "\nTHE LAST LINE\n")
    monkeypatch.setattr("fused_render.logs.log_path", lambda: str(log))

    incident, _ = selffix.record_incident({"note": "slow"})
    text = open(incident, encoding="utf-8").read()
    assert len(text) < selffix.LOG_TAIL_BYTES + 8_000
    assert "THE LAST LINE" in text  # the TAIL, not the head


def test_a_missing_log_is_not_worth_a_word(install, monkeypatch):
    monkeypatch.setattr("fused_render.logs.log_path", lambda: "/nope/absent.log")
    incident, _ = selffix.record_incident({"note": "slow"})
    assert "Recent app log" not in open(incident, encoding="utf-8").read()


def test_a_failed_row_with_no_message_still_gets_the_failure_brief(client,
                                                                   monkeypatch):
    """A job row may be `state: error` with an empty `message` (jobs.py leaves
    it "" and the manager renders a bare "Failed"). Keying the brief off the
    error TEXT handed that row the Preferences one — "nothing crashed, the user
    opened Preferences and described something" — which is false twice over and
    steers the session away from a failure that really happened."""
    seen = {}
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda t, p, m: seen.update(prompt=p) or {"run_id": "r"})
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready", lambda *a, **k: None)

    res = post(client, "/api/selffix/start",
               {"job_id": "sys:ai-model:x", "title": "FLUX.2-klein-4B",
                "state": "error", "kind": "download", "message": "",
                "source": "download manager"})
    assert res.status_code == 200
    assert "NOTHING CRASHED" not in seen["prompt"]
    assert "trace the failure" in seen["prompt"]


def test_start_refuses_a_session_with_nothing_to_look_at(client, monkeypatch):
    """Not validation for its own sake: a session handed no failure, no
    description and no name would read code at random and then report on having
    done so — which costs the user minutes to discover."""
    called = []
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda *a, **k: called.append(a) or {"run_id": "r"})
    res = post(client, "/api/selffix/start", {"source": "preferences", "note": "   "})
    assert res.status_code == 400
    assert "say what is wrong" in res.json()["error"]
    assert called == []


def test_a_described_problem_starts_a_session_and_is_labelled_by_its_first_line(
        client, install, monkeypatch):
    seen = {}

    def fake_spawn(target, prompt, mode):
        seen.update(prompt=prompt)
        return {"run_id": "run-9"}

    monkeypatch.setattr(selffix_routes, "_spawn_helper", fake_spawn)
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready", lambda *a, **k: None)

    res = post(client, "/api/selffix/start",
               {"note": "Dates are wrong in the parquet preview.\nOff by a day.",
                "source": "preferences"})
    assert res.status_code == 200
    # No error was reported, so the session gets the reproduce-first brief.
    assert "NOTHING CRASHED" in seen["prompt"]
    assert "Dates are wrong" in open(res.json()["incident"], encoding="utf-8").read()

    # The marker labels the fix by the description's first line — "a problem the
    # user described" over every row in the panel would say nothing.
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="run-9",
                   title="Dates are wrong in the parquet preview.")
    assert selffix.status()["fixes"][0]["title"] == (
        "Dates are wrong in the parquet preview.")


def test_list_reports_is_newest_first(install):
    _, first = selffix.record_incident({"title": "a"}, now=1000.0)
    _, second = selffix.record_incident({"title": "b"}, now=2000.0)
    os.utime(first, (1000.0, 1000.0))
    os.utime(second, (2000.0, 2000.0))
    assert [r["path"] for r in selffix.list_reports()] == [second, first]


@pytest.mark.parametrize("method", ["brew", "dmg", "windows", "linux", "source", "pip"])
def test_every_install_method_can_say_how_to_reinstall(install, method, monkeypatch):
    """The badge's other half. A panel that says "this app has been modified"
    and cannot tell you how to get an unmodified one is only half an answer, so
    every branch has to carry a headline, a note and a working link."""
    monkeypatch.setattr(selffix, "install_method", lambda: method)
    advice = selffix.reinstall_advice()
    assert advice["method"] == method
    assert advice["headline"] and advice["note"]
    assert advice["url"].startswith("https://")
    # The panel promotes the link to the section's ACTION when there is no
    # command to type, and words it from here — a raw URL as the only call to
    # action reads as a citation. So a label is never optional.
    assert advice["url_label"]


def test_a_dmg_install_has_nothing_to_type(install, monkeypatch):
    """The contract the panel's styling reads: empty `command` means the link
    IS the instruction. The DMG is dragged, not run — and it is the most common
    end-user install, so this is the branch that decides whether the reinstall
    section has a visible call to action at all."""
    for method in ("dmg", "windows", "linux"):
        monkeypatch.setattr(selffix, "install_method", lambda m=method: m)
        assert selffix.reinstall_advice()["command"] == ""
    for method in ("brew", "pip", "source"):
        monkeypatch.setattr(selffix, "install_method", lambda m=method: m)
        assert selffix.reinstall_advice()["command"]


def test_the_prompt_names_the_two_files_and_fences_the_agent_in(install):
    prompt = selffix.fix_prompt("/i/incident.md", "/r/report.md")
    assert "/i/incident.md" in prompt
    assert "/r/report.md" in prompt
    assert str(install) in prompt
    # The three rules a wandering agent breaks first.
    assert "static/shell-dist" in prompt
    assert "restarted" in prompt
    assert "Only edit files under" in prompt


# ------------------------------------------------------------------ the API


def test_start_requires_the_write_guard(client):
    assert client.post("/api/selffix/start", json={}).status_code == 403


def test_start_refuses_a_read_only_installation(client, monkeypatch):
    """Refused BEFORE the spawn: a session that cannot write spends minutes
    reading and then reports a fix that was never applied, which reads to the
    user exactly like a fix that was."""
    monkeypatch.setattr(selffix, "writable", lambda: False)
    called = []
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda *a, **k: called.append(a) or {"run_id": "r"})
    res = post(client, "/api/selffix/start", {"title": "boom"})
    assert res.status_code == 409
    assert "read-only" in res.json()["error"]
    assert called == []


def test_start_spawns_on_the_install_root_and_hands_back_the_run(client, install,
                                                                 monkeypatch):
    seen = {}

    def fake_spawn(target, prompt, mode):
        seen.update(target=target, prompt=prompt, mode=mode)
        return {"run_id": "run-7"}

    monkeypatch.setattr(selffix_routes, "_spawn_helper", fake_spawn)
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready",
                        lambda *a, **k: None)

    res = post(client, "/api/selffix/start",
               {"title": "download failed", "message": "OSError(28)"})
    assert res.status_code == 200
    body = res.json()
    assert body["run_id"] == "run-7"
    assert body["target"] == str(install)
    assert seen["target"] == str(install)
    # Not "auto" (the app scaffolder's mode): this session edits the
    # application itself, in front of a user who is watching it.
    assert seen["mode"] == selffix.FIX_PERMISSION_MODE == "prompt"
    assert os.path.exists(body["incident"])
    assert "OSError(28)" in open(body["incident"], encoding="utf-8").read()


def test_a_failed_spawn_says_why(client, monkeypatch):
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda *a, **k: {"error": "claude CLI not found"})
    res = post(client, "/api/selffix/start", {"title": "boom"})
    assert res.status_code == 502
    assert "claude CLI not found" in res.json()["error"]


def test_a_failed_spawn_does_not_wedge_the_one_session_slot(client, monkeypatch):
    """Every early return has to release it, or one missing CLI locks the
    feature out for the TTL's whole hour."""
    monkeypatch.setattr(selffix_routes, "_spawn_helper",
                        lambda *a, **k: {"error": "claude CLI not found"})
    assert post(client, "/api/selffix/start", {"title": "a"}).status_code == 502

    monkeypatch.setattr(selffix_routes, "_spawn_helper", lambda *a, **k: {"run_id": "r"})
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready",
                        lambda *a, **k: None)
    assert post(client, "/api/selffix/start", {"title": "b"}).status_code == 200


def test_only_one_fix_session_runs_at_a_time(client, monkeypatch):
    """Two agents rewriting one installation is not concurrency, it is a
    conflict — and a user with two failed rows clicking Fix on both is the
    ordinary way to get there."""
    monkeypatch.setattr(selffix_routes, "_spawn_helper", lambda *a, **k: {"run_id": "r1"})
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    # A watcher that never returns: the first session is still running.
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready",
                        lambda *a, **k: threading.Event().wait(5))

    assert post(client, "/api/selffix/start", {"title": "a"}).status_code == 200
    second = post(client, "/api/selffix/start", {"title": "b"})
    assert second.status_code == 409
    assert "already running" in second.json()["error"]
    assert "r1" in second.json()["error"]


def test_the_watcher_stamps_when_the_session_changed_something(install, monkeypatch):
    """The stamp is the app's decision, not the model's — a session asked to
    mark its own work is a session that can forget to."""
    _pristine()

    def fake_record(agent, run_id, on_tick=None):
        (install / "jobs.py").write_text("patched by the session\n")
        on_tick({"done": True, "session_id": "sess-1"})

    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready", fake_record)
    selffix_routes._watch_fix("run-7", "/i.md", "/r.md", "download failed", BEFORE[0])

    state = selffix.status()
    assert state is not None
    assert state["fixes"][0]["session_id"] == "sess-1"
    assert state["fixes"][0]["title"] == "download failed"


def test_the_watcher_leaves_an_untouched_installation_alone(install, monkeypatch):
    _pristine()
    monkeypatch.setattr(selffix_routes, "_load_agent", lambda: None)
    monkeypatch.setattr(selffix_routes, "_record_session_when_ready",
                        lambda agent, run_id, on_tick=None: on_tick({"done": True}))
    selffix_routes._watch_fix("run-7", "/i.md", "/r.md", "", BEFORE[0])
    assert selffix.status() is None


def test_two_fixes_in_the_same_second_do_not_overwrite_each_other(install):
    """The collision would not be a duplicate file — it would be the second
    session clobbering the first session's report while it was being written."""
    incident_a, report_a = selffix.record_incident({"title": "a"}, now=1000.0)
    incident_b, report_b = selffix.record_incident({"title": "b"}, now=1000.0)
    assert report_a != report_b and incident_a != incident_b
    assert "# Incident — a" in open(report_a, encoding="utf-8").read()
    assert "# Incident — b" in open(report_b, encoding="utf-8").read()


def test_snapshot_carries_the_panel_and_config_carries_only_the_flag(client, install):
    """The split that keeps /api/config cheap: the chip's PRESENCE rides the
    config poll, its CONTENTS (a directory walk and, on a mac, a brew probe)
    are fetched once when the panel opens."""
    _pristine()
    assert "modified_install" not in client.get("/api/config").json()

    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")

    config = client.get("/api/config").json()
    assert config["modified_install"]["modified"] is True

    snapshot = client.get("/api/selffix").json()
    assert snapshot["modified"] is True
    assert snapshot["install_root"] == str(install)
    assert snapshot["reinstall"]["headline"]
    assert snapshot["reinstall"]["url"]
    assert "issues_url" in snapshot


def test_clear_endpoint(client, install):
    _pristine()
    (install / "jobs.py").write_text("patched\n")
    selffix.settle(before=BEFORE[0], run_id="r1")
    assert client.post("/api/selffix/clear").status_code == 403
    assert post(client, "/api/selffix/clear").json() == {"cleared": True}
    assert "modified_install" not in client.get("/api/config").json()
