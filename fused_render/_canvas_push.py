"""Answer a `fused workbench canvas push` / `pull` of a CANVAS CLONE itself.

Since the revision spine (2026-11-11, owner Q7) a Claude session in a clone
has NO sync verbs: the folder publishes itself (the session's turn end — the
Stop hook — and whenever the folder goes quiet), and the server's changes are
merged into it automatically. A clone push therefore prints the folder's state
line (from GET /api/canvases/sync/state) and exits 0; a pull into a clone is
refused, exit 1. What follows is the history of why the raw commands must
never run there, which still holds.

Imported by ``_fused_cli.py`` — the in-interpreter `fused` entry point every
shipping install runs (the DMG bakes the pre-release fused into the app's own
interpreter and `fusedcli.fused_cli()` spawns ``[sys.executable,
_fused_cli.py]``), and therefore the one place that sees a real argv for every
push a Claude session makes. A session working in a clone is *told* to push with
the standard command; this is what makes that command safe there.

Why it cannot just run:

  * `canvas push` REPLACES the remote UDF set. `_SyncManager._push` wraps that
    in a probe+merge+abort guard, which is the only thing standing between the
    replace and a concurrent workbench edit — and the clobber is unrecoverable
    (`.sync/trash` only ever protects local files, and the watcher's next probe
    sees remote == local, so the merge no-ops).
  * a push that bypasses the watcher also moves the remote behind its back.
    The next poll cannot tell that from a workbench edit: it flags the canvas
    server-changed, which holds every push until someone pulls the agent's
    own push back down.

So a push whose target is a clone never runs: the watcher publishes the folder
itself, inside the server process, under its `_flight_lock`, with the guard
and the revision CAS intact.

Design rules, all of them load-bearing:

  * CONSERVATIVE MATCHING. Anything not recognised with certainty falls through
    to the real CLI unchanged. A false negative costs the safety net for one
    push; a false positive breaks a command that has nothing to do with
    canvases.
  * NO HEAVY IMPORTS. This is reached on the CLI's startup path for *every*
    `fused` invocation, including ones that have nothing to do with canvases.
    Standard library only — no FastAPI, no server state, no `canvases`. The
    canvases-root rule is duplicated here for that reason, the same trade
    ``templates/shared/appenv.py`` makes. (`canvas_marker`, imported lazily
    for one check, is itself standard library only.)
  * FALLING THROUGH IS SAFE ONLY WHEN IT IS INERT. No origin, a target outside
    the canvases root, or a request that cannot connect at all: sync was never
    engaged for that folder, so there is genuinely no merge base to protect and
    the real CLI is exactly right. But a positively-identified clone
    (`_clone_name` matched) is a different case even when the endpoint reports
    "no watcher" (409 `no_watcher`): a merge base from a prior sync session
    can still be sitting on disk, and the remote can have moved through the
    hosted workbench with nothing watching to notice, so that one refuses
    instead of falling through. "When unsure, fall through" only
    holds while the fall-through is a no-op; for a clone target it never is.
"""
import json
import os

# Set by canvases.py on the environment of every fused CLI child IT spawns
# (`_cli_env`), and the FIRST thing checked here.
#
# Without it this module eats its own tail. `_SyncManager._push` runs
# `[*cli.command, "workbench", "canvas", "push", …]`, and on the shim path
# `fused_cli()` resolves `cli.command` to `[sys.executable, _fused_cli.py]` —
# the very file that calls into here. So the server's own push re-entered the
# interception, POSTed back to /api/canvases/sync/push, was refused because a
# push was already running (itself), and `_push` recorded that refusal as a CLI
# failure: push_state "error", push_seq stuck at 0, canvas sync dead with a
# "Fix with Claude" button offering to fix nothing.
#
# An env marker is the right mechanism: it survives exactly one process hop, so
# it marks "this child was launched BY the sync manager" and nothing else. A
# Claude session that simply runs `fused` inherits the server's own environment,
# which never carries it, so a session cannot acquire it by accident or spoof
# its way past the interception. (Inspecting the parent process or timing the
# call would both be guesses about the same fact.)
INTERNAL_ENV = "FUSED_RENDER_CANVAS_PUSH_INTERNAL"

