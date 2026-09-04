"""The App Doctor engine (fused_render/app_doctor.py): secrets and
device-specific paths, the two HIGH-severity families it ships first.

`check()` is the one function both `fused-render doctor` and the
community-apps CI workflow call — see the module docstring for why severity
lives here and nowhere else. This file exercises it directly, against
fixtures written into `tmp_path`, with no server and no workspace.
"""
import os

from fused_render import app_doctor


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _rules(findings):
    return {f["rule"] for f in findings}


def _leaks(findings):
    """This file's own two families — secrets and device paths. `check()`
    also runs housekeeping checks (structure, version, stray files) added in
    later tasks; a fixture built only for a leak assertion is not a complete
    app, so those show up here too and are not what these tests are about."""
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
