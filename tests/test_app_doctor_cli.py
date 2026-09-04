"""`fused-render doctor` (cli.py): the CLI a skill drives on request, and the
one command the community-apps CI workflow runs on every push (Task 5 puts
it in repo mode; this file is the single-app case both share).

A real subprocess: the point of this surface is its exit code, and an
in-process call swallows that behind a caught SystemExit rather than proving
what a shell script actually sees.
"""
import json
import os
import subprocess
import sys

CLI = [sys.executable, "-m", "fused_render.cli"]

# The child process's cwd is a pytest tmp_path, nowhere near this repo, so
# `-m fused_render.cli` would otherwise resolve against whatever fused_render
# happens to be installed in the interpreter rather than this worktree's
# copy. Put this worktree first on the child's PYTHONPATH so it always runs
# the code under test.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _clean_app(tmp_path):
    _write(tmp_path, "index.html", (
        '<html><head><meta name="fused-app" />'
        '<meta name="fused-api-version" content="1" /></head>'
        "<body>hi</body></html>\n"
    ))
    _write(tmp_path, "README.md", "a small app\n")
    (tmp_path / "preview.png").write_bytes(b"\x89PNG" + b"0" * 32)
    return tmp_path


def _run(*args, cwd):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [_REPO_ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return subprocess.run(CLI + ["doctor", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=30)


def test_a_clean_folder_exits_zero_and_says_so(tmp_path):
    _clean_app(tmp_path)
    r = _run(str(tmp_path), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "no findings" in r.stdout.lower() or "clean" in r.stdout.lower()


def test_defaults_to_the_working_directory(tmp_path):
    _clean_app(tmp_path)
    r = _run(cwd=tmp_path)
    assert r.returncode == 0, r.stderr


def test_a_fake_key_exits_zero_without_check(tmp_path):
    _clean_app(tmp_path)
    _write(tmp_path, "app.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    r = _run(str(tmp_path), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "app.py" in r.stdout


def test_a_fake_key_exits_nonzero_under_check(tmp_path):
    _clean_app(tmp_path)
    _write(tmp_path, "app.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    r = _run(str(tmp_path), "--check", cwd=tmp_path)
    assert r.returncode != 0
    assert "AKIAABCDEFGHIJKLMNOP" not in r.stdout


def test_low_severity_alone_stays_zero_under_check(tmp_path):
    _write(tmp_path, "index.html", (
        '<html><head><meta name="fused-app" /></head><body>hi</body></html>\n'
    ))  # no README, no thumbnail, no version tag — all LOW findings
    r = _run(str(tmp_path), "--check", cwd=tmp_path)
    assert r.returncode == 0, r.stderr


def test_json_output_is_well_formed_and_masks_secrets(tmp_path):
    _clean_app(tmp_path)
    _write(tmp_path, "app.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    r = _run(str(tmp_path), "--json", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    body = json.loads(r.stdout)
    findings = body["findings"]
    assert any(f["rule"] == "secrets:aws-access-key" for f in findings)
    assert not any("AKIAABCDEFGHIJKLMNOP" in f["excerpt"] for f in findings)


def test_json_and_check_together_still_reports_nonzero(tmp_path):
    _clean_app(tmp_path)
    _write(tmp_path, "app.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    r = _run(str(tmp_path), "--json", "--check", cwd=tmp_path)
    assert r.returncode != 0
    body = json.loads(r.stdout)
    assert body["ok"] is False