# Mirrors canvases.canvases_base(). Duplicated rather than imported: importing
# canvases here would pull FastAPI into the startup path of every `fused`
# command. Keep the two in step.
_CANVASES_DIR_ENV = "FUSED_RENDER_CANVASES_DIR"
# Under the user's Fused workspace, as canvases_base() does (shell/seed.py
# `fused_dir`): `~/Fused/canvas`, FUSED_RENDER_DIR overriding the workspace.
_FUSED_DIR_ENV = "FUSED_RENDER_DIR"
_DEFAULT_FUSED_DIR = "~/Fused"
# The environment segment between the base and the scope folder
# (`~/Fused/canvas/<env>/user/<name>`): fusedcli.WORKBENCH_ENV_CHOICES,
# duplicated for the same no-heavy-imports reason. Anything else in that
# position is not a clone of ours.
_ENVS = ("unstable", "prod", "custom")
# The scope folders under an environment (D1476; canvas_marker.SCOPES, and a
# test pins the copies together). Clones live in `user/` and nowhere else;
# `team/` and `public/` hold read-only reference copies written by `fused
# canvas mirror`, and so may a `user/<name>` folder whose marker says so.
_CLONES_SCOPE = "user"
_REFERENCE_SCOPES = ("team", "public")
_SCOPES = (_CLONES_SCOPE, *_REFERENCE_SCOPES)
# fusedcli.CUSTOM_WORKBENCH_ENV, duplicated likewise: the env whose runs leave
# FUSED_ENV unset, so the CLI keeps `~/.fused/settings.toml`'s endpoint.
_CUSTOM_ENV = "custom"

# The server's own bound origin, published by server/app.py:set_server_origin_env
# before it serves, so every spawned session inherits it. Absent means "no
# fused-render server around" — never a guessed port: the baseline 1777 is wrong
# under any --port override, and posting a push to a stranger's port is worse
# than falling through.
_ORIGIN_ENV = "FUSED_RENDER_ORIGIN"

# server/common.py:_require_fused (duplicated in canvases.py). A constant guard
# against drive-by requests from a browser, not a secret.
_GUARD_HEADER = "X-Fused"
_GUARD_VALUE = "1"

# Flags that change what `canvas push` publishes in ways the watcher does not
# do: it always publishes the whole clone, with validation, honouring
# .fusedignore. A clone push carrying them gets the same answer as any other
# (the folder publishes itself); they never reach the raw CLI there.
_UNSUPPORTED_FLAGS = ("--no-validate", "--no-ignore")

# Recognised option tokens. Anything else that looks like an option means this
# is not the command shape we know, so we fall through.
_VALUE_OPTS = ("--canvas",)
_BARE_OPTS = ("--id",)

# fused._cli mounts the legacy `workbench` group under a TOP-LEVEL group
# (`fused.agent_core.cli`) that has its own options — so
# `fused --env unstable workbench canvas push .` is the canonical push command
# with a global option in front of it, and matching `args[:3]` verbatim missed
# it entirely (fell through to the raw, unguarded push). These are that
# group's options as of this writing (`fused/agent_core/cli.py`'s `cli()`):
# `--backend` and `--env`, both value-taking. (The old bare flags
# `--enable-infra`/`--enable-destructive`/`--disable-reset` went with the root
# MCP server in fusedio/fused#371; the real CLI now rejects them, so they are
# deliberately NOT skipped here — a push behind one falls through to the CLI's
# own usage error.) Anything else ahead of `workbench` is unrecognised, so
# parsing stops and falls through — same conservative rule as everywhere else
# in this matcher.
_GLOBAL_VALUE_OPTS = ("--backend", "--env")
# The `workbench` group's OWN options (fused `workbench/cli/__init__.py`'s
# `cli()`), between `workbench` and `canvas`. Its `--env` is not the
# top-level one above (that is agent_core's OPENFUSED_ENV, which does not
# move a workbench push): it is FUSED_ENV's flag form, beats the variable,
# and so is the run's real target on a fall-through (`_fall_through`).
_WORKBENCH_VALUE_OPTS = ("--env", "--format")


