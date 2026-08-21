"""Executing a workflow — the compiler and the progress reader (SPEC §44 / D401).

**The graph is the plan; Claude is the runtime.** There is no step engine here.
`start` turns the `.workflow.json` document into two artefacts and hands both to
one headless `claude -p` session:

1. an `--mcp-config` naming ONE stdio server per distinct app folder the graph
   touches — `{command: <the exported fused>, args: ["app", "serve", <folder>]}`,
   which is the same command `mcp/template.html` registers with the user's Claude
   host (MC-5), pointed at a run-local config instead of `~/.claude.json`;
2. a prompt that states the steps in order, each step's tool, its fixed inputs,
   which of its inputs come from the step before, and the condition on each
   branch — plus the instruction to call those tools in that order and reshape
   the outputs between them.

That division is deliberate. The interesting part of chaining two tools is
never the sequencing, it is the reshaping: `search_mail` answers a list of
messages and `send_mail` wants a `to` and a `body`, and no edge annotation the
user could draw would specify that mapping. A model is the right thing to put in
that gap, so the graph pins down everything a model should not be improvising —
which tools, in what order, with which values fixed — and leaves the gap itself
to it.

**A human clicking Run is the entire approval model**, and MC-6's reasoning
carries over unchanged: every tool in the graph is one the user curated into an
app's `mcp.toml` themselves, and the person who wired them into this chain is the
person a per-call prompt would ask. Nothing here adds a second gate that would
only be a false sense of one.

## Progress comes from the tool records, never from the narration

`poll` derives each node's state from the real `tool_use` / `tool_result` records
in the run's stream-json, joined by `tool_use_id`. It does NOT read the model's
prose, and that is the whole point: a model narrating "step 2 complete" is a
claim, while a `tool_result` for `mcp__open-mail__search_mail` is a fact about
what the machine did. The one heuristic is which NODE a call belongs to when two
nodes call the same tool — matched in plan order against the not-yet-started
nodes for that tool, which is right for the ordinary case and stated here rather
than hidden, because a graph that calls one tool twice can attribute the second
call to the wrong node if the model runs them out of order.

The plan is snapshotted into the run dir at `start`. `poll` reads THAT, never the
`.workflow.json` — the user can edit and save the document while a run is in
flight, and a progress readout that re-derived its node list from the live file
would start attributing a running call to whatever now sits at that index.

## Where observed outputs land, and the deviation

Each node's actual returned output comes back on `poll` as a shape summary
(`{kind, keys, sample}`). The handoff for this work asked for those to be written
into the app's `mcp.toml` as a per-`[[tool]]` output schema; they are recorded in
the **workflow document** instead, as `nodes[].observedOutput`, and returned to
the page to merge and Save.

Two reasons, and the first is the one that decided it. `mcp.toml` belongs to the
app folder and `mcp/manifest.py` is its single writer, with a validate-render-
reparse-verify contract built around replacing the whole `[[tool]]` array; a
merge from this module would either duplicate that contract (a second renderer
for a file whose failure mode is silent corruption of the user's own file) or
reach across template folders to import it, which is not a convention this tree
has. Second, a workflow legitimately touches app folders the user never opened —
they came out of the file index — and a run silently rewriting a manifest in a
folder somebody is not looking at is not a thing to do without being asked.
Recording on the document keeps the writer single (the page) and keeps the
observation next to the node it was observed for.

## Cross-platform, and never a PATH guess for `fused`

The `fused` command comes from `appenv.fused_cli_dir()` (**D334** / MC-5a) and
never from `shutil.which("fused")`. The config this writes is executed later, by
another process, with no one watching: on a machine whose venv lacks the `[fused]`
extra but whose PATH carries some other `fused`, a PATH lookup would wire the
graph to an unvetted binary with no `FUSED_ENV` and fail where the user cannot
see it. When the var is absent, Run is refused with the reason stated. `claude`
itself IS looked up on PATH — it is spawned right here, immediately, and its
absence surfaces instantly.

stdlib only, plus `../shared/appenv.py` and `../shared/private_dir.py`. Nothing
here imports fused_render (SPEC PY-15 / D166), and nothing imports
`claude/agent.py` — the spawn below is modelled on its `_start` (the detach
flags, the 0700 run dir, the stream-json log) and is a focused re-statement of
it, not a reuse.

Actions:
  main(action="start",  path=<abs .workflow.json>) -> {ok, runId, nodes:[...]}
  main(action="poll",   runId=...)                 -> {ok, done, error, nodes:[...]}
  main(action="cancel", runId=...)                 -> {ok, cancelled}
Anything wrong is a refusal payload (`{ok: false, reason, message}`), never an
exception.
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

# How much of one tool result is kept for the page. A node's output is shown as
# evidence of what happened, not as a data viewer, and a single MCP result can be
# a megabyte of mail bodies.
_OUTPUT_CAP = 8000

# How many bytes of the stream-json log `poll` reads per call. A long run's log
# grows without bound and `poll` runs on a timer; the tail is where the progress
# is. Generous enough that a whole ordinary run fits.
_LOG_CAP = 8 * 1024 * 1024

# Caps on the compiled plan. A prompt is a finite thing and a canvas is not a
# programming language: a graph past this is not a workflow, and stuffing it into
# one `-p` argument would blow past what the session can act on reliably.
_MAX_NODES = 40
_MAX_EDGES = 200

# How many keys of an observed object are recorded. The point is the SHAPE, so a
# result with two hundred keys is answered with the first few and the count.
_MAX_OBSERVED_KEYS = 40
# How much of a result is kept as the `sample` in an observed shape.
_SAMPLE_CAP = 400

# Detach the run so it outlives this executor subprocess. `start_new_session`
# (setsid) is POSIX-only — Windows ignores it silently, where DETACHED_PROCESS +
# CREATE_NEW_PROCESS_GROUP is the equivalent. Only the taken branch is evaluated,
# so the win32-only constants are never touched on POSIX. Mirrors
# `claude/agent.py::_DETACH`.
_DETACH = (
    {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    if os.name == "nt" else {"start_new_session": True}
)


def _shared(name: str):
    """Import a module out of `../shared`. Guarded insert; exec'd standalone."""
    shared = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    return __import__(name)


