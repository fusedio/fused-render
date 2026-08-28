"""Push compiled fused-render apps through the existing Workbench Canvas CLI."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from typing import Any

from fused_render.canvases import _cli_env, web_base_url
from fused_render.fusedcli import cli_error, fused_cli, workbench_env
from fused_render.shell import storage
from fused_render.workbench_app import (
    CompiledWorkbenchApp,
    compile_workbench_app,
    write_compiled_canvas,
)


PUSH_TIMEOUT_S = 240
SHARE_TIMEOUT_S = 90
_STORE_NAME = "workbench_app_deployments.json"
# The trailing set matters: `fused workbench` echoes results as JSON unless
# told otherwise, so a URL arrives wrapped in quotes. We ask for text below;
# this stays as the belt to that braces.
_URL_RE = re.compile(r"""https?://[^\s"']+""")
_TOKEN_RE = re.compile(r"/canvas/(fc_[A-Za-z0-9_-]+)")
# The www hosts live in canvases.web_base_url(); only the UDF hosts are this
# module's own, because nothing else builds them.
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


def _deployed_at(item: dict[str, Any]) -> float:
    """The sort key, defensively.

    A bare float() here made one corrupt row fatal to DEPLOYING, not just to
    listing: _save_record reads the store after the push and the share have
    already succeeded, so the canvas would be live — possibly public — while
    the user saw a 500 and reasonably retried a deploy that had already
    happened. The rest of this store's readers are careful about its shape;
    this is the one that was not.
    """
    try:
        return float(item.get("deployed_at", 0))
    except (TypeError, ValueError):
        return 0.0


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
    return sorted(valid, key=_deployed_at, reverse=True)


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


# `--format text` on the `workbench` group, not a bare command. echo_result()
# falls back to JSON whenever the CLI context carries no output_format, which
# is how `canvas share` came back as "https://…/canvas/fc_x" — quotes and all —
# and put a stray quote on the end of every share link we handed out. Parsing
# whatever format the user's config happens to select is not a contract; asking
# for one is.
def _text_output(args: list[str]) -> list[str]:
    if args and args[0] == "workbench":
        return [args[0], "--format", "text", *args[1:]]
    return args


def _describe(args: list[str]) -> str:
    return "fused " + " ".join(arg for arg in args if not arg.startswith("--"))[:60]


def _run_cli(cli, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(
            [*cli.command, *_text_output(args)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_cli_env(cli),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkbenchDeployError(
            f"`{_describe(args)}` timed out after {timeout}s"
        ) from exc
    except OSError as exc:
        raise WorkbenchDeployError(
            f"could not run the fused CLI ({cli.command[0]}): {exc}"
        ) from exc
    if process.returncode:
        raise WorkbenchDeployError(
            cli_error(process.stderr or process.stdout, f"`{_describe(args)}` failed")
        )
    return process


def _last_url(output: str) -> str | None:
    matches = _URL_RE.findall(output or "")
    return matches[-1].rstrip(".,)\"'") if matches else None


def _bases(environment: str) -> tuple[str, str]:
    """The (www, udf) hosts one deployment's URLs are built from.

    Each host has its own override. They used to share one: FUSED_RENDER_
    WORKBENCH_URL moved the www base while the UDF base stayed pinned to the
    table, so pointing the app at a non-prod host produced a record whose
    workbench_url and app_url named two different deployments.
    """
    udf = os.environ.get("FUSED_RENDER_WORKBENCH_UDF_URL") or _UDF_BASES.get(
        environment, "https://udf.ai"
    )
    return web_base_url(environment), udf.rstrip("/")


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
    # UDF names follow the app's own file names, so they hold still across
    # deploys and the app URL with them. They did not always: an earlier
    # scheme put the content digest in every name, which renamed everything
    # on every push. Say so once, when a previous deploy of this page really
    # did answer to a different name, rather than warning forever.
    previous = next(iter(list_deployments(html_path)), None)
    if previous and previous.get("shell_slug") not in (None, compiled.shell_slug):
        warnings.append(
            f"The app URL moved: this deploy answers to '{compiled.shell_slug}', where "
            f"the last one answered to '{previous['shell_slug']}'. Links to the old "
            "address will not resolve."
        )
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
        # `canvas share` mints (or reuses) a share token and does NOT touch the
        # Canvas access scope: server-side share_collection leaves
        # access_scope and allow_public_read exactly as they were, so a Canvas
        # created by push stays team-scoped and the share link reads as
        # restricted. Nothing in the CLI can change that, so say so rather
        # than implying the link is public.
        warnings.append(
            "Sharing minted a share token but did NOT change the Canvas access scope: "
            "a Canvas created by this deploy stays team-scoped. Make it public in "
            "Workbench if you need link-only access."
        )
        warnings.append(
            "While the Canvas is team-scoped, the direct app URL needs a "
            "fused_session_token query parameter to run."
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