def _skip_global_options(args: list[str], opts: tuple[str, ...] = _GLOBAL_VALUE_OPTS,
                         seen: dict[str, str] | None = None) -> list[str]:
    """Argv with any recognised leading value options (`opts`, the top-level
    group's by default) removed, so the `workbench canvas push` match below
    still fires behind them. Each skipped option's value lands in `seen`
    when given, the last one winning as in click."""
    i = 0
    while i < len(args):
        token = args[i]
        if token in opts:
            if i + 1 >= len(args):
                break  # malformed; let the real CLI produce the error
            if seen is not None:
                seen[token] = args[i + 1]
            i += 2
            continue
        matched = False
        for opt in opts:
            if token.startswith(opt + "="):
                if seen is not None:
                    seen[opt] = token[len(opt) + 1:]
                i += 1
                matched = True
                break
        if matched:
            continue
        break  # not a global option this recognises — stop here
    return args[i:]


def canvases_base() -> str:
    """`~/Fused/canvas` — the parent of every environment's clone folder."""
    override = os.environ.get(_CANVASES_DIR_ENV)
    if override:
        return override
    fused_dir = os.path.abspath(os.path.expanduser(
        os.environ.get(_FUSED_DIR_ENV) or _DEFAULT_FUSED_DIR))
    return os.path.join(fused_dir, "canvas")


def _locate(path: str) -> dict | None:
    """Where `path` sits in the canvases tree — `{kind, env, name, root}` —
    or None for anything outside it.

    `kind` is one of:

      * "clone" — at or inside `<base>/<env>/user/<name>` (D1476), the one
        place a clone lives.
      * "reference" — at or inside `<base>/<env>/team` or `/public`, or a
        `user/<name>` folder whose marker says `mirror`: a read-only
        reference copy. `name` is "" for the scope folder itself.
      * "legacy" — at or inside a canvas folder at the pre-D1476
        `<base>/<env>/<name>` (it holds a `canvas.toml`): a clone the layout
        move has not reached yet. canvas_layout.py defers one exactly while
        a session is working in it, so this is the folder a session is most
        likely to type a push in — and nothing syncs it there.
      * "tree" — `<base>/<env>` itself, or anywhere else under it: the
        clones folder itself, or a folder that is no canvas. Not a push we know anything
        about; a pull INTO it would plant a canvas folder in a tree
        fused-render and the mirror command own.

    `root` says whether `path` IS the canvas folder rather than something
    inside it. Resolved through `realpath` on both sides (the CLI wrapper
    reaches a clone through `pwd -P`)."""
    base = os.path.realpath(canvases_base())
    target = os.path.realpath(path)
    try:
        rel = os.path.relpath(target, base)
    except ValueError:  # another drive on Windows
        return None
    parts = rel.split(os.sep)
    if parts[0] not in _ENVS:
        return None
    if len(parts) == 1:
        # The environment folder itself: every clone, scope folder and
        # `.sync/` is in it, and a forced pull there would replace them all.
        return {"kind": "tree", "env": parts[0], "name": "", "root": False}
    if parts[1] in ("", os.pardir):
        return None
    env, first = parts[0], parts[1]
    env_dir = os.path.join(base, env)
    if first in _SCOPES and not os.path.isfile(os.path.join(env_dir, first, "canvas.toml")):
        if first in _REFERENCE_SCOPES:
            return {"kind": "reference", "env": env, "root": len(parts) <= 3,
                    "name": parts[2] if len(parts) > 2 else ""}
        if len(parts) < 3:
            return {"kind": "tree", "env": env, "name": "", "root": False}
        name = parts[2]
        mirror = _is_mirror(os.path.join(env_dir, first, name))
        return {"kind": "reference" if mirror else "clone", "env": env, "name": name,
                "root": len(parts) == 3}
    # Pre-D1476: a clone directly under the environment (a scope-named folder
    # holding a canvas.toml is a legacy clone of that name). Any other folder
    # there is nothing of ours.
    if not os.path.isfile(os.path.join(env_dir, first, "canvas.toml")):
        return {"kind": "tree", "env": env, "name": first, "root": False}
    return {"kind": "legacy", "env": env, "name": first, "root": len(parts) == 2}


