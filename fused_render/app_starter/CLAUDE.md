# fused-render app

This folder is a **fused-render app** — a self-contained folder rendered by
the fused-render explorer. `index.html` is the app's entry view; it was
scaffolded from the starter kit, so edit it in place (don't create a second
top-level `.html` next to it — one entry file is what makes the folder open
as an app). Keep the `<meta name="fused-app" />` tag near the top of its
`<head>`: the marker is the ONLY thing that identifies this page as a fused
app's entry — without it the app disappears from the apps hub (detection reads
only the first 4 KiB of the file, so keep the tag near the top).

The page runs inside the explorer, which injects a `fused` runtime bridge:
`fused.params` (URL-synced view state), `fused.runPython("./file.py", args)`
(compute in Python files beside this one), `fused.readFile` / `fused.rawUrl`,
and more. There is no network at runtime and no build step.

Before non-trivial changes, invoke the **`fused-render-authoring`** skill —
the full contract for `.html` views and `.py` data files: the `fused` bridge,
params-as-state wiring, file IO, theming, and debugging blank views /
traceback overlays.

## Where this app keeps its stuff: `.fused/`

Every app folder gets a hidden `.fused/` at its root, created for you:

```
.fused/
  data/       state the app owns and cannot rebuild
  cache/      derived bytes the app can rebuild — deletable at any time
  meta.json   {"version": 1, "app_dir": "<absolute path>", "created_at": "<iso>"}
```

**Write persistent state to `.fused/data/`, and nowhere else.** Not a JSON file
beside `index.html` (that is authored content, and it lands in git history),
not a folder under the user's home, not the system temp dir. Reach it from a
`.py` as `os.path.join(os.path.dirname(__file__), ".fused", "data")` — relative
paths in a `.py` resolve next to that file, so a script at the app root can
just use `./.fused/data/`. From the page, go through a `.py`.

**Cache anything slow into `.fused/cache/`, and cache more than feels
necessary.** Every `fused.runPython` call is a fresh subprocess: no globals
survive, no import cost is amortised, and a 60 s timeout is waiting for
anything that recomputes work it already did. A network fetch, a parsed large
file, a rendered tile, a model response, an expensive aggregation — key it by a
hash of its inputs, write the result under `.fused/cache/`, and return the
cached copy on the next call. Nothing sweeps this folder automatically, so if a
cache can grow without bound, give it your own cap or TTL.

The split between the two folders is a promise, not a naming preference:
**everything in `cache/` must be reconstructible from `data/` plus the outside
world.** Deleting `cache/` entirely must cost the user nothing but time. If it
would lose something, it is data.

`meta.json` records where the app was set up. Nothing rewrites it, so if its
`app_dir` no longer matches where the app actually is, the folder was moved or
copied — the moment to distrust anything you keyed by absolute path. What to do
about that is the app's call; there is no automatic repair.

`.fused/` is machine-local. It is gitignored, and it is not included when the
app is exported as a `.fused` app file — so never put anything there that the
app needs in order to work on a fresh machine.

## Version control

This folder lives inside the workspace apps repository — one local git repo
at the parent `local/` folder, shared by every app beside this one (the
starter landed as this folder's first commit). **Commit as you work, in small
chunks** — after every coherent change, even tiny ones (a copy tweak, a
single style fix, one function). Never batch a whole task into one commit,
and never leave this folder dirty at the end of a turn.

**Always scope git to this folder.** Stage with `git add -A -- .` and commit
with `git commit -m "…" -- .`, run from this directory. Never run a bare
`git add -A`, `git commit -a`, or an unscoped `git commit` — sibling apps
share the repository, and an unscoped command sweeps their in-progress work
into your commit. (`git status -- .` and `git log -- .` are the scoped reads.)
Use short imperative subjects ("Add dark theme toggle", "Fix param sync").
Don't push, don't add remotes, don't rewrite history, don't touch paths
outside this folder — the repo is purely local undo history for the apps.

If the app reads the machine-wide **file index** — a file search box, a
disk-usage or file-type breakdown, a repos list, SQL over the filesystem —
invoke **`fused-render-index`** as well: `fused.fileIndex.search/query` and their
readiness envelope, plus the direct-parquet reader for bulk Python.

fused-render supplies those skills (and their siblings, `fused-render-usage`
and `fused-render-custom-templates`) to every chat it launches, as a plugin loaded
for that session, so it is available here by name with no install step. It
also keeps a copy in Claude Code's user-level skills directory for sessions
fused-render didn't start — a plain `claude` in this folder, say. If the skill
isn't listed in one of those, start (or restart) fused-render once; it
refreshes both on startup.
