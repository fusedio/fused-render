"""The shared file-association table (scripts/file_associations.json) must stay
in sync with the shipping runtime's own registry + icon map. If a template or
icon-variant change drifts the two apart, fail here rather than shipping a stale
"Open with" association set in either packaging pipeline."""
import importlib.util
import io
import sys
from contextlib import redirect_stdout
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
    committed = [
        {"extension": a.extension, "icon": a.icon, "standard_mime": a.standard_mime}
        for a in fa.associations()
    ]
    assert committed == fa.derive_from_winopen(), (
        "scripts/file_associations.json is stale; run "
        "`python scripts/file_associations.py regenerate`"
    )


def _mime_types_output(fa) -> str:
    buf = io.StringIO()
    argv = sys.argv
    sys.argv = ["file_associations.py", "mime-types"]
    try:
        with redirect_stdout(buf):
            fa.main()
    finally:
        sys.argv = argv
    return buf.getvalue().strip()


def test_mime_types_includes_scheme_handler():
    # The .desktop MimeType= must register the app as the handler for
    # fused-render:// (x-scheme-handler/fused-render), alongside the file types,
    # so the OS routes deep links to the AppImage.
    fa = _load_module()
    out = _mime_types_output(fa)
    assert "x-scheme-handler/fused-render;" in out
    assert out.endswith(";")


def test_mime_types_still_lists_file_types():
    # The output lists the effective type per association (standard_mime
    # tiering): a standard type where one exists (.py -> text/x-python) and the
    # custom glob type for an orphan extension (.parquet). The custom glob type
    # for an extension that HAS a standard type is never emitted.
    fa = _load_module()
    out = _mime_types_output(fa)
    assert "text/x-python;" in out
    assert "application/x-fused-render-parquet;" in out
    assert "application/x-fused-render-py;" not in out


def test_mime_and_glob_derivations():
    fa = _load_module()
    by_ext = {a.extension: a for a in fa.associations()}
    py = by_ext[".py"]
    assert py.mime == "application/x-fused-render-py"
    assert py.glob == "*.py"
    assert py.type_name == "PY File (FusedRender)"


def test_standard_mime_tiering():
    fa = _load_module()
    by_ext = {a.extension: a for a in fa.associations()}
    # An extension with a standard shared-mime-info type reuses it (never a
    # custom glob type) — its effective type is the standard one.
    py = by_ext[".py"]
    assert py.standard_mime == "text/x-python"
    assert py.effective_mime == "text/x-python"
    # An orphan extension keeps the custom glob type.
    parquet = by_ext[".parquet"]
    assert parquet.standard_mime is None
    assert parquet.effective_mime == "application/x-fused-render-parquet"


def test_heic_and_heif_are_distinct_standard_types():
    # shared-mime-info registers .heic as its own type (image/heic), distinct
    # from image/heif — mapping .heic to image/heif would attach the handler to
    # the wrong registered type.
    fa = _load_module()
    from fused_render.winopen import standard_mime_for_token

    assert standard_mime_for_token("heic") == "image/heic"
    assert standard_mime_for_token("heif") == "image/heif"

    by_ext = {a.extension: a for a in fa.associations()}
    assert by_ext[".heic"].standard_mime == "image/heic"
    assert by_ext[".heif"].standard_mime == "image/heif"


def test_mime_types_dedupes_and_appends_scheme_handler():
    fa = _load_module()
    types = fa.mime_types(fa.associations())
    # No duplicates even though .jpg/.jpeg and .tif/.tiff share a standard type.
    assert len(types) == len(set(types))
    assert "image/jpeg" in types and "image/tiff" in types
    # The deep-link scheme handler is last (Task B).
    assert types[-1] == "x-scheme-handler/fused-render"
    # A standard type is present; the custom glob for that extension is NOT.
    assert "text/x-python" in types
    assert "application/x-fused-render-py" not in types


def test_mime_xml_defines_glob_types_only_for_orphans():
    fa = _load_module()
    xml = fa._mime_xml(fa.associations())
    # Orphan extension: a custom glob type is defined.
    assert 'type="application/x-fused-render-parquet"' in xml
    assert '<glob pattern="*.parquet"/>' in xml
    # Extension with a standard type: NO custom glob type (no identity hijack).
    assert 'type="application/x-fused-render-py"' not in xml
    assert "text/x-python" not in xml  # the XML never redefines the standard type