def _is_mirror(folder: str) -> bool:
    """Whether `folder` holds a reference copy's files — the mirror command's
    own, or one whose adoption has not been pulled yet
    (`canvas_marker.is_reference`, the reading the server uses). The one import
    of this module beyond the standard library: canvas_marker is stdlib-only
    itself, and only a command aimed at `user/<name>` ever gets here."""
    from fused_render import canvas_marker

    return canvas_marker.is_reference(folder)


def _clone_name(source_dir: str) -> tuple[str, str] | None:
    """`(name, env)` when `source_dir` IS a clone root, else None.

    A clone lives at exactly ``canvases_base()/<env>/user/<name>`` (D1476).
    Requiring that precise shape — one segment under a known environment's
    `user/` folder, not merely somewhere beneath the base — is what keeps a
    push of some subdirectory from being rewritten into a push of the whole
    canvas, which is a different operation with different content. EVERY
    environment's clones match: the `env` segment is sent with the push, and
    the server routes it to that environment's manager (prod's and unstable's
    same-named canvases sync at once, each pushing with its own FUSED_ENV).
    A reference copy is never a clone (`_reference_of`).
    """
    found = _locate(source_dir)
    if found is None or found["kind"] != "clone" or not found["root"]:
        return None
    return found["name"], found["env"]


def parse_push(args: list[str]) -> dict | None:
    """`{"source_dir", "canvas", "unsupported", "env"}` for a `workbench
    canvas push` we recognise, else None ("not ours, fall through"). `env` is
    the workbench group's `--env`, or None.

    `args` is argv WITHOUT the program name, i.e. what click sees.
    """
    skipped = _skip_global_options(args)
    if skipped[:1] != ["workbench"]:
        return None
    workbench_opts: dict[str, str] = {}
    skipped = _skip_global_options(skipped[1:], _WORKBENCH_VALUE_OPTS, workbench_opts)
    if skipped[:2] != ["canvas", "push"]:
        return None
    rest = skipped[2:]
    positionals: list[str] = []
    canvas = None
    unsupported = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--":
            # Everything after it is positional, by POSIX convention.
            positionals.extend(rest[i + 1:])
            break
        if token in _UNSUPPORTED_FLAGS:
            unsupported.append(token)
            i += 1
            continue
        if token in _BARE_OPTS:
            # --id makes the reference a canvas ID rather than a directory —
            # a different command shape entirely.
            return None
        if token in _VALUE_OPTS:
            if i + 1 >= len(rest):
                return None  # malformed; let the real CLI produce the error
            canvas = rest[i + 1]
            i += 2
            continue
        matched = False
        for opt in _VALUE_OPTS:
            if token.startswith(opt + "="):
                canvas = token[len(opt) + 1:]
                i += 1
                matched = True
                break
        if matched:
            continue
        if token.startswith("-"):
            # --help, or any flag this parser has never heard of. The real CLI
            # is the authority on both.
            return None
        positionals.append(token)
        i += 1
    if len(positionals) != 1:
        # SOURCE_DIR is required and singular. Zero or several means either a
        # usage error (the CLI should say so) or a shape we do not model.
        return None
    return {"source_dir": positionals[0], "canvas": canvas,
            "unsupported": unsupported, "env": workbench_opts.get("--env")}


# `canvas pull`'s own options (fused workbench/cli/canvas.py): value-taking
# `-o/--output`, flags `--force/-f`, `--dry-run/-n`, `--show-diff`, and the
# canvas-ref options. Only `-o` matters to the matcher (where it writes);
# every other token is accepted as is — a pull INTO a clone is refused
# whatever its flags.
_PULL_OUTPUT_OPTS = ("-o", "--output")


