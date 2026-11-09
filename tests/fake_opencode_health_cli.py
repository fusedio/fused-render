#!/usr/bin/env python3
"""A stand-in for the `opencode` BINARY as the health probe, the login and
the version check see it — driven by tests/test_opencode_health.py. (The
session host's richer ACP fake is tests/fake_opencode_acp.py; this one is
deliberately separate so each suite owns its own stand-in.) Shapes are
OpenCode 1.19.5's (docs/OPENCODE_BACKEND.md D10).

`opencode --version` prints `1.19.5` bare. `opencode acp` speaks just enough
ACP for the health probe: `initialize` (OpenCode's `agentInfo` and its one
`authMethods` row) and `session/new` (`{sessionId, configOptions}` — the
`model` select with a few `fused/` rows among the provider-wide hundreds,
and the `mode` select). `opencode auth login` prints a provider picker's
first lines and exits the way `FAKE_OPENCODE_MODE` says.

Modes (`FAKE_OPENCODE_MODE`, default `in`):
  in         session/new answers a result (signed in)
  in-local   same, but the current model is `fused/...`
  out        -32000 "no provider configured"
  other      -32000 with other words (unknown)
  internal   -32603 with the signed-out words (unknown)
  garbage    non-JSON on stdout, then exit 1
  noise      a server request and a notification before the result
  hang       never answers session/new
  stubborn   hang, and ignore SIGTERM
  login-ok   `auth login` exits 0
  login-fail `auth login` exits 1 after a refusal line
  login-hang `auth login` waits forever

`FAKE_OPENCODE_LOG` names a file the fake writes `{pid, cwd, argv, got,
env, tty}` to on every message.
"""
import json
import os
import signal
import sys
import time

MODE = os.environ.get("FAKE_OPENCODE_MODE", "in")
VERSION = "1.19.5"

MODEL_OPTIONS = [
    {"value": "anthropic/claude-sonnet-5-5", "name": "Claude Sonnet 5.5"},
    {"value": "fused/qwen3-8b", "name": "Qwen3 8B (local)"},
    {"value": "openai/gpt-5.5", "name": "GPT-5.5"},
    {"value": "fused/qwen3-8b", "name": "dup"},
    {"value": "fused/gemma-4-12b", "name": "Gemma 4 12B (local)"},
    {"name": "no value"},
    "junk",
    {"value": "google/gemini-3-pro", "name": "Gemini 3 Pro"},
]


def session_result():
    current = "fused/qwen3-8b" if MODE == "in-local" else "anthropic/claude-sonnet-5-5"
    return {
        "sessionId": "ses_7f3a2b1c9d",
        "configOptions": [
            {"id": "model", "type": "select", "currentValue": current,
             "options": MODEL_OPTIONS},
            {"id": "mode", "type": "select", "currentValue": "build",
             "options": [{"value": "build", "name": "Build"},
                         {"value": "plan", "name": "Plan"}]},
        ],
    }


got = []


def log(**extra):
    path = os.environ.get("FAKE_OPENCODE_LOG")
    if not path:
        return
    with open(path, "w") as f:
        json.dump({"pid": os.getpid(), "cwd": os.getcwd(), "got": got,
                   "argv": sys.argv[1:],
                   "env": {k: os.environ.get(k) for k in (
                       "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_CONFIG_CONTENT",
                       "OPENCODE_CONFIG_DIR", "PYTHONPATH")},
                   **extra}, f)


def send(msg):
    print(json.dumps(msg), flush=True)


def main():
    args = sys.argv[1:]
    if args == ["--version"]:
        print(VERSION)
        return 0
    if args == ["auth", "login"]:
        log(tty=os.isatty(0))
        print("┌  Add credential", flush=True)
        print("│  Select provider", flush=True)
        if MODE == "login-hang":
            time.sleep(60)
        if MODE == "login-fail":
            print("Enter your API key: sk-SECRET-KEY", flush=True)
            print("Error: provider refused the key", flush=True)
            return 1
        print("└  Done", flush=True)
        return 0
    assert args == ["acp"], args
    if MODE == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    log()
    for line in sys.stdin:
        msg = json.loads(line)
        got.append(msg)
        log()
        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "authMethods": [{"id": "opencode-login", "name": "Login with opencode",
                                 "description": "Run `opencode auth login` in the terminal"}],
                "agentInfo": {"name": "OpenCode", "version": VERSION}}})
        elif method == "session/new":
            if MODE in ("hang", "stubborn"):
                time.sleep(60)
            if MODE == "garbage":
                print("not json at all", flush=True)
                return 1
            if MODE == "noise":
                send({"jsonrpc": "2.0", "id": 2, "method": "session/request_permission",
                      "params": {}})
                send({"jsonrpc": "2.0", "method": "session/update",
                      "params": {"update": {"sessionUpdate": "available_commands_update"}}})
            if MODE == "out":
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32000, "message": "no provider configured"}})
            elif MODE == "other":
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32000, "message": "config file is not valid JSON"}})
            elif MODE == "internal":
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {
                    "code": -32603, "message": "no provider configured"}})
            else:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": session_result()})
    # stdin EOF: the real ACP server exits here.
    return 0


if __name__ == "__main__":
    sys.exit(main())
