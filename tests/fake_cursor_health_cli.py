#!/usr/bin/env python3
"""A stand-in `cursor-agent` for tests/test_cursor_backend.py: `--version`
prints the release string, `status --format json` answers the shape the
CLI's own `status` command builds (picked by FAKE_CURSOR_STATUS: `in`,
`out`, `partial`, `error`, `garbage`), `acp` hands over to
tests/fake_cursor_acp.py, `login` prints the CLI's success line and exits
0 (or 1 under FAKE_CURSOR_LOGIN=fail), and `-p` answers a one-shot
`{type: "result"}` line (FAKE_CURSOR_ONESHOT=error makes it `is_error`).
Every argv is appended to FAKE_CURSOR_LOG when set."""
import json
import os
import runpy
import sys

args = sys.argv[1:]
log = os.environ.get("FAKE_CURSOR_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": args, "stdin_tty": sys.stdin.isatty(),
                             "no_open_browser": os.environ.get("NO_OPEN_BROWSER")}) + "\n")

if args[:1] == ["--version"]:
    print("2026.11.19-f005e9d")
elif args[:1] == ["status"]:
    mode = os.environ.get("FAKE_CURSOR_STATUS", "in")
    if mode == "in":
        print(json.dumps({"status": "authenticated", "isAuthenticated": True,
                          "userInfo": {"email": "dev@example.com", "teamName": "Acme",
                                       "teamId": 7}}, indent=2))
    elif mode == "out":
        print(json.dumps({"status": "unauthenticated", "isAuthenticated": False,
                          "message": "Not logged in"}))
    elif mode == "partial":
        print(json.dumps({"status": "partially-authenticated", "isAuthenticated": False}))
    elif mode == "error":
        print(json.dumps({"status": "error", "message": "Status check error: net"}))
        sys.exit(1)
    else:
        print("not json at all")
elif args[:1] == ["acp"]:
    sys.argv = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "fake_cursor_acp.py")] + args
    runpy.run_path(sys.argv[0], run_name="__main__")
elif args[:1] == ["login"]:
    if os.environ.get("FAKE_CURSOR_LOGIN") == "fail":
        print("Opening https://cursor.com/loginDeepControl?challenge=secret")
        print("Login failed: timed out")
        sys.exit(1)
    print("Login successful!")
elif "-p" in args:
    prompt = sys.stdin.read()
    if os.environ.get("FAKE_CURSOR_ONESHOT") == "error":
        print(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                          "result": "Authentication required. Please run 'cursor-agent login'"}))
    else:
        print(json.dumps({"type": "system", "subtype": "init"}))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": "Recap: " + str(len(prompt)), "session_id": "x"}))
else:
    sys.exit(2)
