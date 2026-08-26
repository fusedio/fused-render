from __future__ import annotations

import os
import subprocess

from fastapi.testclient import TestClient

from fused_render.fusedcli import FusedCli
from fused_render.server import create_app
from fused_render import workbench_deploy


def _page(tmp_path):
    page = tmp_path / "index.html"
    page.write_text(
        '<meta name="fused-app"><script>fused.runPython("./run.py", {})</script>'
    )
    (tmp_path / "run.py").write_text("def main():\n    return 42\n")
    return page


def test_deploy_pushes_shares_and_records_without_generated_tree(tmp_path, monkeypatch):
    page = _page(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setattr(
        workbench_deploy, "fused_cli", lambda: FusedCli(command=["fused"], external=False)
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "push" in command:
            stage = command[command.index("push") + 1]
            assert os.path.isfile(os.path.join(stage, "canvas.toml"))
            return subprocess.CompletedProcess(
                command, 0, "https://www.fused.io/workbench/me/Demo_App\n", ""
            )
        return subprocess.CompletedProcess(
            command, 0, "https://www.fused.io/canvas/fc_testtoken\n", ""
        )

    monkeypatch.setattr(workbench_deploy.subprocess, "run", run)
    record = workbench_deploy.deploy_workbench_app(str(page), "Demo_App")

    assert record.shared is True
    assert record.app_url.endswith(f"/fc_testtoken/{record.shell_slug}")
    assert [call[0][3] for call in calls] == ["push", "share"]
    assert all(call[1]["env"]["FUSED_RENDER_CANVAS_PUSH_INTERNAL"] == "1" for call in calls)
    stored = workbench_deploy.list_deployments(str(page))
    assert stored[0]["digest"] == record.digest
    assert stored[0]["page"] == str(page)


def test_share_failure_keeps_successful_push_record(tmp_path, monkeypatch):
    page = _page(tmp_path)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        workbench_deploy, "fused_cli", lambda: FusedCli(command=["fused"], external=False)
    )

    def run(command, **kwargs):
        if "push" in command:
            return subprocess.CompletedProcess(command, 0, "pushed\n", "")
        return subprocess.CompletedProcess(command, 1, "", "Error: sharing unavailable\n")

    monkeypatch.setattr(workbench_deploy.subprocess, "run", run)
    record = workbench_deploy.deploy_workbench_app(str(page), "Demo_App")
    assert record.shared is False
    assert any("could not be shared" in warning for warning in record.warnings)
    assert workbench_deploy.list_deployments(str(page))[0]["digest"] == record.digest


def test_plan_api_reports_generated_canvas(tmp_path):
    page = _page(tmp_path)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    response = client.post(
        "/api/apps/workbench/plan",
        headers={"X-Fused": "1"},
        json={"page": str(page), "canvas_name": "Demo_App", "cache_max_age": "1m"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["canvas_name"] == "Demo_App"
    assert data["entrypoints"]["./run.py"].startswith("fr_run_")
    assert "canvas.toml" in data["generated_files"]


def test_plan_api_requires_guard_and_valid_canvas_name(tmp_path):
    page = _page(tmp_path)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert client.post(
        "/api/apps/workbench/plan", json={"page": str(page), "canvas_name": "Demo"}
    ).status_code == 403
    response = client.post(
        "/api/apps/workbench/plan",
        headers={"X-Fused": "1"},
        json={"page": str(page), "canvas_name": "not allowed"},
    )
    assert response.status_code == 400
    assert "letters" in response.json()["error"]


def test_a_corrupt_row_does_not_break_listing_or_saving(tmp_path, monkeypatch):
    """One bad `deployed_at` used to 500 every later deploy, after the push.

    _save_record reads the store once the canvas is already pushed and shared,
    so a bare float() there turned a successful deploy into a visible failure
    the user would reasonably retry.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    from fused_render.shell import storage

    store = workbench_deploy._store_path()
    storage.write_json(
        store,
        {
            "version": 1,
            "deployments": [
                {"page": "/a/index.html", "canvas_name": "A", "deployed_at": "corrupt"},
                {"page": "/b/index.html", "canvas_name": "B", "deployed_at": 20.0},
                {"page": "/c/index.html", "canvas_name": "C", "deployed_at": 10.0},
            ],
        },
    )

    listed = workbench_deploy.list_deployments()
    assert [item["canvas_name"] for item in listed] == ["B", "C", "A"]

    record = workbench_deploy.DeploymentRecord(
        page="/d/index.html",
        canvas_name="D",
        digest="d" * 64,
        shell_slug="fr_shell_dddddddd",
        environment="prod",
        deployed_at=30.0,
        generated_bytes=1,
        workbench_url="https://www.fused.io/workbench",
        share_url=None,
        app_url=None,
        shared=False,
        warnings=(),
    )
    workbench_deploy._save_record(record)
    assert [item["canvas_name"] for item in workbench_deploy.list_deployments()][0] == "D"
