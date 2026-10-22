# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see NOTICE.
"""App-folder MCP assembly (spec/serve/app-mcp.md).

A *fused-render app* is a plain folder — `index.html` plus the `.py` entrypoints
the page drives through `fused.runPython()` (spec/serve/fused-render.md). This
module republishes a **curated subset** of those entrypoints as MCP tools so a
Claude session can call them with fused-render not running at all.

The curation is authored in the folder, as `[[tool]]` tables in `mcp.toml`
(§2) — fused-render's `mcp` template writes them; this module only reads them.
That manifest is the entire contract between the two repos.

Two things about this assembly are load-bearing and stated here because they are
easy to "fix" into wrongness:

* **Loading is strict** (§3). Here the manifest *is* the surface, so a malformed entry raises
instead of quietly shrinking
  `tools/list`.
* **Execution is always the local backend** (§5). An app's entrypoints read the
  author's own token files and localhost services; an env-resolved backend would
  ship their source to Lambda / Fused Cloud the moment the ambient environment
  is not `local`. `config.get_backend()` is deliberately not consulted.

An app folder is NOT a workspace project (no registry entry, no `scripts/`
layout, no `openfused.toml` — which is also why the manifest is named
`mcp.toml`: an app must stay inert to the project walk-up in `projects.py`).
The signature → JSON-schema helpers it uses live in `mcp_params.py` (§4).
"""

from __future__ import annotations

import ast
import asyncio
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fused_render.compute.mcp_extra import load_fastmcp
from fused_render.compute.mcp_params import (
    ParamSpec,
    funcdef_named,
    params_from_funcdef,
    synthesize_callable,
)

if TYPE_CHECKING:
    # `mcp` is the opt-in `mcp` extra: the class is loaded inside
    # `create_app_mcp` via `mcp_extra.load_fastmcp()`, so this module (manifest
    # loading, tool specs) imports without it.
    from mcp.server.fastmcp import FastMCP

# The manifest filename, at the app folder root (§2). NOT ``openfused.toml``:
# project resolution walks up looking for that name, so an app carrying it would
# start resolving as a project for every command run inside it.
MANIFEST_NAME = "mcp.toml"

# The default entrypoint name when a ``[[tool]]`` omits it (§2).
DEFAULT_ENTRYPOINT = "main"

# Pinned values cross the compute boundary as JSON (`_params.json`, §5), so the
# TOML types that have no JSON counterpart (dates, times, datetimes) are rejected
# at load time rather than failing inside `json.dumps` on the first call.
_JSON_SCALARS = (str, int, float, bool)


class AppManifestError(ValueError):
    """An app folder's `mcp.toml` is missing, unparseable, or invalid (§3).

    Carries a message naming the offending key/value: the manifest is authored by
    hand (or by fused-render's panel), and this error is the only feedback the
    operator gets before the MCP host reports a startup failure.
    """


@dataclass(frozen=True)
class AppTool:
    """One curated tool, read from a `[[tool]]` table (§2).

    ``pinned`` maps a parameter name to a fixed value: it is dropped from the
    tool's derived schema and supplied on every call, so the model can neither
    see it nor override it (§4, §5). ``signature`` is a snapshot fused-render
    captured at curation time for its drift report — **never read here** (§7): the
    served schema is always re-derived from the current source.
    """

    name: str
    description: str
    file: str
    entrypoint: str = DEFAULT_ENTRYPOINT
    pinned: dict[str, Any] = field(default_factory=dict)
    signature: str | None = None

    def target_path(self, app_dir: Path) -> Path:
        """The absolute path of this tool's target `.py` file inside `app_dir`."""
        return app_dir / self.file


@dataclass(frozen=True)
class AppManifest:
    """An app folder's curated tool set, in manifest order (§2).

    Order is preserved rather than sorted: the author curated it, and the order
    they wrote is the order the model sees the tools in.
    """

    app_dir: Path
    tools: tuple[AppTool, ...]


