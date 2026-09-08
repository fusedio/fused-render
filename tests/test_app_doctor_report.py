"""App Doctor as the app page and the explorer see it: the checklist
(`fused_render/app_doctor.py`) and the two endpoints that serve it
(`GET`/`POST /api/apps/doctor`).

The deterministic FLOOR — which secret shapes and device-path roots count — is
tested where it lives, against the script itself (test_app_doctor.py,
test_app_doctor_housekeeping.py). What is tested here is everything the report
adds on top: the rows the CI floor deliberately omits (the entry rule, the
declared fused API version, stray generated state, an optional file that does
not parse, an uncommitted working tree), the grouping of the floor's findings
into rows, and the fix task's one-per-app rule.

The spawn is stubbed at the same module seam the scaffolding tests use
(`apps_mod._create_app_task`) — no test here launches a real claude.
"""
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from fused_render import app_doctor, schedule
from fused_render.server import create_app
from fused_render.server.routers import apps as apps_mod

HDRS = {"X-Fused": "1"}

PAGE = ('<html><head><meta name="fused-app" />'
        '<meta name="fused-api-version" content="{v}" /></head><body>hi</body></html>')


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _app(workspace, name="demo", *, version=None, readme=True, preview=True):
    """An app folder that passes every check by default, so each test can break
    exactly one thing and read the row it broke."""
    d = workspace / "local" / name
    d.mkdir(parents=True)
    current = app_doctor.fused_api_version.current_version()
    (d / "index.html").write_text(PAGE.format(
        v=current if version is None else version))
    if readme:
        (d / "README.md").write_text("what this is\n")
    if preview:
        (d / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 8)
    return d


def _rows(report):
    return {c["id"]: c for c in report["checks"]}


def _state(report, cid):
    return _rows(report)[cid]["state"]


# --------------------------------------------------- section/severity/kind

# The spec's table, pinned exactly (app-doctor-spec.md §1).
_EXPECTED_META = {
    "secrets": ("essentials", "critical", "candidate"),
    "entry": ("essentials", "critical", "fact"),
    "api-version": ("essentials", "critical", "fact"),
    "pyproject": ("essentials", "warning", "fact"),
    "readme": ("essentials", "suggested", "fact"),
    "icon": ("essentials", "suggested", "fact"),
    "device-paths": ("sharing", "warning", "candidate"),
    "git": ("sharing", "warning", "fact"),
    "pushed": ("sharing", "warning", "fact"),
    "generated": ("sharing", "warning", "fact"),
    "preview": ("sharing", "suggested", "fact"),
}


def test_every_check_carries_the_exact_table(workspace):
    d = _app(workspace)
    report = app_doctor.report(str(d))
    got = {c["id"]: (c["section"], c["severity"], c["kind"]) for c in report["checks"]}
    assert got == _EXPECTED_META


def test_checks_are_grouped_essentials_then_sharing_in_server_order(workspace):
    d = _app(workspace)
    report = app_doctor.report(str(d))
    ids = [c["id"] for c in report["checks"]]
    assert ids == list(app_doctor.CHECK_ORDER)
    sections = [c["section"] for c in report["checks"]]
    # Every essentials row before every sharing row.
    assert sections == sorted(sections, key=app_doctor.SECTIONS.index)


def test_the_report_carries_the_ordering_the_modal_reads(workspace):
    d = _app(workspace)
    report = app_doctor.report(str(d))
    assert report["sections"] == ["essentials", "sharing"]
    assert report["severities"] == ["critical", "warning", "suggested"]


def test_ok_ignores_a_failing_suggested_row(workspace):
    """readme and preview are both severity "suggested" — failing either must
    not turn the app "not ok"."""
    d = _app(workspace, readme=False, preview=False)
    report = app_doctor.report(str(d))
    assert _state(report, "readme") == "fail"
    assert _state(report, "preview") == "fail"
    assert report["ok"] is True


def test_ok_is_false_on_a_failing_warning_row(workspace):
    d = _app(workspace)
    (d / "pyproject.toml").write_text("[project\nbroken\n")
    report = app_doctor.report(str(d))
    assert _state(report, "pyproject") == "fail"
    assert report["ok"] is False


# ------------------------------------------------------------------ the report

