"""Push compiled fused-render apps through the existing Workbench Canvas CLI."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Any

from fused_render._canvas_push import INTERNAL_ENV
from fused_render.fusedcli import child_env, cli_error, fused_cli, workbench_env
from fused_render.shell import storage
from fused_render.workbench_app import (
    CompiledWorkbenchApp,
    compile_workbench_app,
    write_compiled_canvas,
)


PUSH_TIMEOUT_S = 240
SHARE_TIMEOUT_S = 90
_STORE_NAME = "workbench_app_deployments.json"
_URL_RE = re.compile(r"https?://[^\s]+")
_TOKEN_RE = re.compile(r"/canvas/(fc_[A-Za-z0-9_-]+)")
_WEB_BASES = {
    "prod": "https://www.fused.io",
    "unstable": "https://unstable.fused.io",
    "stg": "https://staging.fused.io",
    "staging": "https://staging.fused.io",
    "dev": "http://localhost:3000",
}
_UDF_BASES = {
    "prod": "https://udf.ai",
    "unstable": "https://unstable.udf.ai",
    "stg": "https://staging.udf.ai",
    "staging": "https://staging.udf.ai",
    "dev": "http://localhost:8783/v1/realtime-shared",
}


class WorkbenchDeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentRecord:
    page: str
    canvas_name: str
    digest: str
    shell_slug: str
    environment: str
    deployed_at: float
    generated_bytes: int
    workbench_url: str
    share_url: str | None
    app_url: str | None
    shared: bool
    warnings: tuple[str, ...]


def _store_path() -> str:
    return os.path.join(storage.home_dir(), _STORE_NAME)


def list_deployments(page: str | None = None) -> list[dict[str, Any]]:
    raw = storage.read_json(_store_path())
    records = raw.get("deployments", []) if isinstance(raw, dict) else []
    valid = [item for item in records if isinstance(item, dict)]
    if page:
        identity = os.path.normcase(os.path.abspath(page))
        valid = [
            item
            for item in valid
            if os.path.normcase(os.path.abspath(str(item.get("page", "")))) == identity
        ]
    return sorted(valid, key=lambda item: float(item.get("deployed_at", 0)), reverse=True)


def _save_record(record: DeploymentRecord) -> None:
    records = list_deployments()
    identity = (os.path.normcase(record.page), record.canvas_name, record.environment)
    records = [
        item
        for item in records
        if (
            os.path.normcase(str(item.get("page", ""))),
            item.get("canvas_name"),
            item.get("environment"),
        )
        != identity
    ]
    records.insert(0, asdict(record))
    storage.write_json(_store_path(), {"version": 1, "deployments": records[:100]})


def deployment_plan(compiled: CompiledWorkbenchApp) -> dict[str, Any]:
    return {
        "canvas_name": compiled.canvas_name,
        "digest": compiled.digest,
        "shell_slug": compiled.shell_slug,
        "entrypoints": compiled.entrypoints,
        "assets": compiled.assets,
        "generated_files": sorted(compiled.files),
        "generated_bytes": compiled.generated_bytes,
        "warnings": list(compiled.warnings),
    }


def _cli_environment(cli) -> dict[str, str]:
    env = child_env(cli)
    env["FUSED_ENV"] = workbench_env()
    env[INTERNAL_ENV] = "1"
    return env


def _run_cli(cli, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(
            [*cli.command, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_cli_environment(cli),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkbenchDeployError(
            f"`fused {' '.join(args[:3])}` timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise WorkbenchDeployError(
            f"could not run the fused CLI ({cli.command[0]}): {exc}"
        ) from exc
    if process.returncode:
        raise WorkbenchDeployError(
            cli_error(process.stderr or process.stdout, f"fused {' '.join(args[:3])} failed")
        )
    return process


def _last_url(output: str) -> str | None:
    matches = _URL_RE.findall(output or "")
    return matches[-1].rstrip(".,)") if matches else None


def _bases(environment: str) -> tuple[str, str]:
    web = os.environ.get("FUSED_RENDER_WORKBENCH_URL") or _WEB_BASES.get(
        environment, "https://www.fused.io"
    )
    udf = _UDF_BASES.get(environment, "https://udf.ai")
    return web.rstrip("/"), udf.rstrip("/")


def deploy_workbench_app(
    html_path: str,
    canvas_name: str,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    cache_max_age: str = "0s",
    share: bool = True,
) -> DeploymentRecord:
    """Compile, push, optionally share, and record a dedicated app Canvas."""
    compiled = compile_workbench_app(
        html_path,
        canvas_name,
        include=include,
        exclude=exclude,
        cache_max_age=cache_max_age,
    )
    cli = fused_cli()
    if cli is None:
        raise WorkbenchDeployError(
            "the fused CLI is unavailable; install fused-render with the [fused] extra "
            "or set FUSED_RENDER_FUSED_BIN"
        )

    with tempfile.TemporaryDirectory(prefix="fused-render-workbench-") as stage:
        write_compiled_canvas(compiled, stage)
        pushed = _run_cli(
            cli,
            ["workbench", "canvas", "push", stage, "--canvas", compiled.canvas_name],
            PUSH_TIMEOUT_S,
        )

    environment = workbench_env()
    web_base, udf_base = _bases(environment)
    workbench_url = _last_url(pushed.stdout) or f"{web_base}/workbench"
    warnings = list(compiled.warnings)
    share_url = None
    app_url = None
    shared = False
    if share:
        try:
            shared_process = _run_cli(
                cli,
                ["workbench", "canvas", "share", compiled.canvas_name],
                SHARE_TIMEOUT_S,
            )
            share_url = _last_url(shared_process.stdout)
            token_match = _TOKEN_RE.search(share_url or "")
            if token_match:
                app_url = f"{udf_base}/{token_match.group(1)}/{compiled.shell_slug}"
                shared = True
            else:
                warnings.append(
                    "Canvas was pushed, but the share command did not return an fc_ token; "
                    "open it in Workbench to manage sharing."
                )
        except WorkbenchDeployError as exc:
            warnings.append(f"Canvas was pushed but could not be shared automatically: {exc}")
    else:
        warnings.append("Canvas was pushed without creating or resolving a share token.")

    if shared:
        warnings.append(
            "A team-scoped direct app URL needs a fused_session_token query parameter; "
            "public Canvas shares do not. Sharing scope remains managed in Workbench."
        )
    record = DeploymentRecord(
        page=os.path.abspath(html_path),
        canvas_name=compiled.canvas_name,
        digest=compiled.digest,
        shell_slug=compiled.shell_slug,
        environment=environment,
        deployed_at=time.time(),
        generated_bytes=compiled.generated_bytes,
        workbench_url=workbench_url,
        share_url=share_url,
        app_url=app_url,
        shared=shared,
        warnings=tuple(warnings),
    )
    _save_record(record)
    return record