def load_app_manifest(app_dir: Path) -> AppManifest:
    """Read and validate `<app_dir>/mcp.toml` into an `AppManifest` (§2, §3).

    Strict by design (§3): every problem raises `AppManifestError` naming the
    offending value. Unrelated top-level tables are ignored so the file can carry
    other tooling's configuration, but an unrecognised key *inside* a
    `[[tool]]` is also ignored — forward tolerance for a newer authoring tool.
    """
    app_dir = Path(app_dir)
    if not app_dir.is_dir():
        raise AppManifestError(f"{app_dir} is not a directory — nothing to serve.")

    manifest_path = app_dir / MANIFEST_NAME
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppManifestError(
            f"No {MANIFEST_NAME} in {app_dir} — curate the app's tools first "
            f"(fused-render's MCP panel writes it; spec/serve/app-mcp.md §2). [{exc}]"
        ) from exc
    try:
        raw = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise AppManifestError(f"{manifest_path} is not valid TOML: {exc}") from exc

    entries = raw.get("tool")
    if not isinstance(entries, list) or not entries:
        raise AppManifestError(
            f"{manifest_path} declares no [[tool]] tables — an app MCP server "
            "serves exactly the curated tools (spec/serve/app-mcp.md §2)."
        )

    tools: list[AppTool] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AppManifestError(
                f"{manifest_path}: [[tool]] #{index + 1} is not a table "
                f"(got {type(entry).__name__})."
            )
        tool = _tool_from_entry(entry, index, manifest_path, app_dir)
        if tool.name in seen:
            raise AppManifestError(
                f"{manifest_path}: duplicate tool name {tool.name!r} — MCP tool names "
                "must be unique."
            )
        seen.add(tool.name)
        tools.append(tool)

    return AppManifest(app_dir=app_dir, tools=tuple(tools))


def _tool_from_entry(
    entry: dict[Any, Any], index: int, manifest_path: Path, app_dir: Path
) -> AppTool:
    """Validate one `[[tool]]` table into an `AppTool` (§2, §3)."""
    where = f"{manifest_path}: [[tool]] #{index + 1}"

    name = _required_str(entry, "name", where)
    if not name.isidentifier():
        raise AppManifestError(
            f"{where}: name {name!r} is not a Python identifier — a tool name has to "
            "be one (it names the callable the MCP schema is derived from)."
        )
    description = _required_str(entry, "description", where)
    file = _required_str(entry, "file", where)

    entrypoint = entry.get("entrypoint", DEFAULT_ENTRYPOINT)
    if not isinstance(entrypoint, str) or not entrypoint.isidentifier():
        raise AppManifestError(
            f"{where} ({name}): entrypoint {entrypoint!r} is not a Python identifier."
        )

    signature = entry.get("signature")
    if signature is not None and not isinstance(signature, str):
        raise AppManifestError(f"{where} ({name}): signature must be a string.")

    return AppTool(
        name=name,
        description=description,
        file=_validated_target(file, name, where, app_dir),
        entrypoint=entrypoint,
        pinned=_validated_pinned(entry.get("pinned"), name, where),
        signature=signature,
    )


def _required_str(entry: dict[Any, Any], key: str, where: str) -> str:
    """A required non-blank string field, else `AppManifestError` (§3)."""
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AppManifestError(
            f"{where}: {key} is required and must be a non-empty string (got {value!r})."
        )
    return value.strip()


def _validated_target(file: str, name: str, where: str, app_dir: Path) -> str:
    """The `file` field, checked to be a `.py` file existing inside `app_dir` (§3).

    Containment is checked on the *resolved* path so neither an absolute path nor
    a `..` segment can point a served tool at source outside the folder the
    operator registered.
    """
    if not file.endswith(".py"):
        raise AppManifestError(
            f"{where} ({name}): file {file!r} is not a .py file — a tool's target is "
            "a Python entrypoint file."
        )

    candidate = app_dir / file
    try:
        resolved = candidate.resolve()
        root = app_dir.resolve()
    except OSError as exc:
        raise AppManifestError(f"{where} ({name}): file {file!r} is unreadable: {exc}") from exc
    if not resolved.is_relative_to(root):
        raise AppManifestError(
            f"{where} ({name}): file {file!r} resolves outside the app folder "
            f"({resolved}) — a served tool may only run the app's own code."
        )
    if not resolved.is_file():
        raise AppManifestError(f"{where} ({name}): file {file!r} does not exist in {app_dir}.")
    return file


def _validated_pinned(pinned: Any, name: str, where: str) -> dict[str, Any]:
    """The `[tool.pinned]` table, checked to be identifier → JSON value (§3)."""
    if pinned is None:
        return {}
    if not isinstance(pinned, dict):
        raise AppManifestError(
            f"{where} ({name}): pinned must be a table of parameter → value "
            f"(got {type(pinned).__name__})."
        )
    for key, value in pinned.items():
        if not key.isidentifier():
            raise AppManifestError(
                f"{where} ({name}): pinned key {key!r} is not a Python identifier — "
                "a pin names a parameter of the entrypoint."
            )
        if not _is_json_value(value):
            raise AppManifestError(
                f"{where} ({name}): pinned value for {key!r} has no JSON equivalent "
                f"({type(value).__name__}); pins cross the compute boundary as JSON."
            )
    return dict(pinned)


def _is_json_value(value: Any) -> bool:
    """Whether `value` survives `json.dumps` — TOML dates/times do not (§5)."""
    if isinstance(value, bool) or isinstance(value, _JSON_SCALARS) or value is None:
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_value(v) for k, v in value.items())
    return False