def _ok(**extra) -> dict:
    out = {"ok": True}
    out.update(extra)
    return out


def _refuse(reason: str, message: str) -> dict:
    """A refusal payload — the panel renders it (never an exception)."""
    return {"ok": False, "reason": reason, "message": message}


# ---------------------------------------------------------------------------
# the run directory
# ---------------------------------------------------------------------------


def _runs_root() -> str:
    """Where run dirs live: a per-user tree under the shared temp root.

    Per-user for the reason `claude/agent.py::_runs_root` records at length: at
    0700 a single shared root would let the first account to run a workflow lock
    every other one out, and anything looser is either world-writable or a
    disclosure. A uid suffix dissolves the conflict. POSIX-only suffix —
    `geteuid` does not exist on Windows, whose temp dir is already per-user.

    A SEPARATE root from the chat template's, not a subdirectory of it: these
    runs have a different lifetime and a different reader, and sharing a tree
    would mean one template's pruning policy silently governing the other's.
    """
    geteuid = getattr(os, "geteuid", None)
    suffix = "-%d" % geteuid() if geteuid is not None else ""
    return os.path.join(tempfile.gettempdir(),
                        "fused_render_workflow" + suffix, "runs")


RUNS = _runs_root()


def _private_dir(path: str) -> None:
    """`shared/private_dir.private_dir`, anchored at our own root.

    Anything from `dirname(RUNS)` downwards is vouched for before being built
    on; above it is the system temp root. The threat is the one that module
    documents: our path under a world-writable root is PREDICTABLE, so another
    local account can pre-create it and own the parent of every run dir — and
    the parent is enough, because 0700 on a directory somebody else can rename
    aside protects nothing.
    """
    _shared("private_dir").private_dir(path, os.path.dirname(RUNS))


def _private_open(path: str):
    """`open(path, "w")` creating the file `rw-------` whatever the umask is.

    Belt and braces beside the 0700 directory: the mode is set by the create
    itself, so the run's transcript is never briefly world-readable. The prompt
    and every tool result the model sees pass through these files.
    """
    return os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
        "w", encoding="utf-8")


def _run_dir(run_id: str) -> str:
    """The directory for `run_id`, or `""` when the id is not one we made.

    The id is generated here and travels through a URL param, so it comes back
    as whatever the page sends. A basename check is the whole guard: no
    separator, no `..`, nothing that could walk out of the runs root — a
    `run_id` of `../../etc` would otherwise have `cancel` sending a signal to a
    pid read out of an arbitrary file.
    """
    if not run_id or run_id != os.path.basename(run_id) or run_id.startswith("."):
        return ""
    if not re.fullmatch(r"[0-9a-zA-Z-]+", run_id):
        return ""
    return os.path.join(RUNS, run_id)


