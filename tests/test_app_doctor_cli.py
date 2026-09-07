"""`python app_check.py` (skills/fused-render-app-doctor/ci/app_check.py): the
command the CI workflow at skills/fused-render-app-doctor/ci/app-check.yml
runs on every push, once per app folder — this file exercises the single-app
case that loop is built on.

A real subprocess, through the same interpreter running the tests: the point
of this surface is its exit code, and an in-process call would swallow that
behind a caught SystemExit rather than proving what the workflow's shell
actually sees.
"""
import os
import subprocess
import sys

_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir,
    "skills", "fused-render-app-doctor", "ci", "app_check.py",
)


def _write(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _clean_app(tmp_path):
    _write(tmp_path, "index.html", "<html><body>hi</body></html>\n")
    _write(tmp_path, "README.md", "a small app\n")
    (tmp_path / "preview.png").write_bytes(b"\x89PNG" + b"0" * 32)
    return tmp_path


def _run(*args, cwd):
    return subprocess.run([sys.executable, _SCRIPT, *args], cwd=str(cwd),
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


def test_a_fake_key_reddens_the_run(tmp_path):
    _clean_app(tmp_path)
    _write(tmp_path, "app.py", 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    r = _run(str(tmp_path), cwd=tmp_path)
    assert r.returncode != 0
    assert "secrets:aws-access-key" in r.stdout
    assert "app.py" in r.stdout
    assert "AKIAABCDEFGHIJKLMNOP" not in r.stdout


def test_findings_across_files_and_families_print_one_pinned_line_each_in_sorted_order(tmp_path):
    """Two files, two families: a leaked key in `app.py` and a hardcoded
    device path in `config.py`. Pins the exact `path:line: rule: excerpt`
    shape and the path-then-line-then-rule sort, so the same app always
    prints the same bytes in the same order."""
    _clean_app(tmp_path)
    _write(tmp_path, "app.py", '#\nAWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    _write(tmp_path, "config.py", 'PATH = "/home/user/project/secret.txt"\n')
    r = _run(str(tmp_path), cwd=tmp_path)
    assert r.returncode != 0
    lines = r.stdout.splitlines()
    assert lines == [
        "app.py:2: secrets:aws-access-key: AK****************OP",
        "config.py:1: device-path:hardcoded: /home/user/project/secret.txt",
        "2 findings",
    ]


def test_a_missing_thumbnail_exits_nonzero(tmp_path):
    """A missing preview.png is a structure finding, and every finding this
    script reports is HIGH severity — it fails the run the same way a leaked
    key does."""
    _clean_app(tmp_path)
    (tmp_path / "preview.png").unlink()
    r = _run(str(tmp_path), cwd=tmp_path)
    assert r.returncode != 0
    assert "preview.png" in r.stdout


def test_a_path_that_does_not_exist_fails_loudly(tmp_path):
    # check() itself swallows the OSError a missing path raises when it tries
    # to list it, and reports an empty finding list — a silent "clean"
    # verdict for a path that was never reviewed at all. main() has to catch
    # this before it ever reaches check().
    missing = tmp_path / "does-not-exist"
    r = _run(str(missing), cwd=tmp_path)
    assert r.returncode != 0
    assert "not a directory" in (r.stderr + r.stdout).lower()
