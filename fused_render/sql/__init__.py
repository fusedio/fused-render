"""A local SQL engine for pages (SPEC §55).

DuckDB running in a worker process of its own, reached over loopback routes,
answering in Arrow IPC streams:

  server/routers/sql.py   the routes under `/api/sql/session/...`
  runner.py   the engine and its sessions in the server process: spawn,
              requests and their answers, cancel, idle reaping, quit, the
              startup sweep of directories left by dead servers
  files.py    the named files a session's SQL can read and write
  _worker.py  the worker (runs in its own interpreter, never imported by the
              server: duckdb and pyarrow are never loaded in the server
              process for this)

Kept free of imports here so `python -m fused_render.sql._worker` loads
nothing of the server.
"""
