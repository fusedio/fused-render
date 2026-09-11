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
    "readme": ("essentials", "warning", "fact"),
    "icon": ("essentials", "warning", "fact"),
    "device-paths": ("sharing", "warning", "candidate"),
    "git": ("sharing", "warning", "fact"),
    "pushed": ("sharing", "warning", "fact"),
    "generated": ("sharing", "warning", "fact"),
    "preview": ("sharing", "warning", "fact"),
}


def test_every_check_carries_the_exact_table(workspace):
    d = _app(workspace)
    report = app_doctor.report(str(d))
    got = {c["id"]: (c["section"], c["severity"], c["kind"]) for c in report["checks"]}
    assert got == _EXPECTED_META


def test_no_checklist_row_is_ever_suggested():
    """The checklist has two severities, not three — a suggestion row got no
    tint, no rail, and no urgency in the dialog, so nobody ever acted on it.
    `suggested` is a CI-floor-only tier (see `_STRUCTURE_META` in
    `skills/fused-render-app-doctor/ci/app_check.py`); it must never appear
    in `_CHECK_META` or `SEVERITIES`."""
    assert "suggested" not in app_doctor.SEVERITIES
    assert all(sev in app_doctor.SEVERITIES for _section, sev, _kind in
              app_doctor._CHECK_META.values())


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
    assert report["severities"] == ["critical", "warning"]


def test_ok_is_false_on_a_failing_warning_row(workspace):
    """readme and preview are both severity "warning" — a warning row worth
    the checklist at all is worth turning the app "not ok"."""
    d = _app(workspace, readme=False, preview=False)
    report = app_doctor.report(str(d))
    assert _state(report, "readme") == "fail"
    assert _state(report, "preview") == "fail"
    assert report["ok"] is False


