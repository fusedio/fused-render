"""`fused-render` refuses to start on Windows with one plain line, instead of
failing later on a POSIX-only import (the wheel is platform-agnostic, so pip
still installs it there)."""
import sys

import pytest

from fused_render import cli as cli_module


def test_main_refuses_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["fused-render", "serve"])
    monkeypatch.setattr(cli_module, "_run_serve", lambda args: pytest.fail("served on Windows"))
    with pytest.raises(SystemExit) as exc:
        cli_module.main()
    assert "does not support Windows" in str(exc.value.code)
