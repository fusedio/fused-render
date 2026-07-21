---
name: setting-up-dev-env
description: Use when setting up a fused-render checkout or git worktree for the first time — before running pytest, the dev server, or any daemon — so tests and the server actually work instead of silently failing on missing deps or an unbuilt React shell.
---

# Setting Up the Dev Env

## Overview

Every fresh checkout/worktree needs a **3.12 venv** and a **built React shell** — both are gitignored, so they don't carry into a worktree. Without them, `pytest`/`python` silently run against the wrong interpreter or fail on missing deps, and every server test dies with `RuntimeError: React shell not built`.

## Steps

From the worktree root:

```bash
uv venv --python 3.12 .venv                                                  # 3.14 lacks duckdb/rasterio wheels
uv pip install --python .venv/bin/python -e ".[dev,bundled,fused]" pytest-xdist
cd frontend && bun install && bun run build && cd ..                         # builds fused_render/static/shell-dist/
```

Verify: `ls fused_render/static/shell-dist/index.html` and `.venv/bin/python -m pytest -q` (~1150 pass).

## Reference

| Item | Why |
|------|-----|
| Python 3.12 | duckdb/rasterio have no 3.14 wheels; fused wheel needs ≥3.11 |
| `dev` extra | pytest + httpx (backs TestClient) |
| `bundled` extra | duckdb, rasterio, zarr, pandas, geopandas… (templates + daemons) |
| `fused` extra | compute-engine wheel for `/api/run` |
| frontend build | `create_app()` refuses to start without `shell-dist/` |
| `ensurepip` | only for the Deploy one-click path, not the suite |

Parallel tests: the suite is xdist-safe (process isolation), so `pytest -n auto` works (~4.5x faster). Process-based only — `os.chdir` + `importlib.reload` would race under a threaded runner.