def test_ok_is_false_on_a_failing_pyproject_row(workspace):
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


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_a_wholly_untracked_app_folder_reads_as_a_folder_finding_not_a_bare_code(
    workspace,
):
    """git collapses a fully-untracked directory to one `?? demo/` line. After
    the app-relative prefix strip, `rest` is exactly empty (D-defect-2) — the
    finding must read as the app folder itself being untracked, never as a
    bare `??` with nothing after it."""
    d = _app(workspace)
    repo = workspace / "local"
    _git(repo, "init", "-q")
    # Nothing committed at all: the whole app folder is untracked.

    row = _rows(app_doctor.report(str(d)))["git"]
    assert row["state"] == "fail"
    assert len(row["findings"]) == 1
    finding = row["findings"][0]
    assert finding["path"] == "."
    assert finding["excerpt"] != "??"
    assert "?" not in finding["excerpt"]
    assert "untracked" in finding["excerpt"]


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_a_nested_untracked_directory_reads_as_a_directory_not_a_bare_code(workspace):
    """A subdirectory that is entirely new collapses to one `?? sub/` line —
    unlike the whole-app case, `rest` is not empty here (D-defect-2's
    variant), but it should still read as a directory, not a raw status
    line."""
    d = _app(workspace)
    repo = workspace / "local"
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    sub = d / "sub"
    sub.mkdir()
    (sub / "new.py").write_text("x = 1\n")

    row = _rows(app_doctor.report(str(d)))["git"]
    assert row["state"] == "fail"
    assert len(row["findings"]) == 1
    finding = row["findings"][0]
    assert finding["path"] == "sub/"
    assert finding["excerpt"] == "sub/ (untracked directory)"


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_an_app_that_is_its_own_repo_root_does_not_over_claim_the_whole_folder(
    workspace,
):
    """Review finding 4: `rest` strips to empty for ANY porcelain line whose
    path equals the app's own basename, not only the whole-app-folder
    collapse. When the app folder IS the repo root (an unmigrated app with
    its own `.git`, `app_git._repo_scope`'s other supported layout) git
    already reports paths relative to the app dir itself — no prefix to
    strip at all. An untracked subdirectory that happens to share the app's
    own folder name (`?? demo/` inside app `demo/`) must read as that
    subdirectory being untracked, not as the whole app being untracked."""
    d = _app(workspace, name="demo")
    _git(d, "init", "-q")
    _git(d, "add", "-A")
    _git(d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    sub = d / "demo"  # same basename as the app folder itself
    sub.mkdir()
    (sub / "new.py").write_text("x = 1\n")

    row = _rows(app_doctor.report(str(d)))["git"]
    assert row["state"] == "fail"
    assert len(row["findings"]) == 1
    finding = row["findings"][0]
    # Must NOT read as "the whole app folder is untracked" — only one
    # subdirectory is.
    assert finding["path"] != "."
    assert "whole app folder" not in finding["excerpt"]
    assert finding["path"] == "demo/"
    assert finding["excerpt"] == "demo/ (untracked directory)"


# --------------------------------------------------------------- pushed

def test_a_folder_that_is_not_a_repo_skips_the_pushed_row_with_the_right_reason(
    workspace,
):
    """A folder with no git repo at all must not be told it has "no upstream
    configured" — that implies a repo that just needs a remote wired up, a
    different and wrong fact from "this isn't a git repository". The wording
    matches `_git_check`'s own NO_REPO sentence for the `git` row, since it
    is the same condition."""
    d = _app(workspace)
    row = _rows(app_doctor.report(str(d)))["pushed"]
    assert row["state"] == "skip"
    assert row["detail"] == "this folder is not in a git repository this server can read"


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
    row = _rows(app_doctor.report(str(d)))["pushed"]
    assert row["state"] == "skip"
    # A REAL, readable repo with no upstream gets a different sentence than
    # "not a git repository" (see the non-repo test above) — this one is
    # actually about the missing upstream.
    assert row["detail"] == "no upstream branch configured for this folder — nothing to compare against"


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


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not on PATH")
def test_a_sibling_apps_unpushed_commit_is_not_this_apps_finding(workspace):
    """Same D626 scoping problem as the git row's own sibling test above, but
    for `pushed`: `rev-list --count @{upstream}..HEAD` and the `log` that
    names the subjects both walk the WHOLE shared repo's history unless
    scoped with `-- .`, so a neighbour app's unpushed commit would otherwise
    count against every app in the workspace and hand its commit subject to
    a fix session that has nothing to do with it."""
    mine = _app(workspace, "mine")
    theirs = _app(workspace, "theirs")
    repo = workspace / "local"
    remote = workspace.parent / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True,
                   capture_output=True, close_fds=False)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "in")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "HEAD")

    (theirs / "wip.py").write_text("half a thought\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "theirs only")

    assert _state(app_doctor.report(str(mine)), "pushed") == "pass"
    theirs_row = _rows(app_doctor.report(str(theirs)))["pushed"]
    assert theirs_row["state"] == "fail"
    assert any("theirs only" in f["excerpt"] for f in theirs_row["findings"])


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


# ------------------------------------------------------------- per-check task
#
# GET /api/apps/doctor used to take an optional `check` query param that
# re-ran just one row (`app_doctor.report_one`), for "a row refreshing itself
# after its own fix task lands". That moment never occurs — creating a fix
# task navigates away and closes the dialog — and `getAppDoctorCheck`
# (frontend/src/platform/lib/api.ts) had no caller anywhere in
# frontend/src, so the branch and its client helper were removed together
# rather than shipping untested surface (the two tests that lived here,
# test_get_with_check_reruns_just_that_row and
# test_get_with_an_unknown_check_is_a_400, went with them). `report_one`
# itself stays: the POST fix task below still uses it to gather one row's
# findings without paying for a full report.


def test_doctor_prompt_reads_kind_from_the_engine_not_the_fallback_table(monkeypatch):
    """`doctor_prompt` must ask `_meta` for `kind`, not read `_CHECK_META`
    directly — `_meta` prefers the floor engine's own `CHECK_META` for
    `secrets`/`device-paths`, the single source of truth for their
    classification (see the module docstring). If the engine ever
    reclassifies one of those two ids, the fix prompt has to follow: asking
    for a fix-outright when triage-first was needed is exactly the mistake
    the candidate/fact split exists to prevent."""

    class FakeEngine:
        CHECK_META = {"secrets": ("essentials", "critical", "fact")}

    monkeypatch.setattr(app_doctor, "engine", lambda: FakeEngine())
    prompt = app_doctor.doctor_prompt("app/index.html", "secrets", [])
    assert "Fix what is safe to fix here" in prompt
    assert "CANDIDATE row" not in prompt


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


# ----------------------------------------------------- R1: prompt carries detail


def test_a_fact_rows_prompt_carries_its_own_detail_and_drops_the_fallback(workspace):
    """`api-version` is a `kind="fact"` row and never carries findings — its
    `detail` IS the diagnosis. The old fallback line pointed at a `detail`
    the prompt never actually included; that string must be gone."""
    current = app_doctor.fused_api_version.current_version()
    if current <= 0:
        pytest.skip("no migration docs resolve, so nothing is behind")
    d = _app(workspace, version=0)
    row = _rows(app_doctor.report(str(d)))["api-version"]
    assert row["state"] == "fail"
    prompt = app_doctor.doctor_prompt(str(d / "index.html"), "api-version",
                                     row["findings"], row["detail"])
    assert row["detail"] in prompt
    assert "no findings listed" not in prompt


def test_a_candidate_rows_prompt_carries_both_detail_and_findings(workspace):
    """`secrets` is a `kind="candidate"` row: it DOES carry findings, and the
    row's own `detail` (e.g. "1 line to look at") must show up alongside
    them, not instead of them."""
    d = _app(workspace)
    (d / "app.py").write_text('AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    row = _rows(app_doctor.report(str(d)))["secrets"]
    assert row["state"] == "fail"
    assert row["findings"]
    prompt = app_doctor.doctor_prompt(str(d / "index.html"), "secrets",
                                     row["findings"], row["detail"])
    assert row["detail"] in prompt
    for f in row["findings"]:
        assert f["excerpt"] in prompt


def test_report_one_returning_none_behaves_as_today(monkeypatch):
    """No row (an unknown check id slipping past validation somehow) must not
    crash `doctor_prompt` — the router already guards `check_id` against
    `CHECK_ORDER`, but the prompt builder itself should not assume a detail
    is always available."""
    prompt = app_doctor.doctor_prompt("app/index.html", "readme", [], "")
    assert "readme" in prompt


def test_empty_detail_and_findings_omit_the_dangling_header(workspace):
    """Review finding 3: `apps.py`'s router passes `findings=[]`, `detail=""`
    whenever `report_one` returns `None` (an unknown check id slipping past
    validation). With no fallback line left, `_findings_block("", [])`
    returns `""` — the "Findings for this row:" header must not be emitted
    over nothing, or the prompt reads as a dangling, empty section."""
    prompt = app_doctor.doctor_prompt(str(_app(workspace) / "index.html"),
                                     "readme", [], "")
    assert "Findings for this row:" not in prompt
    assert "\n\n\n" not in prompt


# --------------------------------------------------- R1 grep: no dead fallback

def test_the_deleted_fallback_string_is_gone_from_the_module():
    """Defect 1's fallback line must not merely be unreachable — it must not
    exist anywhere a session (or a future test) could still find it."""
    src = open(app_doctor.__file__, encoding="utf-8").read()
    assert "no findings listed" not in src


# ------------------------------------------------------- R3: fix sessions commit


def test_a_single_row_prompt_ends_with_a_conditional_commit_step_and_no_push(workspace):
    """This covers a row whose fix EDITS FILES (`readme`) — the shape
    `_COMMIT_STEP` is right for. It deliberately does NOT stand in for every
    row: `git` and `pushed` don't edit files, their fix IS a git action
    spelled out in their own SKILL.md section, and appending this same
    "never push" / edit-conditioned step to THOSE rows is exactly what
    review findings 1 & 2 caught (a `pushed` row's only real fix is
    forbidden by "never push"; a `git` row's fix needs no edit, so "no edit
    -> no commit" disables it). Those two rows are covered separately below
    by `test_the_pushed_rows_own_prompt_carries_no_never_push_step` and
    `test_the_git_rows_own_prompt_carries_no_edit_conditioned_commit_step`."""
    d = _app(workspace)
    prompt = app_doctor.doctor_prompt(str(d / "index.html"), "readme", [], "no README")
    assert "commit" in prompt.lower()
    # "push" only ever appears as an explicit PROHIBITION ("never push") —
    # never as an instruction to actually push.
    assert "never push" in prompt.lower()
    assert "push the branch" not in prompt.lower()
    assert "push it" not in prompt.lower()


def test_the_pushed_rows_own_prompt_carries_no_never_push_step(workspace):
    """Review finding 1: the trailing commit step's "never push" contradicts
    SKILL.md's `pushed` section ("Push the branch") — the row's only real
    fix. The `pushed` row's own prompt must not carry the generic commit/
    no-push step at all; its own SKILL.md section is the whole instruction."""
    d = _app(workspace)
    prompt = app_doctor.doctor_prompt(str(d / "index.html"), "pushed", [],
                                     "1 commit ahead of upstream")
    assert "never push" not in prompt.lower()
    assert app_doctor._COMMIT_STEP not in prompt


def test_the_git_rows_own_prompt_carries_no_edit_conditioned_commit_step(workspace):
    """Review finding 2: `_COMMIT_STEP`'s "if you made no edit at all ...
    make no commit" disables the `git` row's own fix, which is exactly to
    commit pre-existing uncommitted paths with no file edit required. The
    `git` row's own prompt must not carry the generic commit step; SKILL.md's
    `git` section ("Commit the listed paths, or .gitignore them") is the
    whole instruction."""
    d = _app(workspace)
    prompt = app_doctor.doctor_prompt(str(d / "index.html"), "git",
                                     [{"rule": "git:uncommitted", "path": "?? a.py",
                                       "line": 0, "excerpt": "?? a.py"}],
                                     "1 uncommitted path")
    assert app_doctor._COMMIT_STEP not in prompt


def test_fix_all_containing_a_pushed_row_can_still_push_for_that_row(workspace):
    """Review finding 1 in `doctor_prompt_all`: a Fix-all run that includes a
    `pushed` row must still be able to push for that row — the trailing
    `_COMMIT_STEP_ALL` step must not be phrased as a blanket "never push"
    that overrides the `pushed` row's own SKILL.md instruction."""
    checks = [
        {"id": "pushed", "label": "Every commit is pushed", "kind": "fact",
         "state": "fail", "detail": "1 commit ahead of upstream", "findings": []},
    ]
    prompt = app_doctor.doctor_prompt_all(str(_app(workspace) / "index.html"), checks)
    assert "never push" not in prompt.lower()


def test_fix_all_containing_a_git_row_can_still_commit_pre_existing_paths(workspace):
    """Review finding 2 in `doctor_prompt_all`: a Fix-all run that includes a
    `git` row must still be able to commit paths that were already
    uncommitted before the run — not only "files you actually edited"."""
    checks = [
        {"id": "git", "label": "Every change is committed", "kind": "fact",
         "state": "fail", "detail": "1 uncommitted path",
         "findings": [{"rule": "git:uncommitted", "path": "?? a.py", "line": 0,
                       "excerpt": "?? a.py"}]},
    ]
    prompt = app_doctor.doctor_prompt_all(str(_app(workspace) / "index.html"), checks)
    # The trailing step must not read as forbidding a commit of pre-existing
    # uncommitted paths that the git row itself asked for.
    assert "pre-existing" in prompt.lower()


def test_fix_all_makes_one_trailing_commit_not_one_per_row(workspace):
    d = _app(workspace, readme=False)
    (d / "app.py").write_text('DATA = "/Users/alice/data.csv"\n')
    checks = app_doctor.report(str(d))["checks"]
    failing = [c for c in checks if c["state"] == "fail"]
    assert len(failing) >= 2  # readme AND device-paths, so a per-row commit
                              # step would show up more than once if present.
    prompt = app_doctor.doctor_prompt_all(str(d / "index.html"), checks)
    # The commit step is the block appended once, after every row — not
    # something each row's own block asks for.
    assert prompt.count(app_doctor._COMMIT_STEP_ALL) == 1
    for c in failing:
        row_block = (
            f"## `{c['id']}` — {c['label']}\n"
            f"{app_doctor._triage_ask(c['kind'])}\n"
            f"{app_doctor._findings_block(c['detail'], c['findings'])}"
        )
        assert "commit" not in row_block.lower()
    assert "push the branch" not in prompt.lower()
    assert "push it" not in prompt.lower()


def test_fix_all_with_nothing_failing_asks_for_no_commit(workspace):
    d = _app(workspace)
    checks = app_doctor.report(str(d))["checks"]
    prompt = app_doctor.doctor_prompt_all(str(d / "index.html"), checks)
    assert "commit" not in prompt.lower()


def test_the_secrets_commit_step_cannot_be_read_as_commit_the_secret_fix(workspace):
    """R3's sensitive case: the commit instruction must not be readable as
    licence to commit a still-live or merely relocated credential. A session
    following `_CREDENTIAL_NOTE` (report + say "rotate it", never move the
    value) makes no file edit for `secrets`, so the wording must make clear
    that no edit means no commit — and must never say "commit the fix" in a
    way that could be misapplied to a relocated secret."""
    d = _app(workspace)
    (d / "app.py").write_text('AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    row = _rows(app_doctor.report(str(d)))["secrets"]
    prompt = app_doctor.doctor_prompt(str(d / "index.html"), "secrets",
                                     row["findings"], row["detail"])
    assert "commit the fix" not in prompt.lower()
    assert "live" in prompt.lower() or "relocat" in prompt.lower()
