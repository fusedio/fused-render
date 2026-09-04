"""App Doctor's repo mode (fused_render/app_doctor.py): the community-apps
repo is a folder of app folders, not an app itself, and the CI workflow
points `fused-render doctor --check` straight at its root with no extra
flag. `check()` has to notice that and review each app inside instead of
reporting the whole clone as one broken app.

Membership uses the same rule the Showcase tab's own catalog scan uses
(community.py's `_scan_catalog` / `_is_slug`) — a top-level directory whose
name is a slug and which carries a `metadata.json` — so CI and the Showcase
agree on which folders are apps without either importing the other.
"""
from fused_render import app_doctor


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _app(root, slug, extra_files=()):
    _write(root, f"{slug}/metadata.json", '{"name": "x"}\n')
    _write(root, f"{slug}/index.html", (
        '<html><head><meta name="fused-app" />'
        '<meta name="fused-api-version" content="1" /></head>'
        "<body>hi</body></html>\n"
    ))
    _write(root, f"{slug}/README.md", "an app\n")
    (root / slug / "preview.png").write_bytes(b"\x89PNG" + b"0" * 32)
    for rel, content in extra_files:
        _write(root, f"{slug}/{rel}", content)


def test_a_folder_of_apps_reports_per_app_with_app_relative_paths(tmp_path):
    _app(tmp_path, "clean-app")
    _app(tmp_path, "leaky-app",
         extra_files=[("app.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')])
    findings = app_doctor.check(str(tmp_path))
    leak = next(f for f in findings if f["rule"] == "secrets:aws-access-key")
    assert leak["app"] == "leaky-app"
    assert leak["path"] == "app.py"  # relative to leaky-app/, not the repo root
    assert not any(f["app"] == "clean-app" and f["rule"].startswith(("secrets:", "device-path:"))
                   for f in findings)


def test_a_single_app_folder_still_checks_only_itself(tmp_path):
    _app(tmp_path, ".")  # tmp_path itself is the app, not a folder of apps
    findings = app_doctor.check(str(tmp_path))
    assert "app" not in (findings[0] if findings else {})
    for f in findings:
        assert "app" not in f


def test_a_non_app_top_level_directory_is_skipped_not_reported(tmp_path):
    _app(tmp_path, "real-app")
    _write(tmp_path, "not-an-app/README.md", "just some docs\n")
    _write(tmp_path, "also-not-an-app/notes.txt", "AKIAABCDEFGHIJKLMNOP\n")
    findings = app_doctor.check(str(tmp_path))
    assert all(f["app"] == "real-app" for f in findings)


def test_an_invalid_slug_directory_is_not_treated_as_an_app(tmp_path):
    _app(tmp_path, "real-app")
    _write(tmp_path, "Not_A_Slug/metadata.json", "{}\n")
    _write(tmp_path, "Not_A_Slug/app.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    findings = app_doctor.check(str(tmp_path))
    assert all(f["app"] == "real-app" for f in findings)


def test_repo_mode_findings_still_carry_every_base_field(tmp_path):
    _app(tmp_path, "leaky-app",
         extra_files=[("app.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')])
    findings = app_doctor.check(str(tmp_path))
    leak = next(f for f in findings if f["rule"] == "secrets:aws-access-key")
    assert set(leak) >= {"rule", "severity", "path", "line", "excerpt", "app"}
    assert leak["severity"] == "high"