# ---------------------------------------------------------------------------
# Tool assembly: the schema is the signature minus the pins (§4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppToolSpec:
    """Everything one curated tool needs at call time (§4, §5).

    ``params`` is the entrypoint's signature **minus** the pinned parameters —
    the schema the model sees. ``code`` is the target file's source wrapped by
    `code_with_file` so it runs with ``__file__`` set (the runner then calls
    ``tool.entrypoint``). All of it is read once at startup: a served
    tool's shape is fixed for the lifetime of the server process, which is also
    what makes a startup error (a vanished entrypoint) surface as a failed
    handshake rather than a runtime surprise.
    """

    tool: AppTool
    params: list[ParamSpec]
    code: str
    app_dir: Path


# The value fused-render's page path gives an entrypoint's ``__name__`` (its
# engine preamble). Copied exactly so a file behaves identically under both: not
# ``"__main__"`` (a ``if __name__ == "__main__":`` block is a script's CLI entry,
# not something a served tool should trigger) and not ``"__module__"``.
_MODULE_NAME = "__fused_module__"


def code_with_file(source: str, path: Path) -> str:
    """`source`, wrapped so it runs with the page path's preamble (§5).

    Three things fused-render's engine gives an entrypoint that the runner's bare
    exec namespace does not, assembled here as one prologue: ``__file__``,
    ``__name__``, and the entrypoint's own directory at the head of ``sys.path``.

    ``__file__`` because an app file locates the app's own data relative to
    itself —
    ``_HERE = os.path.dirname(os.path.abspath(__file__))`` at module top level is
    how every showcase app finds its lake, its config, its assets. The runner
    execs the code it is handed into a bare namespace (``<lambda_exec>``), which
    defines no ``__file__``, so serving the source unwrapped killed such a file
    with ``NameError`` on a tool the page path runs fine — fused-render's engine
    runs the file *as a file*. This closes that parity gap; ``open-mail``'s
    hand-written ``__file__`` fallback exists only because of it.

    The path is the RESOLVED source path under the app folder, never the
    ``/tmp`` sandbox the backend stages the code in: the point is for
    ``dirname`` to land on the app folder, where the data actually is.

    ``__name__`` because a file with an ``if __name__ == "__main__":`` guard
    otherwise raises ``NameError`` at import time — the guard is unreachable
    either way, but only one of the two spellings lets the rest of the file run.

    The entrypoint's directory on ``sys.path`` because ``sys.path[0]`` is the
    backend's sandbox directory, so a bare ``import helpers`` of the module next
    to the entrypoint failed under serve while working on the page. It is
    inserted at the head, and it is the ENTRYPOINT'S OWN directory (the app
    folder itself for a root-level entrypoint, which is the normal shape) —
    exactly what the page path inserts, so a subfolder entrypoint's imports
    resolve the same way under both.

    The working directory is deliberately NOT changed: the runner's cwd is where
    it stages ``_params.json`` and collects output, so it belongs to the backend
    and not to this wrapper. A file that reads a relative path rather than a
    ``__file__``-relative one therefore still behaves differently here than on
    the page — use ``__file__``.

    The source is compiled as its own unit rather than concatenated after the
    prologue, which buys two things prepended lines cannot:

    * a leading ``from __future__ import annotations`` stays the first statement
      of its compilation unit (after an assignment it is a ``SyntaxError``);
    * the app's frames keep their own line numbers and are labelled with the
      real file path, so a traceback the operator reads points at the file they
      wrote — better than the ``<lambda_exec>`` line numbers they got before.

    ``globals()`` is the module namespace the runner execs into, so everything
    the file defines — including ``tool.entrypoint`` — lands where the runner
    looks it up. ``sys`` is reached through ``__import__`` rather than an
    ``import`` statement so the prologue leaves no name of its own in that
    namespace for the app's code to collide with.
    """
    return (
        f"__file__ = {str(path)!r}\n"
        f"__name__ = {_MODULE_NAME!r}\n"
        f"__import__('sys').path.insert(0, {str(path.parent)!r})\n"
        f"exec(compile({source!r}, __file__, 'exec'), globals())\n"
    )