def test_a_clean_app_passes_every_check_it_can_answer(workspace):
    d = _app(workspace)
    report = app_doctor.report(str(d))
    rows = _rows(report)
    assert report["ok"] is True
    assert report["entry"] == str(d / "index.html")
    # Every row is one of the three states, and none of them failed.
    assert {c["state"] for c in report["checks"]} <= {"pass", "fail", "skip"}
    assert [c["id"] for c in report["checks"] if c["state"] == "fail"] == []
    # The two optional files are absent, which is not a finding.
    assert rows["pyproject"]["state"] == "skip"
    assert rows["icon"]["state"] == "skip"


def test_no_tagged_page_fails_the_entry_row(workspace):
    d = workspace / "local" / "pageless"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<html><body>no marker</body></html>")
    report = app_doctor.report(str(d))
    assert report["entry"] is None
    assert _state(report, "entry") == "fail"
    assert report["ok"] is False
    # The version row cannot be answered without an entry to read it off —
    # "skip", not a failure invented out of a missing file.
    assert _state(report, "api-version") == "skip"


def test_a_stale_api_version_fails_and_names_both_numbers(workspace):
    current = app_doctor.fused_api_version.current_version()
    if current <= 0:
        pytest.skip("no migration docs resolve, so nothing is behind")
    d = _app(workspace, version=0)
    report = app_doctor.report(str(d))
    row = _rows(report)["api-version"]
    assert row["state"] == "fail"
    assert "0" in row["detail"] and str(current) in row["detail"]


def test_a_leaked_key_and_a_device_path_land_in_their_own_rows(workspace):
    d = _app(workspace)
    (d / "app.py").write_text(
        'KEY = "AKIAIOSFODNN7EXAMPLE"\n'
        'DATA = "/Users/someone/private/data.csv"\n'
    )
    rows = _rows(app_doctor.report(str(d)))
    assert rows["secrets"]["state"] == "fail"
    assert rows["device-paths"]["state"] == "fail"
    # Grouped by family, not merged: a credential and a path are different
    # problems with different fixes.
    assert all(f["rule"].startswith("secrets:") for f in rows["secrets"]["findings"])
    assert all(f["rule"].startswith("device-path:")
               for f in rows["device-paths"]["findings"])
    # The excerpt is masked at the source (app_check.py's `_mask`) — the whole
    # secret must never reach a modal, a log, or a chat transcript.
    assert not any("AKIAIOSFODNN7EXAMPLE" in f["excerpt"]
                   for f in rows["secrets"]["findings"])
    assert rows["device-paths"]["findings"][0]["line"] == 2


def test_generated_state_outside_dot_fused_is_a_finding_and_inside_it_is_not(workspace):
    d = _app(workspace)
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00")
    (d / "notes.log").write_text("run 1\n")
    # The app's OWN state folder is exactly where this belongs (D548).
    (d / ".fused" / "cache").mkdir(parents=True)
    (d / ".fused" / "cache" / "tiles.db").write_bytes(b"\x00")

    row = _rows(app_doctor.report(str(d)))["generated"]
    assert row["state"] == "fail"
    found = {f["path"] for f in row["findings"]}
    # The cache dir is reported as ITSELF, not once per file inside it.
    assert found == {"__pycache__/", "notes.log"}


def test_missing_readme_and_thumbnail_each_fail_their_own_row(workspace):
    d = _app(workspace, readme=False, preview=False)
    rows = _rows(app_doctor.report(str(d)))
    assert rows["readme"]["state"] == "fail"
    assert rows["preview"]["state"] == "fail"


def test_an_empty_thumbnail_reads_as_no_thumbnail(workspace):
    d = _app(workspace)
    (d / "preview.png").write_bytes(b"")
    assert _state(app_doctor.report(str(d)), "preview") == "fail"


@pytest.mark.parametrize("name,body,cid", [
    ("pyproject.toml", "[project\nname = 'x'\n", "pyproject"),
    ("icon.svg", "<svg><path d=", "icon"),
])
def test_an_optional_file_that_does_not_parse_fails_its_row(workspace, name, body, cid):
    d = _app(workspace)
    (d / name).write_text(body)
    row = _rows(app_doctor.report(str(d)))[cid]
    assert row["state"] == "fail"
    assert name in row["detail"]


