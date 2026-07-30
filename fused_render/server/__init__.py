"""The FastAPI server: static shell, filesystem API, HTML rendering, Python
execution, fused.ai. See app.py for create_app() and the package layout.
"""

from fused_render.server.app import create_app, export_app_env, set_server_origin_env

__all__ = ["create_app", "export_app_env", "set_server_origin_env"]
