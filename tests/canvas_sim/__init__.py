"""Model-based simulator for the canvas sync revision program (contract §4).

`server.py` is the fake Fused control plane plus the in-process stand-in for
every fused CLI / shim subprocess canvases.py spawns; `world.py` is the
actors (frame, Claude session, human), the seeded scheduler and the four
invariants. tests/test_canvas_sync_sim.py drives both against the REAL
`_SyncManager`.
"""