@pytest.mark.parametrize("name,body,cid", [
    ("pyproject.toml", '[project]\nname = "x"\n', "pyproject"),
    ("icon.svg", '<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>', "icon"),
])
def test_an_optional_file_that_parses_passes_its_row(workspace, name, body, cid):
    d = _app(workspace)
    (d / name).write_text(body)
    assert _state(app_doctor.report(str(d)), cid) == "pass"


def test_a_folder_that_is_not_a_repo_skips_the_git_row(workspace):
    d = _app(workspace)
    row = _rows(app_doctor.report(str(d)))["git"]
    assert row["state"] == "skip"
    # A skip is not a pass, but it is not a problem either.
    assert app_doctor.report(str(d))["ok"] is True


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, close_fds=False)


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_uncommitted_work_fails_the_git_row_and_a_clean_tree_passes(workspace):
    """The check the owner asked for: everything in the app is committed.

    Staged in the SHARED `local` repo (D626) rather than a repo per app, which
    is the layout real apps have — and the reason the row's paths are stripped
    back to app-relative: `git status` reports them from the repo root, one
    level above the folder being reviewed.
    """
    d = _app(workspace)
    repo = workspace / "local"
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")

    row = _rows(app_doctor.report(str(d)))["git"]
    assert row["state"] == "pass"

    (d / "app.py").write_text("print('new')\n")
    row = _rows(app_doctor.report(str(d)))["git"]
    assert row["state"] == "fail"
    assert [f["path"] for f in row["findings"]] == ["?? app.py"]


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_a_sibling_apps_uncommitted_work_is_not_this_apps_finding(workspace):
    """Sibling apps share one repo, so the status has to be scoped to the
    folder under review or every app in the workspace reads as dirty at once."""
    mine = _app(workspace, "mine")
    theirs = _app(workspace, "theirs")
    repo = workspace / "local"
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    (theirs / "wip.py").write_text("half a thought\n")

    assert _state(app_doctor.report(str(mine)), "git") == "pass"
    assert _state(app_doctor.report(str(theirs)), "git") == "fail"


# --------------------------------------------------------------- pushed

@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_no_remote_at_all_skips_the_pushed_row(workspace):
    """A folder with no remote configured is not a failing app — there is
    nothing to compare its branch against."""
    d = _app(workspace)
    repo = workspace / "local"
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    assert _state(app_doctor.report(str(d)), "pushed") == "skip"


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_a_branch_with_no_upstream_skips_the_pushed_row(workspace):
    d = _app(workspace)
    repo = workspace / "local"
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    # A remote exists, but the current branch has no tracking ref set up.
    remote = workspace.parent / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True,
                   capture_output=True, close_fds=False)
    _git(repo, "remote", "add", "origin", str(remote))
    assert _state(app_doctor.report(str(d)), "pushed") == "skip"


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_commits_ahead_of_upstream_fail_the_pushed_row_and_name_the_subjects(workspace):
    d = _app(workspace)
    repo = workspace / "local"
    remote = workspace.parent / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True,
                   capture_output=True, close_fds=False)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "HEAD")

    (d / "app.py").write_text("print('new')\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a new commit")

    row = _rows(app_doctor.report(str(d)))["pushed"]
    assert row["state"] == "fail"
    assert any("a new commit" in f["excerpt"] for f in row["findings"])


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_a_branch_pushed_up_to_date_passes_the_pushed_row(workspace):
    d = _app(workspace)
    repo = workspace / "local"
    remote = workspace.parent / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True,
                   capture_output=True, close_fds=False)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "HEAD")
    assert _state(app_doctor.report(str(d)), "pushed") == "pass"


def test_a_missing_folder_reports_rather_than_raising(tmp_path):
    """A doctor that crashes on the app it was asked to examine is worse than
    no doctor: every check degrades, none raises."""
    report = app_doctor.report(str(tmp_path / "nope"))
    assert report["ok"] is False
    assert _state(report, "entry") == "fail"
    assert {c["id"] for c in report["checks"]}  # a full checklist, not a stub


