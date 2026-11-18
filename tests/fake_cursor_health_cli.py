#!/usr/bin/env python3
"""A stand-in for the `cursor-agent` BINARY as the health probe, the login and
the version check see it — driven by tests/test_cursor_health.py. (The session
host's ACP fake is tests/fake_cursor_acp.py; this one is deliberately separate
so each suite owns its own stand-in.) Shapes are 2026.11.13's: `--version`
prints `YYYY.MM.DD-hash` bare, `status` prints "Not logged in" with exit 0
when signed out, `models` needs a login.

Modes (`FAKE_CURSOR_MODE`, default `in`):
  in          status names an account, models lists the account's models
  in-bare     same, but `models` prints one bare id per line
  out         status says "Not logged in" (exit 0), models fails
  out-exit    status fails (exit 1) with an authentication sentence
  hang        status never answers
  models-fail signed in, but `models` fails
  impostor    `--version` prints something that is not a Cursor version
  login-ok    `login` prints the browser line and exits 0 (and signs in)
  login-fail  `login` prints a refusal line and exits 1
  login-hang  `login` waits forever

`FAKE_CURSOR_LOG` names a file the fake writes `{pid, argv, env, tty}` to on
every run. `FAKE_CURSOR_STATE` names a file whose existence means "logged in"
once `login` succeeded (status reads it in the `out` mode).
"""
import json
import os
import sys
import time

MODE = os.environ.get("FAKE_CURSOR_MODE", "in")
VERSION = "2026.11.13-eacade1"
STATE = os.environ.get("FAKE_CURSOR_STATE")

if os.environ.get("FAKE_CURSOR_LOG"):
    with open(os.environ["FAKE_CURSOR_LOG"], "w") as f:
        json.dump({"pid": os.getpid(), "argv": sys.argv[1:],
                   "tty": os.isatty(0),
                   "env": {k: os.environ.get(k) for k in (
                       "NO_OPEN_BROWSER", "CURSOR_CONFIG_DIR", "PYTHONPATH",
                       "CURSOR_API_KEY", "TERM")}}, f)

args = sys.argv[1:]


def logged_in() -> bool:
    if MODE in ("in", "in-bare", "models-fail"):
        return True
    return bool(STATE and os.path.exists(STATE))


if args == ["--version"]:
    print("1.2.3" if MODE == "impostor" else VERSION)
elif args == ["status"]:
    if MODE == "hang":
        time.sleep(60)
    if MODE == "out-exit":
        print("Error: Authentication required. Run 'agent login'.", file=sys.stderr)
        sys.exit(1)
    if logged_in():
        print("\n \x1b[32m✓\x1b[0m Logged in as dev@example.com\n")
    else:
        print("\n Not logged in\n")
elif args == ["models"]:
    if not logged_in() or MODE == "models-fail":
        print("Error: Authentication required. Run 'agent login'.", file=sys.stderr)
        sys.exit(1)
    if MODE == "in-bare":
        print("auto\ngpt-5\nsonnet-4.5")
    else:
        print("Loading models…\n\nAvailable models\n\n"
              "auto - Auto (default)\n"
              "composer-1 - Composer 1\n"
              "gpt-5 - GPT-5\n"
              "sonnet-4.5 - Claude 4.5 Sonnet (current)\n\n"
              "Tip: use --model <id> to switch.")
elif args == ["login"]:
    print("Opening browser to log in...", flush=True)
    print("https://cursor.com/loginDeepControl?challenge=SECRET&uuid=U1", flush=True)
    if MODE == "login-hang":
        time.sleep(60)
    if MODE == "login-fail":
        print("Authentication timed out.", flush=True)
        sys.exit(1)
    if STATE:
        open(STATE, "w").close()
    print("Logged in.", flush=True)
else:
    print(f"unexpected argv {args}", file=sys.stderr)
    sys.exit(2)