def build_app_tool_spec(manifest: AppManifest, tool: AppTool) -> AppToolSpec:
    """Read `tool`'s target file and derive its served shape (§4).

    Parameters come from a **static AST read** of the entrypoint's def — never
    from importing the module, whose top-level code for these folders means
    touching token files and local state. Pinned parameters are dropped from the
    derived list, so they are invisible to the model and unoverridable by it.
    """
    path = tool.target_path(manifest.app_dir)
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise AppManifestError(
            f"{tool.name}: {tool.file} does not parse as Python ({exc}) — the served "
            "tool's schema is read from its source."
        ) from exc

    fn = funcdef_named(tree, tool.entrypoint)
    if fn is None:
        raise AppManifestError(
            f"{tool.name}: {tool.file} defines no top-level {tool.entrypoint!r} "
            "function — the manifest's entrypoint must exist in the target file."
        )

    params = [p for p in params_from_funcdef(fn) if p.name not in tool.pinned]
    return AppToolSpec(
        tool=tool,
        params=params,
        code=code_with_file(source, path.resolve()),
        app_dir=manifest.app_dir,
    )


def call_params(tool: AppTool, kwargs: dict[str, Any]) -> dict[str, Any]:
    """The `_params.json` payload for one call: caller args **overlaid by** pins (§5).

    Pins are applied last on purpose: a pinned parameter is absent from the
    schema, but a client is free to send whatever JSON it likes, and a pin that a
    fabricated argument could override would be a curation the operator only
    *believes* is enforced.
    """
    params = dict(kwargs)
    params.update(tool.pinned)
    return params


# ---------------------------------------------------------------------------
# Execution: always the local backend (§5)
# ---------------------------------------------------------------------------


async def _app_interpreter(spec: AppToolSpec) -> str:
    """The interpreter the tool's page-side twin runs on: the one resolution
    `engine.run_python` uses (SPEC PY-16/17), so the app-interpreter fast path and
    the macOS bundle's wrapper interpreter apply here too. A tool cannot show the
    install loader, so an environment that is not installed yet is an error that
    says so."""
    from fused_render import engine

    interpreter, unavailable = await engine.resolve_interpreter(
        str(spec.tool.target_path(spec.app_dir))
    )
    if interpreter is None:
        detail = (unavailable or {}).get("error") or {}
        raise RuntimeError(
            f"app tool {spec.tool.name!r} cannot run: "
            f"{detail.get('message') or 'its Python environment is not available'}"
            " Open the app in fused-render once to install its environment."
        )
    return interpreter


async def _run_app_tool(spec: AppToolSpec, kwargs: dict[str, Any]) -> Any:
    """Execute one curated tool locally and return the entrypoint's value (§5).

    Threads ``entrypoint`` so the runner looks the name up in the executed
    module's namespace and binds ``_params.json`` to it: an app file needs neither
    a ``result`` variable nor a ``@fused.udf`` decorator. A failed run raises so
    the MCP client sees the error instead of a silent null. Nothing is cached:
    these tools act, and a memoized action is a wrong answer.
    """
    from fused_render import engine

    # The engine's one backend: each backend owns an invoke dispatcher (a temp dir
    # and a watcher thread) that nothing stops, so a backend per call would leak
    # both for the life of a long-running server.
    backend = engine.get_backend()
    params = call_params(spec.tool, kwargs)
    input_files = {"_params.json": json.dumps(params, sort_keys=True, default=str).encode()}
    execution = await asyncio.to_thread(
        backend._execute_sync,
        code=spec.code,
        input_files=input_files,
        interpreter=await _app_interpreter(spec),
        entrypoint=spec.tool.entrypoint,
    )

    if execution.error:
        raise RuntimeError(f"app tool {spec.tool.name!r} failed: {execution.error}")

    rv = execution.return_value
    if isinstance(rv, str):
        try:
            rv = json.loads(rv)
        except (ValueError, TypeError):
            pass
    return rv


def create_app_mcp(app_dir: Path) -> FastMCP:
    """Assemble the stdio MCP server for one app folder (§1, §4).

    Exactly one tool per `[[tool]]` in `<app_dir>/mcp.toml`, in manifest order.
    Raises `AppManifestError` for a missing/invalid manifest or a target file
    whose entrypoint has gone — before any MCP handshake, so the host reports a
    startup failure rather than an empty tool list (§3, §6). Raises
    `McpExtraMissingError` first when the `mcp` extra is not installed.
    """
    fastmcp_cls = load_fastmcp()
    manifest = load_app_manifest(app_dir)

    server = fastmcp_cls(
        f"fused-app:{manifest.app_dir.name}",
        instructions=(
            f"Curated tools from the local app {manifest.app_dir.name!r}. Each one "
            "runs the app's own Python on this machine, with the author's "
            "credentials (spec/serve/app-mcp.md)."
        ),
    )
    for tool in manifest.tools:
        spec = build_app_tool_spec(manifest, tool)
        callable_ = synthesize_callable(
            spec.params,
            name=tool.name,
            doc=tool.description,
            handler=lambda kwargs, spec=spec: _run_app_tool(spec, kwargs),
        )
        server.add_tool(callable_, name=tool.name, description=tool.description)
    return server
