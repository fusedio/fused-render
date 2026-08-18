"""Route a `fused workbench canvas push` of a CANVAS CLONE through the server.

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
    With a clean clone the next poll cannot tell that from a workbench edit and
    takes the wholesale force-pull branch: the agent's own push comes straight
    back down, every unignored local file the push did not publish is deleted,
    and the UI shows a phantom "pulled from workbench".

So a push whose target is a clone is turned into a POST to
``/api/canvases/sync/push``, which performs the very same push inside the server
process, under the watcher's own lock, with the guard intact.

Design rules, all of them load-bearing:

  * CONSERVATIVE MATCHING. Anything not recognised with certainty falls through
    to the real CLI unchanged. A false negative costs the safety net for one
    push; a false positive breaks a command that has nothing to do with
    canvases.
  * NO HEAVY IMPORTS. This is reached on the CLI's startup path for *every*
    `fused` invocation, including ones that have nothing to do with canvases.
    Standard library only — no FastAPI, no server state, no `canvases`. The
    canvases-root rule is duplicated here for that reason, the same trade
    ``templates/shared/appenv.py`` makes.
  * FALLING THROUGH IS ALWAYS SAFE. No origin, target outside the canvases
    root, no watcher running, or a request that cannot connect: in each of those
    the manifest-based two-way sync is not running for that folder, so there is
    no merge base to protect and the real CLI is exactly right.
"""
import json
import os

# Mirrors canvases.canvases_root(). Duplicated rather than imported: importing
# canvases here would pull FastAPI into the startup path of every `fused`
# command. Keep the two in step.
_CANVASES_DIR_ENV = "FUSED_RENDER_CANVASES_DIR"
_DEFAULT_CANVASES_DIR = "~/.fused-render/canvases"

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

# The endpoint runs the push synchronously and canvases.PUSH_TIMEOUT is 180s, so
# this has to outlast it — otherwise a slow-but-successful push looks like a
# failure here and the session is told the wrong thing.
_HTTP_TIMEOUT_S = 200.0

# Flags that change what `canvas push` publishes in ways the endpoint cannot
# express: it always pushes the whole clone, with validation, honouring
# .fusedignore. Refused rather than passed through, because falling through
# would run exactly the unguarded push this module exists to prevent — and
# refusing puts a readable reason in the session's transcript instead.
_UNSUPPORTED_FLAGS = ("--no-validate", "--no-ignore")

# Recognised option tokens. Anything else that looks like an option means this
# is not the command shape we know, so we fall through.
_VALUE_OPTS = ("--canvas",)
_BARE_OPTS = ("--id",)


def canvases_root() -> str:
    return os.environ.get(_CANVASES_DIR_ENV) or os.path.expanduser(_DEFAULT_CANVASES_DIR)


def _clone_name(source_dir: str) -> str | None:
    """The canvas name when `source_dir` IS a clone root, else None.

    A clone lives at exactly ``canvases_root()/<name>``. Requiring that precise
    shape — one path segment under the root, not merely somewhere beneath it —
    is what keeps a push of some subdirectory from being rewritten into a push
    of the whole canvas, which is a different operation with different content.
    """
    root = os.path.realpath(canvases_root())
    target = os.path.realpath(source_dir)
    parent, name = os.path.split(target)
    if parent != root or not name:
        return None
    return name


def parse_push(args: list[str]) -> dict | None:
    """`{"source_dir", "canvas", "unsupported"}` for a `workbench canvas push`
    we recognise, else None ("not ours, fall through").

    `args` is argv WITHOUT the program name, i.e. what click sees.
    """
    if args[:3] != ["workbench", "canvas", "push"]:
        return None
    rest = args[3:]
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
            "unsupported": unsupported}


def _post_push(origin: str, name: str) -> tuple[int | None, dict]:
    """POST the push. `(status, body)`; status None = could not connect."""
    import urllib.error
    import urllib.request

    payload = json.dumps({"name": name}).encode("utf-8")
    request = urllib.request.Request(
        origin.rstrip("/") + "/api/canvases/sync/push",
        data=payload, method="POST",
        headers={"Content-Type": "application/json", _GUARD_HEADER: _GUARD_VALUE},
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx still carries the server's JSON body, which is the whole
        # point — the refusal or the failure detail is in there.
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


def maybe_intercept(args: list[str], out, err) -> int | None:
    """Handle a clone push and return its exit code, or None to fall through.

    `args` is argv without the program name; `out`/`err` are the streams to
    write the CLI-shaped output to.
    """
    parsed = parse_push(args)
    if parsed is None:
        return None
    origin = os.environ.get(_ORIGIN_ENV)
    if not origin:
        return None  # no server around
    name = _clone_name(parsed["source_dir"])
    if name is None:
        return None  # not a canvas clone
    if parsed["canvas"] is not None and parsed["canvas"] != name:
        # Pushing one clone's contents at a DIFFERENT canvas. The endpoint is
        # keyed on the watcher for this folder, so it cannot express that; the
        # real CLI can, and there is no merge base for the named canvas here.
        return None
    if parsed["unsupported"]:
        err.write(
            "fused-render: %s cannot be used inside the canvas clone %r.\n"
            "This folder is two-way synced, and the push runs through "
            "fused-render's sync manager so it cannot silently replace a "
            "concurrent workbench edit. Push without that flag; fix validation "
            "errors rather than skipping them.\n"
            % (", ".join(parsed["unsupported"]), name))
        return 2

    status, body = _post_push(origin, name)
    if status is None:
        return None  # server unreachable — the real CLI is the honest fallback
    if status == 409 and body.get("code") == "no_watcher":
        # Nothing is syncing this folder, so there is no merge base to protect.
        return None
    if status != 200:
        err.write("fused-render: %s\n"
                  % (body.get("error") or "the canvas push was refused"))
        return 1
    if body.get("ok"):
        out.write("Pushed canvas %r (via fused-render sync).\n" % name)
        return 0
    # A real push failure. Everything the CLI printed is in error_detail, one
    # entry per line, and it is reproduced verbatim: these lines name the files
    # and nodes to fix, and rewording them makes them unsearchable (D328).
    for line in body.get("error_detail") or []:
        err.write("%s\n" % line)
    if not body.get("error_detail"):
        err.write("%s\n" % (body.get("error") or "canvas push failed"))
    return 1
