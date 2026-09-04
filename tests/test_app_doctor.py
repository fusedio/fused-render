"""The App Doctor script (skills/fused-render-app-doctor/ci/app_check.py):
secrets and device-specific paths, two of its three severity-high families.

`check()` is the one function both a developer running the script directly
and the CI workflow at skills/fused-render-app-doctor/ci/app-check.yml call —
see the module docstring for why severity lives here and nowhere else. This
file exercises it directly, against fixtures written into `tmp_path`, with no
server and no workspace. The script is loaded by path (see
_app_check_module.py) since it lives under a skill directory, not inside the
`fused_render` package.
"""
from _app_check_module import app_doctor


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _rules(findings):
    return {f["rule"] for f in findings}


def _leaks(findings):
    """This file's own two families — secrets and device paths. `check()`
    also runs the structure family (index.html, README, preview.png), which a
    fixture built only for a leak assertion rarely satisfies in full, so
    those findings show up here too and are not what these tests are about."""
    return [f for f in findings if f["rule"].startswith(("secrets:", "device-path:"))]


# --------------------------------------------------------------- secrets


def test_a_clean_app_yields_nothing(tmp_path):
    _write(tmp_path, "index.html", "<html><body>hi</body></html>\n")
    _write(tmp_path, "README.md", "a small app\n")
    assert _leaks(app_doctor.check(str(tmp_path))) == []


