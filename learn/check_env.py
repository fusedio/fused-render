"""Report whether the `claude` CLI (Claude Code) is installed on the user's machine.

Called from a Fused Render "Learn" page via:
    await fused.runPython("./check_env.py", {})

Returns:
    {"claude": {"installed": bool, "path": str|None, "version": str|None}}

Runs inside FusedRender's bundled Python 3.12, which does NOT inherit the
user's login-shell PATH. So we resolve the user's real PATH by launching their
login shell, and we scrub PYTHONHOME/PYTHONPATH before shelling out so external
interpreters/binaries don't crash with "No module named 'encodings'".
"""


def main():
    import os
    import subprocess

    def clean_env():
        env = os.environ.copy()
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        return env

    def find_claude():
        shell = os.environ.get("SHELL", "/bin/zsh")
        env = clean_env()

        # 1) Ask the user's login shell where `claude` is (picks up npm /
        #    homebrew / user-local installs that FusedRender's PATH misses).
        for flags in (["-lic"], ["-l", "-c"], ["-ic"]):
            try:
                out = subprocess.run(
                    [shell, *flags, "command -v claude"],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=8,
                )
                for line in out.stdout.splitlines():
                    cand = line.strip()
                    if cand and os.path.exists(cand):
                        return cand
            except Exception:
                pass

        # 2) Fall back to common install locations.
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".claude", "local", "claude"),
            os.path.join(home, ".local", "bin", "claude"),
            "/opt/homebrew/bin/claude",
            "/usr/local/bin/claude",
            os.path.join(home, ".npm-global", "bin", "claude"),
            os.path.join(home, ".bun", "bin", "claude"),
        ]
        # npm global bin, if npm is resolvable via the login shell.
        try:
            npm = subprocess.run(
                [shell, "-lic", "npm prefix -g 2>/dev/null"],
                capture_output=True,
                text=True,
                env=env,
                timeout=8,
            )
            prefix = npm.stdout.strip().splitlines()
            if prefix:
                candidates.append(os.path.join(prefix[-1].strip(), "bin", "claude"))
        except Exception:
            pass

        for cand in candidates:
            if cand and os.path.exists(cand):
                return cand
        return None

    def get_version(path):
        try:
            out = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                env=clean_env(),
                timeout=5,
            )
            v = (out.stdout or out.stderr or "").strip()
            return v or None
        except Exception:
            return None

    try:
        path = find_claude()
    except Exception:
        path = None

    if path:
        return {
            "claude": {
                "installed": True,
                "path": path,
                "version": get_version(path),
            }
        }

    return {"claude": {"installed": False, "path": None, "version": None}}
