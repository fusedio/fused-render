"""`fused_render.server` and `fused_render.cli` import, and the app builds and
runs its startup hooks, with every POSIX-only stdlib module unavailable (what
Windows is) and the `os` calls Windows lacks removed and `_platform.IS_WINDOWS` set, so the bots and the terminal are off.

`sys.platform` stays real here: the stdlib itself branches on it at import and
breaks when it lies. `fused_render._platform` is imported first so the file
lock helper has `fcntl` in hand, then `fcntl` is hidden — any later bare
`import fcntl` on the serve path fails the test."""
import os
import subprocess
import sys
import textwrap

POSIX_OS_ATTRS = ("geteuid", "getuid", "getegid", "getpgid", "killpg", "setsid", "statvfs",
                  "nice", "sysconf", "fork", "getpgrp")
POSIX_ONLY = ("fcntl", "pwd", "grp", "termios", "pty", "resource", "syslog")

SCRIPT = textwrap.dedent("""
    import sys
    import fused_render._platform
    fused_render._platform.IS_WINDOWS = True
    for name in {names!r}:
        sys.modules[name] = None
    import fused_render.cli
    from fused_render.server import create_app
    from fastapi.testclient import TestClient
    import os, shutil, subprocess
    for attr in {posix_attrs!r}:
        if hasattr(os, attr):
            delattr(os, attr)
    app = create_app(sys.argv[1])
    with TestClient(app) as client:
        assert client.get("/api/config").status_code == 200
        assert client.get("/api/bots").status_code == 503
        r = client.post("/api/terminal", json={{}}, headers={{"X-Fused": "1"}})
        assert r.status_code == 501, r.text
    print("ok")
""")


def test_serve_builds_and_starts_without_posix_modules(tmp_path):
    env = {**os.environ, "FUSED_RENDER_HOME": str(tmp_path / "home")}
    out = subprocess.run([sys.executable, "-c", SCRIPT.format(names=POSIX_ONLY, posix_attrs=POSIX_OS_ATTRS), str(tmp_path)],
                         capture_output=True, text=True, timeout=180, env=env)
    assert out.returncode == 0 and out.stdout.strip().endswith("ok"), (out.stderr or out.stdout)[-3000:]
    # Startup hooks log their failures instead of raising, so look there too.
    assert "halted; None in sys.modules" not in out.stderr, out.stderr[-3000:]
    assert "Traceback" not in out.stderr, out.stderr[-3000:]
