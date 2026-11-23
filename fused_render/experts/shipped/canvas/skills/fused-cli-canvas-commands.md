# The fused CLI for canvases
trigger: fused cli, fused workbench, canvas push, canvas pull, canvas validate, canvas mirror, canvas list, canvas create, canvas rename, canvas delete, command, FUSED_ENV, login

## How to run it

- Always run the bare command `fused ...`. That resolves to the CLI fused-render ships.
- Never `pip install fused`, never `python -m fused`, never run a `fused` from another path, venv or environment: a different fused misses the pieces the clone's two-way sync depends on and bypasses its push protection.
- `fused --help` lists the commands.
- It uses the user's existing Fused sign-in. A session must never run `fused workbench login` or `fused cloud login` (they wait on a browser round-trip that cannot complete there). On an auth error, ask the user to sign in from fused-render's Canvases page or a terminal.

## Environment

Inside `~/Fused/canvas/<env>/...` the `fused` wrapper sets `FUSED_ENV` from the `<env>` path segment when it is not already set, so a command run in a prod clone reaches prod. Outside a clone it uses fused-render's default environment (Preferences, default `prod`). For `custom` it leaves `FUSED_ENV` unset so the CLI uses the endpoint in `~/.fused/settings.toml`. An explicit `FUSED_ENV=prod fused ...` on the command line wins.

## Canvas commands fused-render itself runs

These are the exact forms in fused-render's code:

| Command | What it does |
|---|---|
| `fused workbench --format json canvas list` | list the account's canvases |
| `fused workbench canvas create <name>` | create a canvas |
| `fused workbench canvas pull <name> -o <dir> --force` | download a canvas into a folder |
| `fused workbench canvas push <dir> --canvas <name>` | upload a folder as that canvas (replaces the remote UDF set) |
| `fused workbench canvas validate <dir>` | check a canvas folder; fused-render also runs it with `--skip node_files` |
| `fused workbench canvas rename <name> <new>` | rename a canvas |
| `fused workbench canvas delete <name>` | delete a canvas |
| `fused workbench --format json canvas mirror --output <env root>` | fill `user/`, `team/`, `public/` with read-only reference copies |

Flags not listed here are not covered by this expert; check `fused --help` or the `workbench:fused-workbench-cli` skill a clone session is handed.

## Inside a live clone

- Do NOT run `fused workbench canvas push` or `pull` in a clone. The folder publishes itself when the session's turn ends (and whenever it goes quiet with no turn running), and server changes are merged in automatically.
- A push whose target is a clone only prints the folder's sync state and exits 0. A pull into a clone is refused (exit 1).
- DO run `fused workbench canvas validate .` after structural edits (renames, added or removed nodes or edges). An invalid canvas is not published and its errors reach the user.

## Inside a reference copy

Push and pull are refused there too. A reference copy is never published.

## The mirror command's results

Its stderr has one progress line per canvas, like `[12/340] pulled public/Some_Canvas`; its stdout is one JSON document. Exit 0 is a full run, exit 1 a partial one (some scope or canvas failed). A bundled `fused` too old to have the command answers "No such command", and fused-render stops running it for that CLI until the server restarts. fused-render keeps the last result per environment in `<env>/.sync/.mirror.json`.
