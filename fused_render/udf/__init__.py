"""Running Fused UDFs from fused-render (SPEC §54).

The page runtime's `fused.workbench.udf()` and everything under it:

  server/routers/udf.py   the doors: the `/api/udf/ws` socket a page runs
              over, and `GET /api/udf/results/<token>` for a result's bytes
  service.py  one run: resolve what to run, the gates (batch/map asks first,
              size), the saved copy, joining an identical run in flight
  store.py    saved results on disk, by run key
  runner.py   one warm SDK worker per Fused environment
  _worker.py  that worker (runs in the bundled interpreter, never imported
              by the server: the fused SDK is never loaded in the server
              process)

Kept free of imports here so `python -m fused_render.udf._worker` loads
nothing of the server.
"""
