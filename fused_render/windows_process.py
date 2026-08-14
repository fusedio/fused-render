import os
import subprocess
import sys


def install_no_window_policy() -> None:
    if (
        sys.platform != "win32"
        or "FUSED_RENDER_DESKTOP_INSTANCE_ID" not in os.environ
        or getattr(subprocess.Popen.__init__, "_fused_render_no_window", False)
    ):
        return

    original = subprocess.Popen.__init__

    def init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    init._fused_render_no_window = True
    subprocess.Popen.__init__ = init
