# fused-render app

Folder = **fused-render app**, rendered by fused-render explorer. `index.html` = entry view; edit in place (no second top-level `.html` — one entry file makes folder open as app). Keep `<meta name="fused-app" />` near top of `<head>`: ONLY marker identifying app entry — detection reads first 4 KiB only; without it app vanishes from apps hub. Right after: `<meta name="fused-api-version" content="N" />` — fused page API version app written against. Leave as is; app page's "Migrate" task updates code + tag together. No tag = version 0.

Page runs inside explorer, which injects `fused` bridge: `fused.params` (URL-synced view state), `fused.runPython("./file.py", args)` (compute in Python beside page), `fused.readFile` / `fused.rawUrl`, more. No network at runtime, no build step.

Before non-trivial changes, invoke **`fused-render-authoring`** skill — full contract for `.html` views + `.py` data files: bridge, params wiring, file IO, `.fused/data` vs `.fused/cache` rules, theming, debugging blank views / traceback overlays.

## `.fused/`

Hidden `.fused/` at app root, created for you: `data/` = state app owns, can't rebuild; `cache/` = derived bytes, deletable anytime; `meta.json` = where app was set up (mismatched `app_dir` → folder moved/copied → distrust absolute-path keys).

**Persistent state → `.fused/data/` only** (not JSON beside index.html, not home dir, not temp). **Cache slow work → `.fused/cache/`, aggressively** — each `runPython` = fresh subprocess, 60 s timeout. Promise: everything in `cache/` reconstructible from `data/` + outside world. `.fused/` is machine-local: gitignored, excluded from `.fused` app exports — never put anything there the app needs on fresh machine.

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

App reads machine-wide **file index** (search box, disk-usage/file-type breakdown, repos list, SQL over filesystem)? Invoke **`fused-render-index`**: `fused.fileIndex.search/query`, readiness envelope, direct-parquet reader for bulk Python.

fused-render supplies these skills (plus `fused-render-usage`, `fused-render-custom-templates`) to every chat it launches as session plugin — available by name, no install. Also keeps copy in Claude Code user-level skills dir for sessions fused-render didn't start (plain `claude` here). Skill missing from both → start/restart fused-render once; refreshes both on startup.