def test_an_unresolvable_check_engine_skips_its_rows_instead_of_failing(
    workspace, monkeypatch,
):
    d = _app(workspace)
    monkeypatch.setattr(app_doctor, "engine", lambda: None)
    monkeypatch.setattr(app_doctor, "engine_error", lambda: "no skill here")
    rows = _rows(app_doctor.report(str(d)))
    assert rows["secrets"]["state"] == "skip"
    assert rows["device-paths"]["state"] == "skip"
    assert rows["secrets"]["detail"] == "no skill here"
    # The rows that do not need the engine still answer.
    assert rows["readme"]["state"] == "pass"


# --------------------------------------------------------------- the endpoints

def test_get_serves_the_checklist_for_a_folder(client, workspace):
    d = _app(workspace)
    r = client.get("/api/apps/doctor", params={"path": str(d)})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # No live fix task on any row.
    assert all(c["task"] is None for c in body["checks"])
    assert [c["id"] for c in body["checks"]] == [
        c["id"] for c in app_doctor.report(str(d))["checks"]
    ]


def test_get_needs_an_absolute_path_and_a_real_folder(client, workspace):
    assert client.get("/api/apps/doctor", params={"path": "relative"}).status_code == 400
    r = client.get("/api/apps/doctor", params={"path": str(workspace / "gone")})
    assert r.status_code == 404


def test_get_reports_a_page_less_folder_instead_of_404ing(client, workspace):
    """A folder with no entry is the FIRST ROW of the report, not an error:
    "this is not shareable yet" is exactly what the dialog exists to say."""
    d = workspace / "local" / "pageless"
    d.mkdir(parents=True)
    r = client.get("/api/apps/doctor", params={"path": str(d)})
    assert r.status_code == 200
    assert r.json()["entry"] is None


def test_post_requires_the_fused_header(client, workspace):
    d = _app(workspace)
    assert client.post("/api/apps/doctor", json={"path": str(d)}).status_code == 403


def test_post_creates_one_task_for_the_named_row_invoking_the_skill(
    client, workspace, monkeypatch,
):
    d = _app(workspace)
    (d / "README.md").unlink()
    seen = []
    monkeypatch.setattr(apps_mod, "_create_app_task",
                        lambda entry, prompt, *rest:
                        seen.append((entry, prompt)) or ({"id": "t1", "run_id": "r1"}, None))
    r = client.post("/api/apps/doctor", json={"path": str(d), "check": "readme"},
                    headers=HDRS)
    assert r.status_code == 200
    body = r.json()
    assert body["entry_html"] == str(d / "index.html")
    assert body["check"] == "readme"
    assert body["task"] == {"id": "t1", "run_id": "r1"}
    assert body["task_error"] is None
    # The task lands on the FILE (that is what lets "open this task" reach the
    # page rather than the folder), and its prompt invokes the skill by name
    # and points at this row's own section.
    (entry, prompt), = seen
    assert entry == str(d / "index.html")
    assert app_doctor.is_doctor_prompt(prompt)
    assert app_doctor.doctor_task_check_id(prompt) == "readme"
    assert app_doctor.SKILL_QUALIFIED in prompt
    assert "readme" in prompt
    assert "index.html" in prompt


def test_post_requires_a_known_check_id(client, workspace):
    d = _app(workspace)
    r = client.post("/api/apps/doctor", json={"path": str(d), "check": "nonesuch"},
                    headers=HDRS)
    assert r.status_code == 400
    assert "check" in r.json()["error"]
    r = client.post("/api/apps/doctor", json={"path": str(d)}, headers=HDRS)
    assert r.status_code == 400


def test_post_check_all_covers_every_failing_row_in_one_task(
    client, workspace, monkeypatch,
):
    """"Fix all": one session, every failing row inline, section order."""
    d = _app(workspace, readme=False)
    (d / "app.py").write_text('DATA = "/Users/alice/data.csv"\n')
    seen = []
    monkeypatch.setattr(apps_mod, "_create_app_task",
                        lambda entry, prompt, *rest:
                        seen.append(prompt) or ({"id": "t1", "run_id": "r1"}, None))
    r = client.post("/api/apps/doctor", json={"path": str(d), "check": "all"},
                    headers=HDRS)
    assert r.status_code == 200
    body = r.json()
    assert body["check"] == "all"
    (prompt,), = [seen]
    assert app_doctor.is_doctor_prompt(prompt)
    assert app_doctor.doctor_task_check_id(prompt) == "all"
    assert "readme" in prompt
    assert "device-paths" in prompt
    # A passing row's own id has no reason to show up in the prompt.
    assert "`preview`" not in prompt


