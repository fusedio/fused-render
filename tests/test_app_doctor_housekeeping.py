"""App Doctor's structure family (skills/fused-render-app-doctor/ci/app_check.py):
three plain file-existence checks that make a shared app openable and
recognizable to whoever receives it — `index.html`, a README, and a
non-empty `preview.png`. All three are HIGH severity and fail the run, same
as secrets and device paths (see app_check.py's module docstring). The
script is loaded by path (see _app_check_module.py) since it lives under a
skill directory, not inside the `fused_render` package.
"""
from _app_check_module import app_doctor


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _good_app(tmp_path):
    _write(tmp_path, "index.html", "<html><body>hi</body></html>\n")
    _write(tmp_path, "README.md", "a small app\n")
    _write(tmp_path, "preview.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return tmp_path


def _rules(findings):
    return {f["rule"] for f in findings}


def test_a_complete_app_has_no_structure_findings(tmp_path):
    _good_app(tmp_path)
    findings = app_doctor.check(str(tmp_path))
    assert not any(f["rule"].startswith("structure:") for f in findings)


# -------------------------------------------------------------------- index


def test_a_missing_index_is_flagged(tmp_path):
    _good_app(tmp_path)
    (tmp_path / "index.html").unlink()
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-index" in _rules(findings)


def test_a_present_index_is_not_flagged(tmp_path):
    _good_app(tmp_path)
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-index" not in _rules(findings)


# ------------------------------------------------------------------ readme


def test_a_missing_readme_is_flagged(tmp_path):
    _good_app(tmp_path)
    (tmp_path / "README.md").unlink()
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-readme" in _rules(findings)


def test_a_readme_with_no_extension_still_counts(tmp_path):
    _good_app(tmp_path)
    (tmp_path / "README.md").unlink()
    _write(tmp_path, "README", "a small app\n")
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-readme" not in _rules(findings)


# --------------------------------------------------------------- thumbnail


def test_a_missing_thumbnail_is_flagged(tmp_path):
    _good_app(tmp_path)
    (tmp_path / "preview.png").unlink()
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-thumbnail" in _rules(findings)


def test_a_present_thumbnail_is_not_flagged(tmp_path):
    _good_app(tmp_path)
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-thumbnail" not in _rules(findings)


def test_a_zero_byte_thumbnail_counts_as_missing(tmp_path):
    _good_app(tmp_path)
    _write(tmp_path, "preview.png", b"")
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-thumbnail" in _rules(findings)


# ------------------------------------------------------------------ --check


def test_a_missing_thumbnail_fails_check(tmp_path):
    """A missing preview.png is severity high, the same as every other
    finding this engine reports, so it fails a run under --check."""
    _good_app(tmp_path)
    (tmp_path / "preview.png").unlink()
    findings = app_doctor.check(str(tmp_path))
    hit = next(f for f in findings if f["rule"] == "structure:missing-thumbnail")
    assert hit["severity"] == "high"
