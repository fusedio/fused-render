"""Claude Code config editing, as a server-side package.

This started life as `core_apps/claude_config/` — an html+py app whose Python
half ran per-click inside the `runPython` sandbox, one subprocess per action.
That shape cost real things: a 30 s wall clock on every action (so the docs
refresh and `claude mcp list` sat right up against the cap), a read-only
:archive: mount to ship the scripts, a builtin-mount readiness gate the
Preferences page had to poll before it could even offer the tab, and flat
`import lib` sibling imports that only resolve because runPython chdirs into
the app folder.

Moving the same modules into the package makes them ordinary imports behind one
FastAPI endpoint (`server/routers/claude_config.py`), so a native React tab can
call them directly. The canonical html+py app still exists in the
fused-render-examples repo for people who want to read/fork it; this copy is the
one the desktop app ships.

What the port changed, and nothing else:
  * `import lib` -> `from . import lib` (package-relative).
  * `lib.config_lock()` also takes a process-local threading lock, because the
    concurrency source is now a threadpool inside ONE process rather than N
    subprocesses, and `fcntl` is absent on Windows (see lib.py).
  * `settings_catalog.json` is read from a user-writable override when present
    and only ever WRITTEN there — site-packages must stay read-only (lib.py's
    catalog helpers).

Every module keeps its `main(action=..., ...)` dispatch signature: that is the
router's whole contract, and keeping it means the modules stay runnable
standalone (and diffable against the example app).

The `<feature>.md §N` citations throughout are that example app's own `specs/`
folder, which did not come along: it documents the html+py app's UI as much as
its Python, so shipping a stale copy inside the package would be worse than a
pointer. Read them in fused-render-examples.
"""
