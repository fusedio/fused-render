"""The Windows installer registry generator
(scripts/windows/generate_installer_registry.py) must emit a `fused-render`
URL Protocol class so Windows routes fused-render:// deep links to the app.
Pure string generation — no winreg, no Windows — so it runs on any platform."""
import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_GEN = _SCRIPTS / "windows" / "generate_installer_registry.py"


def _run(tmp_path) -> list[str]:
    out = tmp_path / "registry.iss"
    spec = importlib.util.spec_from_file_location("generate_installer_registry", _GEN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    argv = sys.argv
    sys.argv = ["generate_installer_registry.py", str(out)]
    try:
        module.main()
    finally:
        sys.argv = argv
    return out.read_text(encoding="utf-8").splitlines()


def test_emits_url_protocol_class(tmp_path):
    lines = _run(tmp_path)
    scheme_lines = [ln for ln in lines if '"Software\\Classes\\fused-render"' in ln]
    # The class default value (a description) and the mandatory empty-string
    # "URL Protocol" value both live directly on the scheme key.
    assert any('ValueName: ""' in ln and "uninsdeletekey" in ln for ln in scheme_lines)
    assert any('ValueName: "URL Protocol"' in ln for ln in scheme_lines)


def test_emits_url_protocol_default_icon_and_command(tmp_path):
    lines = _run(tmp_path)
    assert any(
        '"Software\\Classes\\fused-render\\DefaultIcon"' in ln for ln in lines
    )
    cmd_lines = [
        ln
        for ln in lines
        if '"Software\\Classes\\fused-render\\shell\\open\\command"' in ln
    ]
    assert cmd_lines, "no shell\\open\\command for the fused-render scheme"
    # Reuses the same EXE + "%1" quoting convention as the file handlers.
    assert '%1' in cmd_lines[0]
    assert "FusedRenderPy.exe" in cmd_lines[0]
