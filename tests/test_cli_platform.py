"""On Windows `fused-render` dispatches to serve and open like anywhere else:
nothing refuses the platform up front."""
import sys

import pytest

from fused_render import cli as cli_module


@pytest.mark.parametrize("argv, target", [
    (["fused-render", "serve"], "_run_serve"),
    (["fused-render"], "_run_serve"),
    (["fused-render", "open", "x.html"], "_run_open"),
])
def test_main_dispatches_on_windows(monkeypatch, argv, target):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", argv)
    hit = []
    for name in ("_run_serve", "_run_open"):
        monkeypatch.setattr(cli_module, name, lambda args, n=name: hit.append(n))
    cli_module.main()
    assert hit == [target]
