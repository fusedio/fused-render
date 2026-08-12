"""Local inference: running a model on this machine (SPEC §40).

Three modules, and the split is who they talk to:

* `registry` — what this machine can do, and which folder does it.
* `supervisor` — the resident worker processes: start, evict, measure, stop.
* `catalog` — which models to suggest for each capability.

Nothing here imports a machine-learning library. The heavy work lives in a
runner's `worker.py`, in an interpreter built from that runner's own
`pyproject.toml`, in a process the supervisor owns — so fused-render's own
environment stays a file explorer's, and a model that falls over falls over
alone.
"""