def parse_pull(args: list[str]) -> dict | None:
    """`{"canvas_ref", "output"}` for a `workbench canvas pull` (output None
    when no `-o`), else None. Same leading-option rules as parse_push."""
    skipped = _skip_global_options(args)
    if skipped[:1] != ["workbench"]:
        return None
    skipped = _skip_global_options(skipped[1:], _WORKBENCH_VALUE_OPTS, {})
    if skipped[:2] != ["canvas", "pull"]:
        return None
    rest = skipped[2:]
    output = None
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in _PULL_OUTPUT_OPTS:
            if i + 1 < len(rest):
                output = rest[i + 1]
            i += 2
            continue
        matched = False
        for opt in _PULL_OUTPUT_OPTS:
            if token.startswith(opt + "=") or (opt == "-o" and token.startswith("-o") and len(token) > 2):
                # `-o` takes the rest of the token as is (a path may hold
                # `=`); only `--output=` splits.
                output = token[len(opt) + 1:] if opt == "--output" else token[2:]
                matched = True
                break
        if not matched and not token.startswith("-"):
            positionals.append(token)
        i += 1
    return {"canvas_ref": positionals[0] if positionals else None, "output": output}


def _clone_of(path: str) -> tuple[str, str] | None:
    """`(name, env)` when `path` is a clone root or anywhere inside one."""
    found = _locate(path)
    if found is None or found["kind"] != "clone":
        return None
    return found["name"], found["env"]


def _reference_of(path: str) -> dict | None:
    """`_locate`'s answer when `path` is, or is inside, a read-only reference
    copy (`team/`, `public/`, a mirror-marked `user/<name>`), else None."""
    found = _locate(path)
    return found if found is not None and found["kind"] == "reference" else None


def _legacy_of(path: str) -> dict | None:
    """`_locate`'s answer when `path` is, or is inside, a clone still at its
    pre-D1476 folder, else None."""
    found = _locate(path)
    return found if found is not None and found["kind"] == "legacy" else None


def _pull_refusal(paths: list[str]) -> str | None:
    """Why a pull writing to any of `paths` is refused, or None (not in the
    canvases tree at all: fall through). Every folder under an environment
    is refused, as before D1476; the sentence says which kind it is. A
    reference copy is named first: of the reasons, it is the one where the
    pull fights another writer (the mirror command) over the folder."""
    kinds = {found["kind"] for found in map(_locate, paths) if found is not None}
    for kind, message in (("reference", _REFERENCE_PULL_REFUSAL),
                          ("legacy", _LEGACY_REFUSAL),
                          ("clone", _PULL_REFUSAL),
                          ("tree", _TREE_PULL_REFUSAL)):
        if kind in kinds:
            return message
    return None


_PULL_REFUSAL = ("This folder is kept in sync by fused-render; a server change is "
                 "merged here automatically. Do not pull.\n")
# A reference copy (D1476). A push from one would REPLACE (or create) the
# user's own canvas of that name with someone else's files; a pull into one
# fights the mirror command that owns the folder.
_REFERENCE_PUSH_REFUSAL = (
    "This folder is a read-only reference copy of a Fused canvas, kept up to "
    "date by fused-render. It is never pushed: a push from here would overwrite "
    "the canvas of that name in your own account. To change a canvas, open it "
    "from fused-render's Canvases page and edit its folder under user/.\n")
_REFERENCE_PULL_REFUSAL = (
    "This folder is a read-only reference copy of a Fused canvas, kept up to "
    "date by fused-render. Do not pull into it.\n")
# A clone still at `<env>/<name>` (D1476 moved clones to `<env>/user/<name>`).
# Nothing syncs it there, and a raw push or pull is the unguarded replace this
# module exists to stop.
_TREE_PULL_REFUSAL = (
    "This folder is inside fused-render's canvases tree, which fused-render "
    "manages. Do not pull into it: open the canvas from fused-render's "
    "Canvases page, or pull into a folder elsewhere.\n")
_LEGACY_REFUSAL = (
    "This canvas folder is at its old location: fused-render now keeps canvases "
    "under user/ and moves this one there once no session is working in it. "
    "Nothing syncs it here, so do not push or pull. Finish this session, then "
    "open the canvas from fused-render's Canvases page.\n")


def _get_state(origin: str, name: str, env: str) -> tuple[int | None, dict]:
    """GET the canvas's sync state (with `pending_files`). `(status, body)`;
    status None = could not connect at all (no server around)."""
    import urllib.error
    import urllib.parse
    import urllib.request

    query = urllib.parse.urlencode({"name": name, "env": env, "files": "1"})
    request = urllib.request.Request(
        origin.rstrip("/") + "/api/canvases/sync/state?" + query,
        method="GET", headers={_GUARD_HEADER: _GUARD_VALUE})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _decode(exc.read())
        except OSError:
            return exc.code, {}
    except (urllib.error.URLError, OSError, ValueError):
        return None, {}


