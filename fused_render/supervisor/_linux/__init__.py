"""Linux desktop supervisor backend.

The Linux counterpart to `_win32/`: process-tree keeper (`tree.py`),
single-instance election + Unix-socket IPC (`instance.py`), autostart toggle
(`startup.py`), and native dialog/shell helpers (`ui.py`). Reached only through
`fused_render.supervisor._backend`; `core.py` never imports this package
directly.
"""