def test_an_aws_key_is_flagged_and_masked(tmp_path):
    _write(tmp_path, "app.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    findings = app_doctor.check(str(tmp_path))
    assert "secrets:aws-access-key" in _rules(findings)
    hit = next(f for f in findings if f["rule"] == "secrets:aws-access-key")
    assert hit["path"] == "app.py"
    assert hit["severity"] == "high"
    assert "AKIAABCDEFGHIJKLMNOP" not in hit["excerpt"]


def test_an_anthropic_key_is_flagged(tmp_path):
    _write(tmp_path, "app.py",
           'key = "sk-ant-api03-' + "a" * 40 + '"\n')
    findings = app_doctor.check(str(tmp_path))
    assert "secrets:anthropic-key" in _rules(findings)


def test_a_private_key_block_is_flagged_and_fully_masked(tmp_path):
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    _write(tmp_path, "creds/key.pem", pem)
    findings = app_doctor.check(str(tmp_path))
    hit = next(f for f in findings if f["rule"] == "secrets:private-key")
    assert hit["path"] == "creds/key.pem"
    assert "MIIEpAIBAAKCAQEA1234567890abcdef" not in hit["excerpt"]
    assert "BEGIN" not in hit["excerpt"]


def test_an_assignment_shaped_secret_is_flagged(tmp_path):
    _write(tmp_path, "config.py", 'DB_PASSWORD = "hunter2-super-secret-value"\n')
    findings = app_doctor.check(str(tmp_path))
    hit = next(f for f in findings if f["rule"] == "secrets:assignment")
    assert "hunter2-super-secret-value" not in hit["excerpt"]
    assert "DB_PASSWORD" in hit["excerpt"]


def test_an_obvious_placeholder_is_not_flagged(tmp_path):
    _write(tmp_path, "config.py",
           'API_KEY = "your-api-key-here"\n'
           'API_TOKEN = "changeme"\n'
           'PASSWORD = "xxxxxxxx"\n')
    findings = app_doctor.check(str(tmp_path))
    assert not any(f["rule"].startswith("secrets:") for f in findings)


def test_a_value_that_only_starts_with_a_placeholder_word_is_flagged(tmp_path):
    """A value has to BE a placeholder end to end to be excused — sharing a
    prefix with one ("my", "test", "none") is not enough, because a real
    credential can start with any of those words too."""
    _write(tmp_path, "config.py",
           'DB_PASSWORD = "mysql-prod-9f3k2xyz"\n'
           'API_KEY = "testkey-prod-abc123456"\n'
           'TOKEN = "nonesuch-real-token-value"\n')
    findings = app_doctor.check(str(tmp_path))
    hits = [f for f in findings if f["rule"] == "secrets:assignment"]
    assert len(hits) == 3


def test_an_unquoted_env_style_assignment_is_flagged(tmp_path):
    """`.env` and docker-compose-style `KEY=value` lines carry no quotes at
    all, and are among the likeliest files in a tree to hold a real
    credential, so the bare form has to be matched too."""
    _write(tmp_path, ".env", "DB_PASSWORD=supersecretvalue123\n")
    _write(tmp_path, "docker-compose.yml",
           "services:\n  db:\n    environment:\n"
           "      POSTGRES_PASSWORD=hunter2hunter2\n")
    findings = app_doctor.check(str(tmp_path))
    assert any(f["rule"] == "secrets:assignment" and f["path"] == ".env"
               for f in findings)
    assert any(f["rule"] == "secrets:assignment" and f["path"] == "docker-compose.yml"
               for f in findings)


def test_a_secret_reports_its_line_number_correctly_past_non_ascii_text(tmp_path):
    """The reported line counts characters, not encoded bytes — a non-ASCII
    comment earlier in the file must not shift the line number of a finding
    that comes after it."""
    _write(tmp_path, "app.py", (
        "# ünïcödé 🎉\n" * 5
        + 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'
    ))
    findings = app_doctor.check(str(tmp_path))
    hit = next(f for f in findings if f["rule"] == "secrets:aws-access-key")
    assert hit["line"] == 6


def test_no_finding_excerpt_ever_contains_the_whole_matched_secret(tmp_path):
    """The masking guarantee, asserted directly rather than left to
    convention: every secret family must hold this, and a new family that
    forgets to mask is a bug this test catches immediately."""
    _write(tmp_path, "app.py", (
        'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'
        'GH = "ghp_' + "a" * 40 + '"\n'
        'SLACK = "xoxb-' + "1234567890-abcdefghijklmno" + '"\n'
        'OPENAI = "sk-' + "b" * 40 + '"\n'
        'GOOGLE = "AIza' + "c" * 35 + '"\n'
        'STRIPE = "sk_live_' + "d" * 30 + '"\n'
        'SECRET = "another-real-looking-secret-value"\n'
    ))
    secrets_planted = [
        "AKIAABCDEFGHIJKLMNOP", "ghp_" + "a" * 40, "xoxb-" + "1234567890-abcdefghijklmno",
        "sk-" + "b" * 40, "AIza" + "c" * 35, "sk_live_" + "d" * 30,
        "another-real-looking-secret-value",
    ]
    findings = app_doctor.check(str(tmp_path))
    assert any(f["rule"].startswith("secrets:") for f in findings)
    for f in findings:
        if not f["rule"].startswith("secrets:"):
            continue
        for secret in secrets_planted:
            assert secret not in f["excerpt"], (secret, f)


# ---------------------------------------------------------- device paths


def test_a_home_folder_path_is_flagged(tmp_path):
    _write(tmp_path, "app.py", 'DATA = "/home/alice/datasets/model.bin"\n')
    findings = app_doctor.check(str(tmp_path))
    hit = next(f for f in findings if f["rule"] == "device-path:hardcoded")
    assert hit["severity"] == "high"
    assert "/home/alice" in hit["excerpt"]


def test_a_macos_users_path_is_flagged(tmp_path):
    _write(tmp_path, "app.py", 'DATA = "/Users/bob/Desktop/cache.json"\n')
    findings = app_doctor.check(str(tmp_path))
    assert any(f["rule"] == "device-path:hardcoded" for f in findings)


def test_a_windows_users_path_is_flagged(tmp_path):
    _write(tmp_path, "app.py", r'DATA = "C:\Users\carol\Documents\file.csv"' + "\n")
    findings = app_doctor.check(str(tmp_path))
    assert any(f["rule"] == "device-path:hardcoded" for f in findings)


def test_an_app_relative_path_is_not_flagged(tmp_path):
    _write(tmp_path, "app.py", 'DATA = "./data/model.bin"\n')
    findings = app_doctor.check(str(tmp_path))
    assert _leaks(findings) == []


def test_a_device_root_inside_a_url_is_not_flagged(tmp_path):
    """A URL's own path component can share a name with a device root
    (`/media/`, `/tmp/`, ...) without saying anything about the local
    filesystem — only a match that is not part of a `scheme://host/...` URL
    is a real device path."""
    _write(tmp_path, "index.html",
           '<img src="https://cdn.example.com/media/logo.png">\n')
    findings = app_doctor.check(str(tmp_path))
    assert _leaks(findings) == []


def test_a_device_root_mentioned_in_prose_is_not_flagged(tmp_path):
    """Prose that merely mentions a path is not a hardcoded path a run
    depends on; only text shaped like a real filesystem path counts."""
    _write(tmp_path, "README.md",
           "Scratch files are written under /tmp/scratch during a run.\n")
    findings = app_doctor.check(str(tmp_path))
    assert _leaks(findings) == []


# -------------------------------------------------------- file enumeration


def test_git_ignored_files_are_not_scanned(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    _write(tmp_path, ".gitignore", "secret.env\n")
    _write(tmp_path, "secret.env", 'TOKEN = "AKIAABCDEFGHIJKLMNOP"\n')
    _write(tmp_path, "app.py", "print('hi')\n")
    findings = app_doctor.check(str(tmp_path))
    assert _leaks(findings) == []


def test_binary_files_are_skipped_without_raising(tmp_path):
    (tmp_path / "asset.bin").write_bytes(b"\x00\x01\x02AKIAABCDEFGHIJKLMNOP")
    assert _leaks(app_doctor.check(str(tmp_path))) == []


def test_bookkeeping_files_are_never_scanned(tmp_path):
    _write(tmp_path, ".claude-split.json", '{"key": "AKIAABCDEFGHIJKLMNOP"}\n')
    _write(tmp_path, ".venv/cache.txt", 'AKIAABCDEFGHIJKLMNOP')
    findings = app_doctor.check(str(tmp_path))
    assert _leaks(findings) == []
