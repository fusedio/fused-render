---
name: fused-render-app-python
description: Use when a bot or Claude session must call an app's .py directly (no page) — GET /api/apps/python + POST /api/run — or when writing a .py so bots can call it: annotated main(**params), docstring, JSON return, 60 s.
---

# App Python, called directly

An app folder's `.py` files are the app's capability; the page is one UI over them. A bot (OpenBot's `py` action, a Claude-harness bot, any local script) can run one **without rendering the page**, through the exact call the page makes. Same runner, same envelope, same 60 s bound — a file that works for the bot works in the page and vice versa.

## Part 1 — Author contract: make a `.py` callable

One rule: **a top-level `main(**params)` is the entrypoint; nothing else is.**

```python
"""Totals per category for one month from the uploaded CSV."""   # module doc = fallback description

def main(month: str = "2026-09", csv: str = "", top: int = 10) -> dict:
    """Totals per category for one month. Reads `csv`; writes nothing."""
    ...
    return {"month": month, "rows": rows}      # JSON-native
```

| Rule | Why |
|---|---|
| `main` only, top level, **sync** | `/api/run` binds exactly `main(**params)` (`_child.py`) and JSON-dumps the return; an `async def main` is listed as not callable. Other public functions are listed but not callable here (curate them in `mcp.toml` for MCP hosts). |
| Annotate every parameter (`str`/`int`/`float`/`bool`), give defaults | Bots read the signature from the AST and coerce URL-style strings by annotation (`_binding.coerce`). Unannotated → raw string. |
| First docstring line = the description a bot sees | One line, plain, states what it reads and what it changes ("Sends the summary by mail" vs "Reads the ledger"). Module docstring is the fallback. |
| Return JSON-native (`dict`/`list`/scalars) | Envelope is JSON. Non-serialisable → error. |
| No `argv`, no `stdin`, no `input()` | Fresh subprocess, params arrive as kwargs. |
| ≤ 60 s, no override | `DEFAULT_TIMEOUT`. Longer → `fused.trackJob` (→ `fused-render-jobs`) or a daemon (→ `fused-render-background-apps`); bots cannot call daemons. |
| Side effects stay inside the folder's `.fused/data` or are named in the docstring | A bot calling a non-owned app pauses for approval; the doc line is what the user reads. |
| Secrets never in params | Read them from `.fused/data` or the keychain inside `main`. |
| `_private()` helpers, `helpers.py` without `main` | Fine; listed as not callable with `reason`. |

Exported pages: unchanged. Nothing here touches `index.html`.

## Part 2 — Consumer: calling a file

Both routes are local, same origin as the shell; send `X-Fused: 1` on both (the guard that blocks blind cross-origin requests; it is not auth).

**Discover** — `GET /api/apps/python?html=<abs page path>` (or `?dir=<abs folder>`):

```json
{ "app": "/Users/x/Fused/app/expense-tracker", "html": ".../index.html",
  "entrypoint": "main", "timeout_s": 60,
  "files": [
    { "file": "summary.py", "callable": true, "signature": "main(month: str='2026-09', csv: str='', top: int=10)",
      "params": [ {"name":"month","type":"str","default":"'2026-09'","required":false}, ... ],
      "doc": "Totals per category for one month. Reads `csv`; writes nothing.", "functions": ["main"], "error": "" },
    { "file": "helpers.py", "callable": false, "reason": "no top-level main()", "functions": ["parse"], ... } ],
  "tools":      [ {"name":"send_report","file":"mail.py","entrypoint":"main","description":"…","curated":true} ],
  "background": null }
```

AST only — nothing is imported. `tools` = the folder's `mcp.toml` (also reachable as MCP tools); `background` = `{kind: main|daemon, file, running}` when the folder runs a resident worker (not callable through these routes).

**Run** — `POST /api/run` `{ "py": "<abs>/summary.py", "html": "<abs>/index.html", "params": {"month": "2026-08"} }`:

```json
{ "ok": true,  "result": {...}, "stdout": "", "resolved_py": "/abs/summary.py", "duration_ms": 412 }
{ "ok": false, "error": {"type": "TimeoutError", "message": "...", "traceback": "..."}, "stdout": "", "resolved_py": "...", "duration_ms": 60004 }
```

Pass `html` even with an absolute `py`: relative-path and mount handling then match the page. `params` keys must be `main`'s parameters; unknown keys fail binding.

```sh
curl -s -H 'X-Fused: 1' "$ORIGIN/api/apps/python?dir=/Users/x/Fused/app/expense-tracker"
curl -s -H 'X-Fused: 1' -H 'Content-Type: application/json' "$ORIGIN/api/run" \
  -d '{"py":"/Users/x/Fused/app/expense-tracker/summary.py","html":"/Users/x/Fused/app/expense-tracker/index.html","params":{"month":"2026-08"}}'
```

`$ORIGIN` = the shell's origin (`~/.fused-render/server.json` → `origin`, or `FUSED_RENDER_ORIGIN`).

OpenBot bots never read this file: their surface is the `py` action in their prompt (`{"action":"py","app":"<folder>","file":"summary.py","args":{...}}`; no `file` → the listing above as RESULT). Claude-harness bots use the two calls as written.
