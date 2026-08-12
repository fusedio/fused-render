---
name: fused-render-index
description: How to read fused-render's file index from an app — the `fused.index.*` JS bridge for interactive UI, and a copy-paste duckdb reader for bulk Python that reads the store's parquet directly. Use whenever a page or script needs to search, count, aggregate, or list files across the machine rather than one folder; whenever the user asks for a file search box, a disk-usage or file-type breakdown, a "find all my X files" view, a repos/projects list, or SQL over their filesystem; whenever they mention the index, indexing, a scan, ~/.fused-render/index, partitions.json, dirs.parquet, /api/index/*, or /api/git-repos; and whenever an app carries its own filesystem-scanning engine that should be deleted in favour of this one. For writing the .html/.py files themselves use `fused-render-authoring`; for binding a preview template to an extension use `fused-render-custom-templates`.
---

# Reading the fused-render file index

fused-render keeps a **parquet index of the user's filesystem** — one row per file, one row per directory, built by a background scan and served by `/api/index/*`. It is what makes machine-wide search instant, and any app can read it. Nothing here is about building or configuring the index; it is about **answering questions from it** without walking the filesystem, spawning `find`, or stat-ing a million paths.

Two ways in, and the choice is not stylistic:

| Need | Path |
|---|---|
| Interactive UI reads — a search box, stat tiles, a result table, paging | **`fused.index.*`** (JS). No subprocess per keystroke; readiness and server-side screening are included. |
| Bulk/analytical reads that stay in Python — aggregate the whole index, join it against another parquet, feed pandas/geopandas | **Read the parquet directly** with duckdb. A 200-row JSON response is the wrong shape *and* the wrong bottleneck. |
| Writes — start a scan, cancel one, edit roots/ignore | **HTTP** (`fused.index.scan` / `cancel` / `config.set`). Never write into the store directory yourself. |

## The readiness rule (read this before anything else)

An index query answers **zero rows** both when nothing matches and when no index has ever been built. Rendering the second as the first is a silent lie — the failure mode `fused_render/server/routers/git_repos.py` calls "the original silent lie and the reason any of this exists". Whichever path you take, an empty result is **two different states** and your UI must be able to tell them apart:

- **no index / not covered** → "still building" or "your folder isn't indexed yet", never "no results".
- **a real empty answer** → "no matches".

The JS bridge hands you that distinction on every call; the Python reader gets it by returning `(None, None)` for a missing manifest. Neither treats "no index" as an error — it is a state you render.

## A. The JS bridge: `fused.index.*`

Injected with the rest of `window.fused` — no script tag, no fetch, no `X-Fused` header to remember (the bridge sends it on every POST for you).

| Call | Answers |
|---|---|
| `await fused.index.stats({root})` | Totals + per-extension breakdown for one subtree: `{rows, dirs, total_size, types: [{ext, n, size}], updated, last_root, empty}`. `root` defaults to the last-scanned root. |
| `await fused.index.lookup({q, limit, offset, sort})` | Files whose **path** matches `q`: `{rows: [{path, dir, name, ext, size, mtime}], total, empty, scanned_partitions, of_partitions}`. `q` is a substring, `*` is a wildcard, and a `q` starting with `/`, `~` or a drive letter is anchored at the start. `sort` is one of `path` / `size` / `mtime` (default) / `name`. |
| `await fused.index.search({root, q, limit})` | The in-folder corpus for `root`, in `/api/fs/walk`'s entry shape: `{entries: [{rel, is_dir, size, mtime}], covered, fresh, age_s, truncated}`. This is the explorer's own search source — reach for it when you want *one folder's* tree, not a machine-wide match. |
| `await fused.index.query({sql, limit})` | One **read-only** SQL statement over the `files` and `dirs` views: `{columns, rows, truncated}`. The real workhorse for anything shaped like a report. |
| `await fused.index.status()` | `{indexed, has_index, scanning, files_indexed, last_completed_at, phase, files, dirs, run_id, …}` — the live scan readout. |
| `await fused.index.scan({root, full})` | Start a scan; `{run_id, root, runs}`. No `root` means every configured root. `full: true` discards the reuse cache. |
| `await fused.index.cancel({runId})` | Cancel a run by id. |
| `await fused.index.config.get()` / `.set({roots, ignore})` | `{roots, configured_roots, ignore, defaults, location}`. A `set` that changes the rules starts reconciling rescans and says so (`needs_rescan`, `rescan_run_ids`). |
| `await fused.index.repos()` | Git repositories on the machine: `{repos: [{path}], indexed, reason, scanning, stale}`. See section E — do not rewrite this query yourself. |

**Every one of them also resolves with `ready`:**

```js
ready = { indexed, scanning, stale, reason }
```

- `indexed` — a compacted index exists at all.
- `scanning` — a scan is in flight. **Independent of `indexed`**: a rescan keeps serving the last completed generation, so this means "say indexing…", not "stop using the index".
- `stale` — the answer may be behind the filesystem (a scan running, or a slice built under superseded ignore rules). It never means "identical to the filesystem" — an index is always slightly behind.
- `reason` — `"no-index"`, `"outdated"` (the rule that would produce these rows never ran, so a rebuild is coming), `"not-covered"` (the index exists but has not visited this root), or `null`.

A field is `null` only where that response genuinely cannot say, never as a guess: `search()` reports `scanning: null` because it is the per-keystroke path and must not double its request count, and a failed readiness probe answers all-null rather than claiming the index is missing. Where the endpoint carries richer facts they stay on the result too — `empty`, `covered`, `fresh`, `age_s`, and `repos()`'s own `reason`.

**Two routes are deliberately not on the bridge**, both still reachable by raw `fetch` for a caller that truly means it: `POST /api/index/delete` (wiping the user's whole index is not something a page does on load) and `POST /api/index/ask` (it spends AI credits per call, so it belongs behind an explicit user action, not a render path).

### The canonical shape: stat tiles + a paginated table

```html
<div id="banner"></div>
<div id="tiles"></div>
<input id="q" placeholder="search files…">
<table><tbody id="rows"></tbody></table>
<script>
  const PAGE = 50;

  // ONE place decides what an empty result means. Every render path goes
  // through it, so "no index yet" can never leak out as "no results".
  function banner(ready) {
    const el = document.getElementById("banner");
    if (ready.indexed === false) {
      el.textContent = ready.scanning
        ? "Building the index…"
        : "No index yet — run a scan to search your files.";
      return false;
    }
    el.textContent = ready.stale ? "Indexing — results may be a little behind." : "";
    return true;
  }

  async function drawTiles() {
    const s = await fused.index.stats({});
    if (!banner(s.ready)) return;
    document.getElementById("tiles").textContent =
      `${s.rows.toLocaleString()} files · ${s.dirs.toLocaleString()} folders · ` +
      `${(s.total_size / 1e9).toFixed(1)} GB`;
  }

  async function drawRows() {
    const q = fused.params.get("q") || "";
    const offset = parseInt(fused.params.get("offset") || "0", 10);
    const out = await fused.index.lookup({ q, limit: PAGE, offset, sort: "size" });
    if (!banner(out.ready)) return;
    document.getElementById("rows").innerHTML = out.rows
      .map((r) => `<tr><td>${r.name}</td><td>${r.size}</td><td>${r.dir}</td></tr>`)
      .join("");
    // `total` is the match count, not the page — that is what pages the table.
    document.title = `${out.total} matches`;
  }

  // Params are the state, exactly as in any other view (`fused-render-authoring`).
  document.getElementById("q").addEventListener("input", (e) => {
    fused.params.set("q", e.target.value);
    fused.params.set("offset", "0");
  });
  fused.params.onChange(drawRows);
  drawTiles();
  drawRows();
</script>
```

Rejections follow the rest of the bridge: an `Error` whose `.message` is the server's sentence verbatim (a duckdb `Binder Error: no such column: nope` is exactly what the user needs to read) plus `.type` (`"forbidden"` for a refused POST, `"bad_request"` otherwise, or the server's own type) and `.status`.

## B. The Python side: read the parquet directly

The index is real parquet on disk, so Python can read it with **nothing but `duckdb`** — no HTTP, no `import fused_render`. That last point is not a preference: an app folder with a `pyproject.toml` gets its own uv-built venv (`<home_dir()>/venvs/<sha256(abspath)[:16]>`, see `fused_render/projectenv.py`) where `fused_render` is **not importable**. So you copy this reader into the app and declare `duckdb` in the app's own `pyproject.toml`; you do not import ours.

### The store on disk

```
~/.fused-render/index/
├── partitions.json        ← THE MANIFEST. Read this first, always.
├── files/
│   ├── part-000008-00000.parquet
│   └── part-000008-00001.parquet
├── dirs.parquet
├── fsevents.json          ┐
├── ignore_applied.json    │ writer-only state.
├── config.json            │ A reader never opens these,
├── scans.json             │ and never writes anything here at all.
└── runs/                  ┘
```

`partitions.json` top-level keys: `generation`, `rows`, `updated`, `last_root`, `partitions`. Each partition entry: `file`, `rows`, `min`, `max`, `min_lower`, `max_lower` — the `_lower` pair is a **separate case-folded aggregate**, not `lower(min)`/`lower(max)`, because byte order and folded order disagree (see `prune` in `fused_render/index/query.py`).

Schemas (`fused_render/index/store.py`, `schemas()`):

- `files` — `path, dir, name, ext, size, mtime, depth`
- `dirs` — `dir, sig, n_files, total_size, mtime_ns, n_subdirs, depth`

Units and conventions: `path`/`dir` are absolute and POSIX-separated; `name` is the basename; `ext` is lowercase with no leading dot and `''` for no extension; `size` is bytes; `mtime` is epoch **seconds** (float) while `dirs.mtime_ns` is epoch **nanoseconds** and `0` there means unknown; `depth` is the absolute count of `/` in the path.

### THE trap: never glob `files/*.parquet`

Compaction writes the next generation **beside** the old one and deliberately leaves the previous generation on disk for readers still holding the previous manifest (`files_src` in `fused_render/index/query.py`). A glob picks up both and silently double-counts. Measured on a real store:

```
manifest names: ['part-000008-00000.parquet', 'part-000008-00001.parquet']
on disk       : ['part-000007-00000.parquet', 'part-000007-00001.parquet',
                 'part-000008-00000.parquet', 'part-000008-00001.parquet']
correct rows (manifest): 578767
naive glob rows        : 1157818   -> inflated by 579051 (2.00x)
```

Nothing errors. Every count, sum and average is simply wrong, by roughly 2x, and only on a store that has been scanned more than once — so it passes on a fresh machine and fails on the user's.

The corollary is what makes the reader simple: **readers never take `store_lock`.** They follow the manifest, and generations make that safe (`store_lock`'s docstring in `fused_render/index/store.py`). No locking, no coordination, no retry loop.

### The reader (copy this)

```python
"""Read-only access to the fused-render file index. Only dep: duckdb."""
import json, os, duckdb

def store_dir(location=None):
    # Prefer the `location` that /api/index/stats and /api/index/config report;
    # this env resolution is the fallback (fused.runPython inherits os.environ).
    if location:
        return location
    home = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    branch = os.environ.get("FUSED_RENDER_BRANCH")
    if branch:
        home = os.path.join(home, "branches", branch)
    return os.path.join(home, "index")

def connect(location=None):
    """A duckdb connection with `files` and `dirs` views, plus the manifest.
    Returns (None, None) when no scan has ever compacted."""
    d = store_dir(location)
    try:
        with open(os.path.join(d, "partitions.json")) as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return None, None                       # no index yet — an ANSWER, not an error
    con = duckdb.connect()
    # NEVER a files/*.parquet glob: old generations stay on disk and would double-count.
    con.read_parquet([os.path.join(d, "files", p["file"])
                      for p in manifest["partitions"]]).create_view("files")
    con.read_parquet(os.path.join(d, "dirs.parquet")).create_view("dirs")
    return con, manifest

def main(min_size="0"):
    con, manifest = connect()
    if con is None:
        # The Python half of the readiness rule: say so, don't return [].
        return {"indexed": False, "rows": []}
    rows = con.execute(
        "SELECT ext, count(*) n, sum(size) bytes FROM files "
        "WHERE size >= ? GROUP BY 1 ORDER BY bytes DESC LIMIT 20",
        [int(min_size)]).fetchall()
    return {"indexed": True, "updated": manifest["updated"],
            "rows": [{"ext": e, "n": n, "bytes": b} for e, n, b in rows]}
```

Two gotchas, both hit while verifying this:

- **`CREATE VIEW x AS SELECT * FROM read_parquet(?)` does not work.** It fails with `Binder Error: Unexpected prepared parameter. This type of statement can't be prepared!` The relation API (`con.read_parquet(list).create_view(...)`) avoids it *and* avoids having to SQL-quote the user's store path — which is exactly why `query.py` carries a `_q` helper for the paths it does interpolate. Parameters work fine in ordinary `SELECT`s (see `main` above); it is the `CREATE VIEW` that refuses them.
- **Returning `(None, None)` for a missing manifest instead of raising.** "No index" is a state to render, not an error — the same discipline as the JS envelope. Raising here turns a normal first-boot condition into a red traceback overlay.

**Branch-scoping matters in a dev worktree.** `home_dir()` (`fused_render/shell/storage.py`) honours `FUSED_RENDER_HOME` and nests under `branches/<ref>/` when `FUSED_RENDER_BRANCH` is set, so a dev server's index is a *different store* from `~/.fused-render/index`. When a reader and a UI disagree about the row count, this is usually why — pass the `location` that `/api/index/stats` reports rather than resolving the path twice.

## C. Raw HTTP reference

`GET`s are unguarded. **`POST`s require `X-Fused: 1`** (`_require_fused`, `fused_render/server/common.py`) — not authentication, a cross-origin-preflight tripwire — and the `fused.index.*` bridge sends it for you. Errors are `{"error": "<message>"}` with a 4xx/5xx; `/api/index/ask` can also answer the AI relay's nested `{"error": {"type", "message"}}`.

| Route | Method | Params (defaults) | Answers |
|---|---|---|---|
| `/api/index/stats` | GET | `root=""` (→ manifest's `last_root`) | `ok, empty, location, rows, dirs, total_size, updated, last_root, partitions, types[]`. `types` is the top 50 extensions by size plus an `other` roll-up. Reads every partition — there is no cached rollup. |
| `/api/index/lookup` | GET | `q=""`, `limit=100` (cap `MAX_LIMIT` = 5000), `offset=0`, `sort=mtime` (`path`/`size`/`mtime`/`name`; anything else falls back to `mtime`) | `ok, empty, location, rows[], total, partitions[], scanned_partitions, of_partitions` |
| `/api/index/search` | GET | `root` **required**, `q=""`, `limit=200000` (`MAX_CORPUS`) | `ok, covered, fresh, updated, age_s, root, entries[], truncated, total, scanned_partitions, of_partitions`. `fresh` is informational (`FRESH_MAX_AGE_S` = 3600 s); `covered` is the gate. A miss is `{covered: false, entries: []}` with a **200**, never an error. |
| `/api/index/status` | GET | `run_id=""` (→ the most recent *running* run, else the latest), `since=0` | `ok, has_index, indexed, scanning, files_indexed, last_completed_at, updated, run_id, root, phase, dirs, files, reused, current, summary, cancelled, error, running` (+ `events`, `cursor` when a `run_id` is given) |
| `/api/index/runs` | GET | — | `ok, runs[]` — per run: `run_id, root, running, phase, files, dirs, …` |
| `/api/index/config` | GET | — | `ok, roots, configured_roots, ignore, defaults, location` |
| `/api/index/scan` | POST | `{root?, full?}` | `ok, run_id, root, runs[]`. No `root` = every configured root; one dead root does not fail the rest. |
| `/api/index/cancel` | POST | `{run_id}` | `ok, cancelled` |
| `/api/index/query` | POST | `{sql, limit?}` (`limit` default 200, cap 5000) | `ok, columns, rows, truncated` |
| `/api/index/ask` | POST | `{prompt, limit?}` | `ok, sql, columns, rows, truncated` — a question in English compiled to SQL by the AI relay and then run through the *same* guard. The compiled `sql` comes back even when it is refused. **Costs AI credits per call.** |
| `/api/index/config` | POST | `{roots?, ignore?}` | the GET's shape + `needs_rescan, rescan_run_id, rescan_run_ids` |
| `/api/index/delete` | POST | — | `ok, deleted, cancelled, location`. Drops the whole store; cancels running scans first. |
| `/api/git-repos` | GET | — | `indexed, reason, scanning, stale, repos[{path}]` — note **no `ok`** on this one |

### What guarded SQL will and will not run

`/api/index/query` (and `/ask`) execute the caller's statement in a confined DuckDB session (`fused_render/index/guarded_query.py`):

- **One statement only.** A batch is refused — that is how a read-looking prefix smuggles a write past a reader skimming the box.
- **Read-only statement types**: `SELECT` (which covers `WITH`, `DESCRIBE` and `PRAGMA` as DuckDB parses them), `CALL`, `EXPLAIN`. `INSERT`/`UPDATE`/`DELETE`/`CREATE`/`DROP`/`COPY`/`ATTACH`/`SET`/… are refused *before* anything runs, because an in-memory write would otherwise succeed even behind a locked configuration.
- **Two views and nothing else**: `files` and `dirs`. The connection is confined to the index directory (`allowed_directories` → `enable_external_access=false` → `lock_configuration=true`, in that order), so any other path is a permission error.
- **Caps**: `MAX_LIMIT` = 5000 rows (the cap goes *into* the SQL, so a whole-index statement is never materialised to be trimmed), and a **10 s** wall clock before `con.interrupt()` stops it.
- A statement that parses but cannot run surfaces duckdb's own message as a 400, verbatim. Show it — "Binder Error: no such column: nope" tells the user what to fix.

Written against an index with no partitions yet, a query still runs: the views become typed empty stand-ins, so it fails on its own logic or not at all.

## D. Deriving a new *kind* of entity from the index

The Repos tab is the worked precedent, and the pattern generalises: **a kind is an index fact, not a filesystem probe.**

`.git` is in `ignore.LEAF_DIR_NAMES`, so the scan records exactly one `dirs` row for it and never descends. "Which directories are repositories" therefore *is* "which `dirs` rows are named `.git`", with the repo root as that row's parent:

```sql
SELECT dir FROM dirs WHERE regexp_extract(dir, '[^/]*$') = '.git'
```

Zero stats, zero subprocesses, no `git` binary. It replaced a first cut that read every indexed directory and asked `os.path.isdir(d + "/.git")` about each — **~71k stats per request** on a real home. Look for the same move whenever you are about to probe the filesystem per row: is the marker something the scan already recorded?

Read `fused_render/server/routers/git_repos.py` in full before copying the pattern; it documents the parts that are not obvious, and two of them **an app cannot reproduce**:

- **`junk_path` is applied to the PARENT, never the row.** `.git` is itself a dot-segment, so screening the raw row rejects every repository on the machine. Without the parent screen the tab is mostly other people's checkouts (`~/.local/share/nvim/lazy/*`, `~/.oh-my-zsh/custom/plugins/*`) outnumbering the user's own better than 2:1. `junk_path` is `fused_render/server/walk.py` — **Python-only, no endpoint**.
- **`MountGuard`** screens the parent too, as the layer that holds when an older build wrote rows a newer guard would have pruned. Also **Python-only**.
- **The per-root applied-ignore signature** — what separates "zero rows because the rule never ran" (`reason: "outdated"`, a rebuild is coming) from "zero rows because this machine genuinely has no repos" (`{indexed: true, repos: []}`, a real answer). It is exposed on **no endpoint**, and the test is on the RAW row count, before screening, because screening can legitimately take real rows to zero.

That is precisely why `fused.index.repos()` wraps the endpoint instead of apps rewriting the query: an app-side `SELECT` gets the repo list roughly right and gets both kinds of zero, and both screening layers, wrong. **Use `fused.index.repos()`.** When you need a *different* kind, the query is yours but the screening lesson is not optional — and a new kind that needs `junk_path`/`MountGuard` belongs in a server route, not in a page.

## E. Migrating an app that carries its own index engine

If an app ships its own scanner, it is now duplicating `fused_render/index/` — which was **ported from exactly such an app** (the `Ported from OpenIndex` docstrings in `fused_render/index/{store,scan,fsevents,query}.py`). The migration is a deletion, not a rewrite:

- **Delete the scan engine wholesale** — the walk, the parquet sink, compaction, FSEvents replay, run bookkeeping (~1300 lines in the original) and the reader module beside it (~190). All of it is in `fused_render/index/` now, behind `/api/index/*`.
- **The app becomes pure HTML + JS**, talking to `fused.index.*`. What was a `sql` action is now `fused.index.query()` — strictly better, because the original had *no allowlist and no read-only flag* and the port deliberately dropped it for that reason.
- **Anything genuinely bulk stays in Python**, using the direct reader in section B rather than a re-implemented store.

One genuine gap, so nobody discovers it mid-migration: **relocating the index directory is not supported.** `IndexConfig.dir` is fixed to `home_dir()/index`, settable only via `FUSED_RENDER_HOME` at process start, with no runtime API — `POST /api/index/config` takes `roots` and `ignore`, not `location`. An app that let the user pick where the index lives loses that.

## Pitfalls checklist

- Globbing `files/*.parquet` instead of following `partitions.json` → every number silently ~2x too big, and only on a machine that has scanned twice.
- Rendering `rows: []` as "no results" without checking `ready.indexed` → the original silent lie.
- Treating `covered: false` from `search()` as an error → it is a 200 and a normal state; fall back to a live walk or say "not indexed yet".
- Reading `total` as "rows on this page" → it is the full match count, which is what pages the table.
- Taking `store_lock`, or writing anything into the index directory → readers follow the manifest; writers are the scan's business.
- `import fused_render` from an app `.py` that has a `pyproject.toml` → `ModuleNotFoundError`; the project venv does not contain it. Copy the reader.
- Forgetting `duckdb` in the app's own `pyproject.toml` → the declaration is the complete list; the bundled set is not unioned in (see `fused-render-authoring`).
- `POST`ing `/api/index/*` by hand without `X-Fused: 1` → 403. Use `fused.index.*`.
- Calling `/api/index/ask` per keystroke → it spends AI credits per call. It is a button, not a debounce target.
- Re-implementing the repos query in a page → wrong on both kinds of zero and on both screening layers; call `fused.index.repos()`.
- Assuming `stale: false` means the index matches the filesystem → it means "as fresh as the index gets". Nothing can know the difference without re-walking, which is the cost this whole thing avoids.
- Comparing a Python reader's counts against the UI's on a dev worktree → different stores (`FUSED_RENDER_BRANCH`). Pass the `location` the API reports.

## When to switch skills

- Writing or debugging the `.html`/`.py` files themselves — the `fused` API, `main()`, params-as-state, the traceback overlay → **`fused-render-authoring`**.
- Binding a preview template to a file extension → **`fused-render-custom-templates`**.
- Opening a view or driving the running app → **`fused-render-usage`**.
