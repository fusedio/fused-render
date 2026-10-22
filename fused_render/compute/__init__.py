# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see NOTICE.
"""The local compute engine: runs one Python script on a given interpreter.

`LocalPythonComputeBackend` (backend.py) writes the handler files (`handler/`)
and the call's `event.json` into a private per-call directory and executes
`<interpreter> run.py` there. `invoke.py` is the host-side broker for the
fan-out calls a script makes (`fused.load(...)` / `udf.map(...)`).

`app_mcp.py` is the app MCP server: it publishes an app folder's manifest tools
and runs them through the same backend.
"""
