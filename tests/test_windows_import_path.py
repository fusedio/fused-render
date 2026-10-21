"""`fused_render.server` and `fused_render.cli` import with every POSIX-only
stdlib module unavailable (what Windows is), so a bare `import fcntl` on the
serve path cannot come back. `sys.platform` stays real: the stdlib itself
branches on it at import and breaks when it lies."""
import subprocess
import sys
import textwrap

POSIX_ONLY = ("fcntl", "pwd", "grp", "termios", "pty", "resource", "syslog")

SCRIPT = textwrap.dedent("""
    import sys
    for name in {names!r}:
        sys.modules[name] = None
    import fused_render.cli
    import fused_render.server
    print("ok")
""")


def test_serve_import_path_needs_no_posix_modules():
    code = SCRIPT.format(names=POSIX_ONLY)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), out.stderr[-3000:]