# ---------------------------------------------------------------------------
# reading the document and resolving it against the machine
# ---------------------------------------------------------------------------


def _load_document(path: str):
    """`(doc, refusal)` for a `.workflow.json`.

    An EMPTY file is an empty workflow, not a parse error — that is the shape
    the explorer's generic New File… leaves behind and the whole creation path
    for this mode (`condition.py`). It refuses to RUN, of course, but it refuses
    as "no steps" rather than as "corrupt".
    """
    if not path or not os.path.isfile(path):
        return None, _refuse("not_a_file", "%r is not a file." % (path,))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return None, _refuse("unreadable", "cannot read %s: %s" % (path, exc))
    if not text.strip():
        return {"nodes": [], "edges": []}, None
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return None, _refuse(
            "bad_document",
            "%s does not parse as JSON (%s). Fix it by hand — running it would "
            "mean guessing what you meant." % (os.path.basename(path), exc))
    if not isinstance(doc, dict):
        return None, _refuse(
            "bad_document",
            "%s must be a JSON object with `nodes` and `edges`."
            % os.path.basename(path))
    return doc, None


def _catalog():
    """Every curated tool on the machine, keyed by `(realpath(app), tool)`.

    Loaded from `discover.py` beside this module — the SAME reader the palette
    draws from, so a node that resolves in the canvas resolves here. Two readers
    of `mcp.toml` would be two answers to "does this tool exist", and the one
    that matters is the one that decides whether Run is allowed.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_workflow_discover", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "discover.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = module.main("tools")
    if not out.get("ok"):
        return None, out
    table = {}
    for app in out.get("apps") or []:
        for tool in app.get("tools") or []:
            try:
                key = (os.path.normcase(os.path.realpath(app["folder"])), tool["name"])
            except OSError:
                continue
            table[key] = tool
    return {"tools": table, "fusedCli": out.get("fusedCli") or ""}, None


def _server_names(folders: list) -> dict:
    """`{folder: mcp server name}` — one server per DISTINCT app folder.

    Named after the folder, because that is what the name is read as in the
    run's own config and in every `mcp__<server>__<tool>` string `poll` matches
    against. Sanitized because it reaches a JSON key the CLI parses into a
    server id, and DEDUPLICATED with a numeric suffix because two app folders
    with the same basename (`showcase/mail` and `work/mail`) are entirely
    ordinary — and two servers under one name would silently make half the
    graph's tools unreachable.
    """
    out, used = {}, set()
    for folder in folders:
        base = re.sub(r"[^A-Za-z0-9_-]+", "-", os.path.basename(folder.rstrip(os.sep)))
        base = base.strip("-") or "app"
        name, n = base, 2
        while name in used:
            name, n = "%s-%d" % (base, n), n + 1
        used.add(name)
        out[folder] = name
    return out


def _order(nodes: list, edges: list) -> list:
    """The node ids in execution order: a topological sort, stable on document order.

    The graph is a chain WITH BRANCHING, so this is a linearisation and not a
    schedule — a branch's condition is what decides whether its step runs, and
    the order only decides what the prompt says to try first. Kahn's algorithm
    over the incoming-edge count, taking ready nodes in document order so the
    order the user laid out on the canvas is the order they read back.

    A CYCLE leaves nodes with a permanent incoming edge. They are appended in
    document order rather than dropped: the refusal for a cycle is the caller's
    (`_compile` names it), and silently omitting the nodes would make that
    refusal describe a graph the user cannot see.
    """
    ids = [n["id"] for n in nodes]
    incoming = {i: 0 for i in ids}
    outgoing = {i: [] for i in ids}
    for edge in edges:
        src, dst = edge.get("from"), edge.get("to")
        if src in incoming and dst in incoming:
            incoming[dst] += 1
            outgoing[src].append(dst)
    ready = [i for i in ids if incoming[i] == 0]
    out = []
    while ready:
        current = ready.pop(0)
        out.append(current)
        for nxt in outgoing[current]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                # Re-inserted in document order rather than appended, so the
                # result is the user's own ordering wherever the graph does not
                # constrain it.
                ready.append(nxt)
                ready.sort(key=ids.index)
    if len(out) < len(ids):
        out += [i for i in ids if i not in set(out)]
    return out


def _compile(doc: dict):
    """`(plan, refusal)` — the document resolved against this machine.

    Everything that can make a run fail late is turned into a refusal here,
    naming the node: an unknown tool, an app folder that has gone, a cycle, a
    graph too big to state in one prompt. A run that starts and then dies inside
    a detached process is a run whose failure the user reads as the model's.
    """
    catalog, refusal = _catalog()
    if catalog is None:
        return None, refusal

    raw_nodes = doc.get("nodes")
    raw_nodes = raw_nodes if isinstance(raw_nodes, list) else []
    nodes = [n for n in raw_nodes if isinstance(n, dict) and n.get("id")]
    if not nodes:
        return None, _refuse(
            "empty", "This workflow has no steps yet — add a tool from the palette.")
    if len(nodes) > _MAX_NODES:
        return None, _refuse(
            "too_big",
            "This workflow has %d steps; %d is the most one run can describe."
            % (len(nodes), _MAX_NODES))
    raw_edges = doc.get("edges")
    edges = [e for e in (raw_edges if isinstance(raw_edges, list) else [])
             if isinstance(e, dict)]
    if len(edges) > _MAX_EDGES:
        return None, _refuse(
            "too_big", "This workflow has %d connections; %d is the maximum."
            % (len(edges), _MAX_EDGES))

    folders = []
    for node in nodes:
        folder = str(node.get("app") or "")
        if not folder:
            return None, _refuse(
                "unresolved",
                "Step %r names no app folder — re-add it from the palette."
                % (node.get("label") or node.get("id"),))
        if folder not in folders:
            folders.append(folder)
    servers = _server_names(folders)

    steps = []
    for node in nodes:
        folder = str(node["app"])
        tool_name = str(node.get("tool") or "")
        try:
            key = (os.path.normcase(os.path.realpath(folder)), tool_name)
        except OSError:
            key = (folder, tool_name)
        tool = catalog["tools"].get(key)
        if tool is None:
            return None, _refuse(
                "unresolved",
                "Step %r calls %r in %s, and that folder no longer declares a tool "
                "by that name. Re-curate the app in its MCP panel, or re-add the "
                "step from the palette."
                % (node.get("label") or node.get("id"), tool_name, folder))
        exposed = node.get("inputs")
        exposed = exposed if isinstance(exposed, list) else []
        literals, from_previous = [], []
        known = {p["name"] for p in tool.get("params") or []}
        for item in exposed:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            # A parameter the tool no longer has is DROPPED rather than refused:
            # the entrypoint's signature drifted under a saved workflow, which is
            # MC-4's situation, and the served tool keeps working. Sending a name
            # the schema does not carry is what would fail the call.
            if not name or name not in known:
                continue
            if item.get("source") == "previous":
                from_previous.append(name)
            else:
                literals.append((name, item.get("value", "")))
        steps.append({
            "id": str(node["id"]),
            "label": str(node.get("label") or "") or tool_name,
            "app": folder,
            "server": servers[folder],
            "tool": tool_name,
            "mcpName": "mcp__%s__%s" % (servers[folder], tool_name),
            "description": tool.get("description") or "",
            "literals": literals,
            "fromPrevious": from_previous,
        })

    by_id = {s["id"]: s for s in steps}
    order = _order(nodes, edges)
    # A cycle means `_order` could not place every node by its edges. Refused
    # rather than linearised, because "run these in this order" would be a claim
    # about a graph that does not have one.
    placed = set()
    for edge in edges:
        if edge.get("from") in by_id and edge.get("to") in by_id:
            placed.add((edge["from"], edge["to"]))
    if _has_cycle(list(by_id), placed):
        return None, _refuse(
            "cycle",
            "This workflow loops back on itself. A run is a finite sequence of "
            "steps, so remove the connection that closes the loop.")

    incoming = {}
    for src, dst in placed:
        incoming.setdefault(dst, []).append(src)
    for step in steps:
        step["after"] = incoming.get(step["id"], [])
        step["conditions"] = [
            {"from": e["from"], "text": str(e.get("condition") or "").strip()}
            for e in edges
            if e.get("to") == step["id"] and e.get("from") in by_id
            and str(e.get("condition") or "").strip()
        ]

    return {
        "name": str(doc.get("name") or "").strip(),
        "steps": [by_id[i] for i in order if i in by_id],
        "servers": {servers[f]: f for f in folders},
        "fusedCli": catalog["fusedCli"],
    }, None


def _has_cycle(ids, edges) -> bool:
    """Whether the directed graph `(ids, edges)` contains a cycle.

    Kahn again, but asking the other question: peel nodes with no remaining
    incoming edge, and a non-empty remainder is a cycle. Separate from `_order`
    so that function can stay total — it must return every node even for a graph
    this one rejects, since the refusal describes them.
    """
    incoming = {i: 0 for i in ids}
    outgoing = {i: [] for i in ids}
    for src, dst in edges:
        incoming[dst] += 1
        outgoing[src].append(dst)
    ready = [i for i in ids if incoming[i] == 0]
    seen = 0
    while ready:
        current = ready.pop()
        seen += 1
        for nxt in outgoing[current]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
    return seen < len(ids)


# ---------------------------------------------------------------------------
# the two artefacts
# ---------------------------------------------------------------------------


def _mcp_config(plan: dict) -> dict:
    """One stdio MCP server per distinct app folder.

    `fused app serve <folder>` publishes that folder's `[[tool]]` tables and
    NOTHING ELSE — the app itself is not running, and the manifest is the entire
    contract (D401). So the config's blast radius is exactly the set of tools the
    user curated in the folders this graph touches: a workflow cannot reach a
    tool it does not name, because the server that would serve it is not in the
    config.
    """
    return {"mcpServers": {
        name: {"command": plan["fusedCli"], "args": ["app", "serve", folder]}
        for name, folder in plan["servers"].items()}}


def _prompt(plan: dict) -> str:
    """The steps, as instructions.

    Written as a numbered list rather than as prose because the list IS the
    graph: each item names one tool, the values the user fixed, the values the
    model has to derive from the step before, and the condition on getting there
    at all. What is deliberately NOT specified is how to turn one step's output
    into the next step's inputs — that is the gap a model is in the plan to
    fill, and over-specifying it would just be writing the workflow twice.

    The closing rules are the ones that keep a run legible afterwards: no tools
    beyond the named ones (so `poll`'s attribution means something), and a stop
    rather than an improvisation when a step fails (so a half-run reads as a
    half-run instead of as a success).
    """
    lines = []
    title = plan["name"] or "this workflow"
    lines.append("Execute the saved workflow %r. Do exactly the steps below, in "
                 "order, and nothing else." % title)
    lines.append("")
    lines.append("STEPS")
    for i, step in enumerate(plan["steps"], 1):
        lines.append("")
        lines.append("%d. %s — call the MCP tool `%s`." % (i, step["label"], step["mcpName"]))
        if step["description"]:
            lines.append("   What it does: %s" % step["description"])
        for cond in step["conditions"]:
            prior = next((n for n, s in enumerate(plan["steps"], 1)
                          if s["id"] == cond["from"]), None)
            lines.append("   Run this step ONLY IF, judging from step %s's output: %s"
                         % (prior if prior else "the previous", cond["text"]))
            lines.append("   If that is not true, skip this step and every step that "
                         "depends on it, and say so at the end.")
        if step["literals"]:
            lines.append("   Send exactly these argument values:")
            for name, value in step["literals"]:
                lines.append("     %s = %s" % (name, json.dumps(value)))
        if step["fromPrevious"]:
            lines.append("   Derive these arguments from the OUTPUT of the step(s) "
                         "before it, reshaping as needed:")
            for name in step["fromPrevious"]:
                lines.append("     %s" % name)
        if not step["literals"] and not step["fromPrevious"]:
            lines.append("   Send no arguments — the tool's own defaults are what "
                         "this step wants.")
        lines.append("   Send no other arguments: every parameter not listed above "
                     "is deliberately left at its default.")
    lines.append("")
    lines.append("RULES")
    lines.append("- Call the tools named above, in the order given. Do not call any "
                 "other tool, and do not read or write files.")
    lines.append("- Between steps, reshape the previous step's output yourself into "
                 "the arguments the next step names. That reshaping is your job; "
                 "the steps are not.")
    lines.append("- If a step's tool returns an error, STOP. Do not retry it with "
                 "different arguments and do not work around it — report which step "
                 "failed and what it said.")
    lines.append("- Do not ask questions. This run has nobody to answer them: the "
                 "person approved the whole workflow by starting it.")
    lines.append("- Finish with one short paragraph saying which steps ran and what "
                 "each returned. The progress readout comes from the tool calls "
                 "themselves, so do not narrate progress as you go.")
    return "\n".join(lines)


def _claude_bin() -> str:
    """Path to the `claude` executable, or `""`.

    `FUSED_RENDER_CLAUDE_BIN` (the same override `claude/agent.py` honours, so a
    machine configured for one template is configured for both) beats PATH. `""`
    rather than a raise: "there is no Claude CLI" is a sentence for the panel to
    render beside a disabled Run button, not a traceback.

    PATH is legitimate HERE and not for `fused` (D334): this binary is spawned by
    this process, right now, so a wrong answer surfaces immediately — whereas the
    `fused` path is written into a config another process executes later, where a
    stray shim fails unattended.
    """
    import shutil

    override = os.environ.get("FUSED_RENDER_CLAUDE_BIN")
    if override and os.path.isfile(override):
        return override
    return shutil.which("claude") or ""


def _fused_bin(cli_dir_bin: str) -> str:
    """The `fused` executable inside the dir the server exported, or `""`.

    `discover.py` already joined `fused_cli_dir()` with the POSIX name; Windows
    puts a `.cmd` or `.bat` shim there instead, so the siblings are tried before
    giving up. Existence is CHECKED rather than assumed because the value is
    about to be baked into a config a detached process runs: a command that is
    not there fails inside the session, where the user cannot see why.
    """
    if not cli_dir_bin:
        return ""
    if os.path.isfile(cli_dir_bin):
        return cli_dir_bin
    for suffix in (".cmd", ".bat", ".exe"):
        if os.path.isfile(cli_dir_bin + suffix):
            return cli_dir_bin + suffix
    return ""


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _start(path: str, model: str) -> dict:
    doc, refusal = _load_document(path)
    if refusal is not None:
        return refusal
    plan, refusal = _compile(doc)
    if refusal is not None:
        return refusal

    fused_bin = _fused_bin(plan["fusedCli"])
    if not fused_bin:
        return _refuse(
            "no_fused_cli",
            "This workflow's steps are served by `fused app serve`, and no `fused` "
            "command was exported by the server — so the MCP servers it needs "
            "cannot be started. Install fused-render's `[fused]` extra and restart "
            "the app.")
    plan["fusedCli"] = fused_bin
    claude_bin = _claude_bin()
    if not claude_bin:
        return _refuse(
            "no_claude_cli",
            "Claude is the runtime for a workflow, and the `claude` CLI is not on "
            "this machine's PATH. Install Claude Code, or set "
            "FUSED_RENDER_CLAUDE_BIN to its full path.")

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    run_dir = os.path.join(RUNS, run_id)
    _private_dir(run_dir)

    config_path = os.path.join(run_dir, "mcp.json")
    with _private_open(config_path) as fh:
        json.dump(_mcp_config(plan), fh, indent=2)
    prompt = _prompt(plan)
    # The prompt is written to the run dir as well as passed on argv. It is the
    # record of what was actually asked — the document can be edited a second
    # later, and without this there is nothing to compare a surprising run
    # against.
    with _private_open(os.path.join(run_dir, "prompt.txt")) as fh:
        fh.write(prompt)
    # The PLAN SNAPSHOT, and `poll` reads only this. The user may edit and save
    # the workflow while the run is in flight; a readout that re-derived its node
    # list from the live document would start attributing a running call to
    # whatever now sits at that index.
    with _private_open(os.path.join(run_dir, "plan.json")) as fh:
        json.dump({"path": os.path.abspath(path), "name": plan["name"],
                   "steps": plan["steps"], "servers": plan["servers"]}, fh)

    cmd = [claude_bin, "-p", prompt,
           "--output-format", "stream-json", "--verbose",
           "--mcp-config", config_path,
           # Every tool in this graph is one the user curated and then wired
           # together and then clicked Run on — MC-6's argument, unchanged. A
           # headless session has nobody to answer a permission prompt, so
           # without this the run would simply hang on the first call; the
           # allowance is NARROW BY CONSTRUCTION, naming each tool this plan
           # actually calls rather than a wildcard, so the session cannot reach
           # a tool the workflow does not contain even though its server offers
           # one.
           "--allowed-tools", ",".join(sorted({s["mcpName"] for s in plan["steps"]}))]
    if model:
        cmd += ["--model", model]

    # The session must not inherit an ambient FUSED_ENV: the `fused` wrapper
    # only DEFAULTS it when unset, so a value the server happened to be launched
    # with would look exactly like a deliberate choice and silently change which
    # environment every served tool resolves. Same reason and same fix as
    # `claude/agent.py::_start`.
    spawn_env = os.environ.copy()
    spawn_env.pop("FUSED_ENV", None)
    try:
        with _private_open(os.path.join(run_dir, "out.jsonl")) as out, \
             _private_open(os.path.join(run_dir, "err.log")) as err:
            proc = subprocess.Popen(
                cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                # cwd is the workflow document's own folder — the session has no
                # file tools allowed, so this only decides which CLAUDE.md and
                # project settings apply, and the document's folder is the one
                # the user is looking at.
                cwd=os.path.dirname(os.path.abspath(path)) or None,
                env=spawn_env, **_DETACH)
    except OSError as exc:
        return _refuse("spawn_failed", "could not start claude: %s" % exc)
    with _private_open(os.path.join(run_dir, "pid")) as fh:
        fh.write(str(proc.pid))

    return _ok(runId=run_id,
               nodes=[{"id": s["id"], "label": s["label"], "tool": s["tool"]}
                      for s in plan["steps"]],
               servers=plan["servers"])


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… [%d more characters]" % (len(text) - limit)


def _result_text(block: dict) -> str:
    """The text of one `tool_result` block.

    `content` is a plain STRING for most tools and a list of typed blocks for the
    ones that return richer payloads — both shapes are on the wire, so both are
    read here rather than at each call site. Anything that is not text is
    ignored: a node's output is shown as evidence of what the tool returned, and
    this panel has nowhere to put an image.
    """
    content = block.get("content")
    if isinstance(content, str):
        return content
    parts = []
    for sub in content if isinstance(content, list) else []:
        if isinstance(sub, dict) and sub.get("type") == "text" and isinstance(
                sub.get("text"), str):
            parts.append(sub["text"])
    return "\n".join(parts)


def _observed(text: str) -> dict:
    """The SHAPE of one tool result: `{kind, keys, count, sample}`.

    Recorded so a workflow can tell the user what a step actually returns, which
    is the fact nothing else on this surface has: an MCP tool's declared input
    schema says nothing about its output, and `search_mail` and `send_mail` share
    one entrypoint while returning entirely different things. Shape and not
    contents — keys and a short sample — because it is written back into a
    document the user commits, and a step's real output is their mail.

    Anything that is not JSON is `kind: "text"`, which is a true statement about
    a tool that answers prose rather than a failure to parse one that does not.
    """
    stripped = (text or "").strip()
    if not stripped:
        return {"kind": "empty", "keys": [], "count": 0, "sample": ""}
    try:
        value = json.loads(stripped)
    except ValueError:
        return {"kind": "text", "keys": [], "count": 0,
                "sample": _cap(stripped, _SAMPLE_CAP)}
    if isinstance(value, dict):
        keys = [str(k) for k in list(value)[:_MAX_OBSERVED_KEYS]]
        return {"kind": "object", "keys": keys, "count": len(value),
                "sample": _cap(stripped, _SAMPLE_CAP)}
    if isinstance(value, list):
        first = value[0] if value else None
        keys = ([str(k) for k in list(first)[:_MAX_OBSERVED_KEYS]]
                if isinstance(first, dict) else [])
        return {"kind": "array", "keys": keys, "count": len(value),
                "sample": _cap(stripped, _SAMPLE_CAP)}
    return {"kind": type(value).__name__, "keys": [], "count": 0,
            "sample": _cap(stripped, _SAMPLE_CAP)}


def _alive(run_dir: str) -> bool:
    """Whether the run's process is still around."""
    try:
        with open(os.path.join(run_dir, "pid"), "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return False
    return bool(_shared("procutil").pid_alive(pid))


def _poll(run_id: str) -> dict:
    run_dir = _run_dir(run_id)
    if not run_dir or not os.path.isdir(run_dir):
        return _refuse("unknown_run", "No run %r — it may have been cleaned up." % (run_id,))
    try:
        with open(os.path.join(run_dir, "plan.json"), "r", encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        return _refuse("unknown_run", "run %s has no readable plan (%s)." % (run_id, exc))

    steps = plan.get("steps") or []
    state = {s["id"]: {"id": s["id"], "label": s["label"], "tool": s["tool"],
                       "status": "pending", "input": None, "output": "",
                       "observedOutput": None, "error": ""}
             for s in steps}

    try:
        with open(os.path.join(run_dir, "out.jsonl"), "r", encoding="utf-8",
                  errors="replace") as fh:
            log = fh.read(_LOG_CAP)
    except OSError:
        log = ""

    # tool_use id -> node id. The join to the result is by `tool_use_id` and
    # never by position: tools can answer out of call order, and a positional
    # join would then paste one node's output onto another's row.
    call_owner = {}
    by_mcp_name = {}
    for step in steps:
        by_mcp_name.setdefault(step["mcpName"], []).append(step["id"])

    done = False
    error = ""
    for line in log.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # A truncated tail is normal: the log is being appended to while this
            # reads it. Skipping the partial line is right; failing on it would
            # make the readout flicker into an error once per poll.
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        if kind == "result":
            done = True
            if row.get("is_error"):
                error = str(row.get("result") or "the run reported an error")
            continue
        message = row.get("message")
        blocks = (message or {}).get("content") if isinstance(message, dict) else None
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                # WHICH NODE THIS CALL BELONGS TO. Matched in plan order against
                # the not-yet-started nodes that call this tool. Exact whenever a
                # tool appears once in the graph, which is the ordinary case; a
                # graph that calls one tool from two nodes can attribute the
                # second call to the wrong node if the model runs them out of
                # order. Stated rather than hidden — the alternative would be
                # asking the model to tag its calls, i.e. trusting narration for
                # the one thing this readout exists to not trust.
                candidates = by_mcp_name.get(str(block.get("name") or ""), [])
                node_id = next(
                    (i for i in candidates if state[i]["status"] == "pending"), None)
                if node_id is None:
                    continue
                state[node_id]["status"] = "running"
                state[node_id]["input"] = block.get("input")
                call_owner[str(block.get("id") or "")] = node_id
            elif block.get("type") == "tool_result":
                node_id = call_owner.get(str(block.get("tool_use_id") or ""))
                if node_id is None:
                    continue
                text = _result_text(block)
                node = state[node_id]
                if block.get("is_error"):
                    node["status"] = "error"
                    node["error"] = _cap(text, _OUTPUT_CAP)
                else:
                    node["status"] = "done"
                    node["output"] = _cap(text, _OUTPUT_CAP)
                    node["observedOutput"] = _observed(text)

    if not done and not _alive(run_dir):
        # The process is gone with no `result` row: it died rather than
        # finished. Said as its own sentence, because "done with nothing to
        # show" and "crashed" look identical on the readout otherwise.
        done = True
        if not error:
            try:
                with open(os.path.join(run_dir, "err.log"), "r", encoding="utf-8",
                          errors="replace") as fh:
                    stderr = fh.read(_OUTPUT_CAP).strip()
            except OSError:
                stderr = ""
            error = stderr or "the run ended without reporting a result"
    if done:
        # A node left `running` when the run is over never got its result.
        for node in state.values():
            if node["status"] == "running":
                node["status"] = "error"
                node["error"] = node["error"] or "the run ended before this step answered"

    return _ok(done=done, error=error, runId=run_id,
               nodes=[state[s["id"]] for s in steps])


def _cancel(run_id: str) -> dict:
    """Stop a run. Best-effort, and honest about which."""
    run_dir = _run_dir(run_id)
    if not run_dir or not os.path.isdir(run_dir):
        return _refuse("unknown_run", "No run %r." % (run_id,))
    try:
        with open(os.path.join(run_dir, "pid"), "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return _ok(cancelled=False, message="the run has no recorded process.")
    try:
        if os.name == "nt":
            # No POSIX process groups on Windows; the detach flags put the child
            # in its own group, and CTRL_BREAK is what reaches it.
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            # The whole session, not just the pid: `start_new_session` made this
            # process a group leader, and the MCP servers it spawned are its
            # children — signalling only the leader leaves them running.
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, AttributeError, ProcessLookupError) as exc:
        return _ok(cancelled=False, message="could not signal the run: %s" % exc)
    return _ok(cancelled=True)


def main(action: str = "start", path: str = "", runId: str = "",
         model: str = "") -> dict:
    """Start, watch, or stop a workflow run.

    Anything wrong is a refusal payload (`{ok: false, reason, message}`), never
    an exception — the panel renders the reason, and a raise would be a red
    traceback overlay in place of a sentence.
    """
    try:
        if action == "start":
            return _start(path, model)
        if action == "poll":
            return _poll(runId)
        if action == "cancel":
            return _cancel(runId)
        return _refuse(
            "unknown_action",
            "unknown action %r — expected 'start', 'poll' or 'cancel'." % (action,))
    except Exception as exc:  # noqa: BLE001 — the module's contract is a payload
        return _refuse("failed", "%s: %s" % (type(exc).__name__, exc))