def _decode(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh" % (seconds // 3600)


def state_line(state: dict) -> str:
    """"base rev N, server rev M, K pending files, last push 12s ago"."""
    import time

    def rev(value) -> str:
        return str(value) if value is not None else "n/a"

    pending = state.get("pending_files")
    d2s = ((state.get("legs") or {}).get("disk_to_server") or {})
    last = d2s.get("last_push_at")
    parts = ["base rev " + rev(state.get("base_revision")),
             "server rev " + rev(state.get("server_revision"))]
    if isinstance(pending, int):
        parts.append("%d pending file%s" % (pending, "" if pending == 1 else "s"))
    parts.append("last push %s ago" % _age(time.time() - last)
                 if isinstance(last, (int, float)) else "no push yet")
    return ", ".join(parts)


def _fall_through(name: str, env: str, err, cli_env: str | None = None) -> int | None:
    """None (run the real CLI) unless that run would target the WRONG Fused:
    1, with the refusal written.

    Only the fall-through paths need this. A push the server takes is routed
    by the clone's own `env` to that environment's manager, whatever this
    process's FUSED_ENV says; the raw CLI instead pushes to FUSED_ENV — which
    the wrapper takes from the clone path when run inside one, from the
    DEFAULT environment otherwise, and an explicit `FUSED_ENV=x fused ...`
    beats both. For a clone of the other environment that is one Fused's
    files into the other's same-named canvas: not inert, so refuse.

    The custom env is the one targeted by an UNSET FUSED_ENV (the CLI then
    keeps `~/.fused/settings.toml`'s endpoint), so a custom clone refuses any
    set FUSED_ENV. A prod/unstable clone under an unset one (a custom default
    outside the clone, or a bare shell) gets FUSED_ENV bound to its own env
    before falling through: unset would otherwise send the raw push to
    `~/.fused/settings.toml`'s endpoint, over a same-named canvas there. The
    shim imports and runs the CLI in this same process right after
    (`_fused_cli.py`), and click reads the variable at parse time.

    `cli_env` is the command's own `workbench --env`, which beats FUSED_ENV
    exactly as it does in the CLI (click: an option over its envvar)."""
    target_env = cli_env or os.environ.get("FUSED_ENV")
    source = "--env" if cli_env else "FUSED_ENV"
    if env == _CUSTOM_ENV:
        if target_env:
            err.write(
                "fused-render: canvas clone %r belongs to the custom environment "
                "(~/.fused/settings.toml), but this `fused` run targets %s "
                "(%s).\n"
                "Pushing it there would overwrite a different canvas. Run the push "
                "from inside the clone folder, with no --env and FUSED_ENV unset.\n"
                % (name, target_env, source))
            return 1
        return None
    if target_env and target_env != env:
        err.write(
            "fused-render: canvas clone %r belongs to the %s environment, but this "
            "`fused` run targets %s (%s).\n"
            "Pushing it there would overwrite a different canvas. Run the push "
            "from inside the clone folder (`fused` then targets the clone's own "
            "environment), or with FUSED_ENV=%s.\n"
            % (name, env, target_env, source, env))
        return 1
    if not target_env:
        os.environ["FUSED_ENV"] = env
    return None


def maybe_intercept(args: list[str], out, err) -> int | None:
    """Handle a clone push or pull and return its exit code, or None to fall
    through.

    The revision spine (2026-11-11, owner Q7): a Claude session in a clone has
    NO sync verbs. The folder publishes itself — at the end of each turn (the
    Stop hook) and whenever it goes quiet — and the server's changes are
    merged into it automatically. So inside a clone:

      * `canvas push` (any flags) prints the folder's state line and exits 0:
        there is nothing for the session to do, and nothing it ran failed.
      * `canvas pull` (any flags: --force, --dry-run, ...) is refused, exit 1:
        a pull would replace or race what the watcher merges.

    `args` is argv without the program name; `out`/`err` are the streams to
    write the CLI-shaped output to.
    """
    if os.environ.get(INTERNAL_ENV):
        # This child IS the sync manager's own push or pull. Intercepting it
        # would refuse the very sync this module defers to — see
        # INTERNAL_ENV. Checked before anything else, including the argv match.
        return None
    pulled = parse_pull(args)
    if pulled is not None:
        target = pulled["output"]
        if target is None and pulled["canvas_ref"]:
            # The CLI's default output folder is the canvas name under cwd.
            target = os.path.join(os.getcwd(), pulled["canvas_ref"])
        where = [target] if target else []
        if pulled["output"] is None:
            where.append(os.getcwd())
        refusal = _pull_refusal(where)
        if refusal is None:
            return None
        err.write("fused-render: " + refusal)
        return 1
    parsed = parse_push(args)
    if parsed is None:
        return None
    if _reference_of(parsed["source_dir"]) is not None:
        # Before everything else, server or no server: there is no state in
        # which pushing a reference copy is right.
        err.write("fused-render: " + _REFERENCE_PUSH_REFUSAL)
        return 1
    legacy = _legacy_of(parsed["source_dir"])
    if legacy is not None and legacy["root"]:
        err.write("fused-render: " + _LEGACY_REFUSAL)
        return 1
    clone = _clone_name(parsed["source_dir"])
    if clone is None:
        return None  # not a canvas clone
    name, env = clone
    origin = os.environ.get(_ORIGIN_ENV)
    if not origin:
        return _fall_through(name, env, err, parsed["env"])  # no server around
    if parsed["canvas"] is not None and parsed["canvas"] != name:
        # Pushing one clone's contents at a DIFFERENT canvas. The watcher for
        # this folder cannot express that; the real CLI can, and there is no
        # merge base for the named canvas here.
        return _fall_through(name, env, err, parsed["env"])

    status, body = _get_state(origin, name, env)
    if status is None:
        # Server unreachable — the real CLI is the honest fallback.
        return _fall_through(name, env, err, parsed["env"])
    if status != 200:
        err.write("fused-render: %s\n" % (body.get("error") or "could not read the canvas's "
                                                                "sync state"))
        return 1
    if body.get("watching") is False or body.get("watcher_epoch") is None:
        others = body.get("other_watchers") or []
        if others:
            # Another fused-render process holds this clone's writer lock: the
            # raw push is never inert for a clone, so refuse, and say where.
            owner = others[0] if isinstance(others[0], dict) else {}
            err.write(
                "fused-render: canvas %r is synced by another fused-render (pid %s, "
                "port %s).\n"
                "That one publishes this folder; quit it and open the canvas in this "
                "fused-render to sync it here.\n"
                % (name, owner.get("pid", "?"), owner.get("port", "?")))
            return 1
        # No LIVE watcher — but the target is a positively-identified clone,
        # and a merge base from a PRIOR sync session may still be on disk at
        # .sync/<name>.json while the remote moved through the hosted
        # workbench with nothing watching. The raw push would be exactly the
        # wholesale-replace clobber this module exists to prevent, so refuse
        # rather than fall through (D350).
        err.write(
            "fused-render: canvas %r is not being synced (no watcher is running).\n"
            "This folder is a canvas clone, so it publishes itself while fused-render "
            "watches it — often it just isn't watched because the canvas tab isn't "
            "open. Open this canvas in fused-render to start sync; it publishes the "
            "folder on its own.\n" % name)
        return 1
    out.write("This folder publishes itself: %s. Nothing to do.\n" % state_line(body))
    d2s = ((body.get("legs") or {}).get("disk_to_server") or {})
    if d2s.get("state") == "error":
        # Not a failure of this command, but the session must see it: these
        # lines name the files and nodes to fix, verbatim (D328).
        out.write("The last publish failed:\n")
        for line in body.get("error_detail") or [d2s.get("reason") or "unknown error"]:
            out.write("%s\n" % line)
    elif d2s.get("state") == "held" and d2s.get("reason"):
        out.write("Publishing is held: %s.\n" % d2s["reason"])
    return 0
