__version__ = "0.6.14"

# Flavor (Fused Render vs Fused Bot) must set FUSED_RENDER_HOME before any
# module derives a home-rooted path at import time. Stdlib-only, no-op for
# the default flavor. See fused_render/_flavor.py.
from fused_render import _flavor as _flavor  # noqa: E402

_flavor.apply_env()
