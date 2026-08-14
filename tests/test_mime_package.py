"""The runtime MIME generator (fused_render._mime_package) and the packaging-time
generator (scripts/file_associations.py) derive the same artifacts from the same
winopen data. Assert they are byte-identical so the AppImage build and the Linux
self-integration can never drift — a stale runtime XML would register a
different "Open with" association set than the one the build staged.
"""
import importlib.util
import sys
from pathlib import Path

from fused_render import _mime_package

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load_packaging_module():
    spec = importlib.util.spec_from_file_location(
        "file_associations", _SCRIPTS / "file_associations.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_custom_xml_matches_packaging_xml():
    fa = _load_packaging_module()
    packaging_xml = fa._mime_xml(fa.associations())
    assert _mime_package.custom_mime_xml() == packaging_xml


def test_runtime_mime_types_match_packaging_mime_types():
    fa = _load_packaging_module()
    assert _mime_package.desktop_mime_types() == fa.mime_types(fa.associations())


def test_runtime_mime_types_end_with_scheme_handler():
    types = _mime_package.desktop_mime_types()
    assert types[-1] == "x-scheme-handler/fused-render"
    assert len(types) == len(set(types))  # deduped


def test_effective_mime_prefers_standard_type():
    assert _mime_package.effective_mime(".py") == "text/x-python"
    assert _mime_package.effective_mime(".parquet") == "application/x-fused-render-parquet"