def test_post_check_all_and_a_single_row_share_the_same_409_lock(
    client, workspace, monkeypatch,
):
    d = _app(workspace)
    monkeypatch.setattr(apps_mod, "_live_app_task",
                        lambda e, matcher: (
                            {"id": "t1", "state": "sent", "run_id": "r1"}
                            if matcher("Run App Doctor on this fused-render app - "
                                      "check `readme` x") else None))
    r = client.post("/api/apps/doctor", json={"path": str(d), "check": "all"},
                    headers=HDRS)
    assert r.status_code == 409


def test_post_reports_a_task_that_could_not_be_stored(client, workspace, monkeypatch):
    d = _app(workspace)
    monkeypatch.setattr(apps_mod, "_create_app_task",
                        lambda *a, **k: (None, "store is read-only"))
    body = client.post("/api/apps/doctor", json={"path": str(d), "check": "readme"},
                       headers=HDRS).json()
    assert body["task"] is None
    assert body["task_error"] == "store is read-only"


def test_post_refuses_a_folder_with_no_entry_page(client, workspace):
    d = workspace / "local" / "pageless"
    d.mkdir(parents=True)
    r = client.post("/api/apps/doctor", json={"path": str(d), "check": "readme"},
                    headers=HDRS)
    assert r.status_code == 404
    assert "entry page" in r.json()["error"]


def test_post_refuses_a_second_task_while_one_is_live_even_for_a_different_row(
    client, workspace, monkeypatch,
):
    """One session per app: two rewriting the same folder at once is a merge
    nobody asked for, whether or not they're the same row. The GET says so
    too, so a reload shows "in progress" rather than offering another."""
    d = _app(workspace)
    entry = str(d / "index.html")
    live = {"id": "t1", "state": "sent", "run_id": "r1"}
    monkeypatch.setattr(apps_mod, "_live_app_task",
                        lambda e, matcher: live if matcher("Run App Doctor on this "
                                                           "fused-render app - check "
                                                           "`readme` x") else None)
    r = client.post("/api/apps/doctor", json={"path": str(d), "check": "icon"},
                    headers=HDRS)
    assert r.status_code == 409
    assert os.path.isfile(entry)


def test_post_rejects_an_unknown_model_or_effort(client, workspace):
    d = _app(workspace)
    for field in ("model", "effort"):
        r = client.post("/api/apps/doctor",
                        json={"path": str(d), "check": "readme", field: "nonesuch"},
                        headers=HDRS)
        assert r.status_code == 400, field
        assert field in r.json()["error"]


# ----------------------------------------------------- per-check GET and task


def test_get_with_check_reruns_just_that_row(client, workspace):
    d = _app(workspace, readme=False)
    r = client.get("/api/apps/doctor", params={"path": str(d), "check": "readme"})
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == ["path", "checks"]
    assert [c["id"] for c in body["checks"]] == ["readme"]
    assert body["checks"][0]["state"] == "fail"


def test_get_with_an_unknown_check_is_a_400(client, workspace):
    d = _app(workspace)
    r = client.get("/api/apps/doctor", params={"path": str(d), "check": "nonesuch"})
    assert r.status_code == 400


def test_a_live_task_attaches_to_its_own_row_only(client, workspace, monkeypatch):
    d = _app(workspace)
    entry = str(d / "index.html")
    prompt = app_doctor.doctor_prompt(entry, "readme", [])
    monkeypatch.setattr(
        schedule, "list_entries",
        lambda: [{
            "id": "t1", "state": schedule.SENT, "turn": None,
            "message": prompt, "target": entry, "run_id": "r1",
        }],
    )
    body = client.get("/api/apps/doctor", params={"path": str(d)}).json()
    rows = {c["id"]: c for c in body["checks"]}
    assert rows["readme"]["task"] == {"id": "t1", "state": "sent", "run_id": "r1"}
    assert rows["icon"]["task"] is None
