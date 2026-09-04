"""App Doctor's housekeeping families (fused_render/app_doctor.py): structure,
the API version, and stray generated files — all LOW severity, reported but
never a reason to fail a run (see the module docstring's severity table).

Structure and the thumbnail mirror rules that live elsewhere in the package
(`app_listing.app_entry`, `app_listing.app_preview_image`, `current_apps`'s
`icon.svg`) without importing them — see app_doctor.py's own docstring for
why. Each test below is written against the SAME rule those modules apply,
so a fixture "good" here is good there too.
"""
from fused_render import app_doctor

ENTRY_HTML = (
    '<html><head><meta name="fused-app" /></head><body>hi</body></html>\n'
)


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _good_app(tmp_path, api_version=app_doctor.CURRENT_API_VERSION):
    _write(tmp_path, "index.html", (
        '<html><head><meta name="fused-app" />'
        f'<meta name="fused-api-version" content="{api_version}" />'
        "</head><body>hi</body></html>\n"
    ))
    _write(tmp_path, "README.md", "a small app\n")
    _write(tmp_path, "icon.svg", '<svg xmlns="http://www.w3.org/2000/svg"></svg>\n')
    _write(tmp_path, "preview.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return tmp_path


def _rules(findings):
    return {f["rule"] for f in findings}


# ------------------------------------------------------------------- entry


def test_a_complete_app_has_no_structure_findings(tmp_path):
    _good_app(tmp_path)
    findings = app_doctor.check(str(tmp_path))
    assert not any(f["rule"].startswith(("structure:", "api-version:"))
                   for f in findings)


def test_no_entry_page_is_flagged(tmp_path):
    _write(tmp_path, "index.html", "<html><body>not an app</body></html>\n")
    findings = app_doctor.check(str(tmp_path))
    assert "structure:no-entry" in _rules(findings)


# -------------------------------------------------------------------- icon


def test_a_broken_icon_is_flagged(tmp_path):
    _good_app(tmp_path)
    _write(tmp_path, "icon.svg", "<svg><not-closed></svg\n")
    findings = app_doctor.check(str(tmp_path))
    assert "structure:bad-icon" in _rules(findings)


def test_no_icon_is_not_flagged(tmp_path):
    """icon.svg is optional (current_apps.app_icon returns None when
    absent) — its absence is not a structural problem."""
    _write(tmp_path, "index.html", ENTRY_HTML)
    _write(tmp_path, "README.md", "hi\n")
    _write(tmp_path, "preview.png", b"\x89PNG" + b"0" * 32)
    findings = app_doctor.check(str(tmp_path))
    assert "structure:bad-icon" not in _rules(findings)


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


# --------------------------------------------------------------- pyproject


def test_a_broken_pyproject_is_flagged(tmp_path):
    _good_app(tmp_path)
    _write(tmp_path, "pyproject.toml", "this is not [valid toml\n")
    findings = app_doctor.check(str(tmp_path))
    assert "structure:bad-pyproject" in _rules(findings)


def test_a_valid_pyproject_is_not_flagged(tmp_path):
    _good_app(tmp_path)
    _write(tmp_path, "pyproject.toml", '[project]\nname = "my-app"\n')
    findings = app_doctor.check(str(tmp_path))
    assert "structure:bad-pyproject" not in _rules(findings)


def test_no_pyproject_is_not_flagged(tmp_path):
    _good_app(tmp_path)
    findings = app_doctor.check(str(tmp_path))
    assert "structure:bad-pyproject" not in _rules(findings)


# --------------------------------------------------------------- thumbnail


def test_missing_thumbnail_is_flagged(tmp_path):
    _good_app(tmp_path)
    (tmp_path / "preview.png").unlink()
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-thumbnail" in _rules(findings)


def test_a_zero_byte_thumbnail_counts_as_missing(tmp_path):
    _good_app(tmp_path)
    _write(tmp_path, "preview.png", b"")
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-thumbnail" in _rules(findings)


def test_wrong_case_thumbnail_name_counts_as_missing(tmp_path):
    """Exact membership, case included — the same reason
    app_listing.app_preview_image lists rather than probes."""
    _good_app(tmp_path)
    (tmp_path / "preview.png").unlink()
    _write(tmp_path, "Preview.png", b"\x89PNG" + b"0" * 32)
    findings = app_doctor.check(str(tmp_path))
    assert "structure:missing-thumbnail" in _rules(findings)


# ----------------------------------------------------------------- version


def test_a_page_behind_the_current_api_version_is_flagged(tmp_path):
    _good_app(tmp_path, api_version=0)
    findings = app_doctor.check(str(tmp_path))
    hit = next(f for f in findings if f["rule"] == "api-version:behind")
    assert hit["severity"] == "low"


def test_the_current_api_version_is_not_flagged(tmp_path):
    _good_app(tmp_path, api_version=app_doctor.CURRENT_API_VERSION)
    findings = app_doctor.check(str(tmp_path))
    assert "api-version:behind" not in _rules(findings)


def test_no_entry_page_reports_no_entry_not_a_version_finding(tmp_path):
    _write(tmp_path, "index.html", "<html><body>not an app</body></html>\n")
    findings = app_doctor.check(str(tmp_path))
    assert "api-version:behind" not in _rules(findings)


# ---------------------------------------------------------- stray files


def test_a_stray_cache_dir_is_flagged(tmp_path):
    _good_app(tmp_path)
    _write(tmp_path, "__pycache__/app.cpython-312.pyc", "junk")
    findings = app_doctor.check(str(tmp_path))
    assert any(f["rule"] == "generated:stray-file" for f in findings)


def test_a_stray_log_file_is_flagged(tmp_path):
    _good_app(tmp_path)
    _write(tmp_path, "run.log", "started\n")
    findings = app_doctor.check(str(tmp_path))
    assert any(f["rule"] == "generated:stray-file" and f["path"] == "run.log"
               for f in findings)


def test_a_stray_sqlite_db_is_flagged(tmp_path):
    _good_app(tmp_path)
    _write(tmp_path, "cache.sqlite3", "junk")
    findings = app_doctor.check(str(tmp_path))
    assert any(f["rule"] == "generated:stray-file" and f["path"] == "cache.sqlite3"
               for f in findings)


def test_generated_data_inside_dot_fused_is_not_flagged(tmp_path):
    """.fused/ is the app's own machine-local state folder (app_git.py) —
    never app history, and never a housekeeping finding either."""
    _good_app(tmp_path)
    _write(tmp_path, ".fused/cache.sqlite3", "junk")
    findings = app_doctor.check(str(tmp_path))
    assert not any(f["rule"] == "generated:stray-file" for f in findings)


def test_ordinary_source_files_are_not_flagged_as_stray(tmp_path):
    _good_app(tmp_path)
    _write(tmp_path, "helpers.py", "def f():\n    return 1\n")
    findings = app_doctor.check(str(tmp_path))
    assert not any(f["rule"] == "generated:stray-file" for f in findings)
