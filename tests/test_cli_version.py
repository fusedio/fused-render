"""`fused-render --version` (and `-V`) prints the package version and exits 0.

`main()` puts `serve` in front of any first token that is not a subcommand so
the historical bare `fused-render --port N` keeps working; `--version` must be
recognised before that, or argparse answers "unrecognized arguments".
"""
import pytest

import fused_render
from fused_render import cli as cli_module


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_the_version_and_exits_zero(monkeypatch, capsys, flag):
    monkeypatch.setattr("sys.argv", ["fused-render", flag])
    with pytest.raises(SystemExit) as exc:
        cli_module.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out == f"fused-render {fused_render.__version__}"


def test_version_is_not_mistaken_for_a_serve_flag(monkeypatch, capsys):
    # The bare-invocation shim must leave `--version` alone: `serve --version`
    # would be an argparse error (exit 2), not the version.
    monkeypatch.setattr("sys.argv", ["fused-render", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli_module.main()
    assert exc.value.code == 0
    assert "usage:" not in capsys.readouterr().err
