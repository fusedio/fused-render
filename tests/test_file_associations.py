"""The shared file-association table (scripts/file_associations.json) must stay
in sync with the shipping runtime's own registry + icon map. If a template or
icon-variant change drifts the two apart, fail here rather than shipping a stale
"Open with" association set in either packaging pipeline."""
import importlib.util
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "file_associations", _SCRIPTS / "file_associations.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_committed_json_matches_winopen():
    fa = _load_module()
    committed = [{"extension": a.extension, "icon": a.icon} for a in fa.associations()]
    assert committed == fa.derive_from_winopen(), (
        "scripts/file_associations.json is stale; run "
        "`python scripts/file_associations.py regenerate`"
    )


def test_mime_and_glob_derivations():
    fa = _load_module()
    by_ext = {a.extension: a for a in fa.associations()}
    py = by_ext[".py"]
    assert py.mime == "application/x-fused-render-py"
    assert py.glob == "*.py"
    assert py.type_name == "PY File (FusedRender)"
