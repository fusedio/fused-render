#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json …` as the bots agent
engine spawns it (docs/bots.md §6). Small and deterministic:

- reads the argv shape the engine passes (`--mcp-config`, `--model`, …),
- spawns the ONE MCP server named in that mcp.json (`mcpServers.bot`) and
  speaks MCP to it (initialize, notifications/initialized, tools/list,
  tools/call), exactly as the real CLI does at connect,
- per user message on stdin, emits `system/init` and plays the next steps of
  the SCRIPT (env `BOTS_FAKE_SCRIPT`, a JSON list) until a `result` step:

      {"text": "..."}               -> assistant text block
      {"tool": name, "args": {...}} -> assistant tool_use, then the MCP call, then a
                                       `user` tool_result event
      {"builtin": "Bash", "args": {...}, "output": "...", "error": false}
                                    -> a Claude Code built-in call as the EA sees it: an
                                       assistant tool_use with the RAW name (no mcp__ prefix),
                                       no MCP call, then the echoed `user` tool_result
      {"result": "final"}           -> `result` subtype=success (ends the turn)
      {"fail": "API Error: …"}      -> `result` is_error=true (ends the turn)
      {"exit": 3}                   -> dies without a result

  a turn with no script left answers `result` "Done.",
- `control_request` interrupt -> `control_response`, then (once, if a turn is
  running) `result subtype=error_during_execution`; the process stays alive.
  Any other control_request gets a plain success `control_response`.

`BOTS_FAKE_LOG` (a path) receives one JSON line per thing a test may want to
assert: {"argv"}, {"tools"} (the listed names), {"user"} (each user message
text), {"call", "args", "result"} (each tool result as the model would read it),
{"control"} (each control request subtype).
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time

ARGV = sys.argv[1:]
LOG = os.environ.get("BOTS_FAKE_LOG")
_log_lock = threading.Lock()
_out_lock = threading.Lock()


def log(**row):
    if not LOG:
        return
    with _log_lock, open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def emit(obj):
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def flag(name, default=None):
    return ARGV[ARGV.index(name) + 1] if name in ARGV else default


class Mcp:
    """A minimal MCP client over the server's stdio."""

    def __init__(self, spec):
        env = dict(os.environ)
        env.update(spec.get("env") or {})
        self.proc = subprocess.Popen([spec["command"]] + list(spec.get("args") or []), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                     encoding="utf-8", bufsize=1)
        self.seq = 0
        self.lock = threading.Lock()
        self.waiting = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            box = self.waiting.pop(msg.get("id"), None)
            if box is not None:
                box.put(msg)

    def request(self, method, params=None):
        """Send; return a Queue that receives the response."""
        with self.lock:
            self.seq += 1
            rid = self.seq
            box = queue.Queue()
            self.waiting[rid] = box
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                              "params": params or {}}) + "\n")
            self.proc.stdin.flush()
        return box

    def notify(self, method):
        with self.lock:
            self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
            self.proc.stdin.flush()


def main():
    log(argv=ARGV)
    script = json.loads(os.environ.get("BOTS_FAKE_SCRIPT") or "[]")
    model = flag("--model", "?")
    with open(flag("--mcp-config"), encoding="utf-8") as f:
        servers = json.load(f)["mcpServers"]
    mcp = Mcp(servers["bot"])
    init = mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "fake-claude", "version": "0"}}).get(timeout=20)
    assert "result" in init, init
    mcp.notify("notifications/initialized")
    listed = mcp.request("tools/list").get(timeout=20)
    names = [t["name"] for t in (listed.get("result") or {}).get("tools", [])]
    log(tools=names, listed_error=listed.get("error"))

    messages = queue.Queue()
    state = {"turn": False, "result_sent": False}
    abort = threading.Event()
    lock = threading.Lock()

    def finish(obj):
        """Emit this turn's ONE result (False when one was already sent)."""
        with lock:
            if not state["turn"] or state["result_sent"]:
                return False
            state["result_sent"] = True
            emit(obj)
            return True

    def reader():
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "user":
                content = msg["message"]["content"]
                text = content if isinstance(content, str) else "".join(
                    b.get("text", "") for b in content if isinstance(b, dict))
                messages.put(text)
            elif msg.get("type") == "control_request":
                sub = (msg.get("request") or {}).get("subtype")
                log(control=sub, request=msg.get("request"))
                emit({"type": "control_response",
                      "response": {"subtype": "success", "request_id": msg.get("request_id"), "response": {}}})
                if sub == "interrupt":
                    abort.set()
                    finish({"type": "result", "subtype": "error_during_execution", "is_error": True,
                            "result": "", "num_turns": 1, "total_cost_usd": 0.0,
                            "usage": {"input_tokens": 0, "output_tokens": 0}})
        messages.put(None)  # stdin closed: exit

    threading.Thread(target=reader, daemon=True).start()

    pos = 0
    turn_no = 0
    # The session this process runs: the one `--resume` named, else a fresh id (the engine persists it).
    session_id = flag("--resume") or ("fake-session-%d" % os.getpid())
    ctx = int(os.environ.get("BOTS_FAKE_CTX") or 0)  # the context size every assistant event reports (rollover tests)
    while True:
        text = messages.get()
        if text is None:
            break
        log(user=text)
        turn_no += 1
        abort.clear()
        with lock:
            state["turn"], state["result_sent"] = True, False
        emit({"type": "system", "subtype": "init", "model": model, "session_id": session_id,
              "tools": ["mcp__bot__" + n for n in names], "mcp_servers": [{"name": "bot", "status": "connected"}]})
        # --replay-user-messages echo
        emit({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
        mid = 0
        done = False
        while pos < len(script) and not done:
            step = script[pos]
            pos += 1
            if abort.is_set():
                if "result" in step or "fail" in step:
                    done = True  # the interrupt already closed this turn
                continue
            mid += 1
            msg_id = "msg_%d_%d" % (turn_no, mid)
            usage = {"input_tokens": ctx or 100 + mid, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                     "output_tokens": 4}
            if "text" in step:
                emit({"type": "assistant", "message": {"id": msg_id, "role": "assistant", "model": model, "usage": usage,
                                                       "content": [{"type": "text", "text": step["text"]}]}})
            elif "tool" in step:
                tid = "toolu_%d_%d" % (turn_no, mid)
                emit({"type": "assistant", "message": {"id": msg_id, "role": "assistant", "model": model, "usage": usage,
                                                       "content": [{"type": "tool_use", "id": tid,
                                                                    "name": "mcp__bot__" + step["tool"],
                                                                    "input": step.get("args") or {}}]}})
                box = mcp.request("tools/call", {"name": step["tool"], "arguments": step.get("args") or {}})
                resp = None
                while resp is None and not abort.is_set():
                    try:
                        resp = box.get(timeout=0.05)
                    except queue.Empty:
                        pass
                if resp is None:
                    log(call=step["tool"], args=step.get("args") or {}, result=None, interrupted=True)
                    continue
                result = resp.get("result") or {"content": [{"type": "text", "text": json.dumps(resp.get("error"))}],
                                                "isError": True}
                texts = "\n".join(b.get("text", "") for b in result.get("content", []) if b.get("type") == "text")
                images = [b.get("mimeType") for b in result.get("content", []) if b.get("type") == "image"]
                log(call=step["tool"], args=step.get("args") or {}, result=texts, images=images,
                    is_error=bool(result.get("isError")))
                emit({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": result.get("content"),
                     "is_error": bool(result.get("isError"))}]}})
            elif "builtin" in step:
                tid = "toolu_%d_%d" % (turn_no, mid)
                emit({"type": "assistant", "message": {"id": msg_id, "role": "assistant", "model": model,
                                                       "content": [{"type": "tool_use", "id": tid, "name": step["builtin"],
                                                                    "input": step.get("args") or {}}]}})
                emit({"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "is_error": bool(step.get("error")),
                     "content": [{"type": "text", "text": step.get("output") or ""}]}]}})
            elif "result" in step:
                finish({"type": "result", "subtype": "success", "is_error": False, "result": step["result"],
                        "num_turns": mid, "total_cost_usd": 0.0012, "session_id": session_id,
                        "usage": {"input_tokens": 100 + mid, "output_tokens": 5}, "stop_reason": "end_turn"})
                done = True
            elif "fail" in step:
                finish({"type": "result", "subtype": "success", "is_error": True, "result": step["fail"],
                        "num_turns": mid, "total_cost_usd": 0.0, "usage": {"input_tokens": 0, "output_tokens": 0}})
                done = True
            elif "exit" in step:
                sys.stdout.flush()
                os._exit(int(step["exit"]))
            elif "sleep" in step:
                end = time.time() + float(step["sleep"])
                while time.time() < end and not abort.is_set():
                    time.sleep(0.02)
        if not done:
            finish({"type": "result", "subtype": "success", "is_error": False, "result": "Done.",
                    "num_turns": mid, "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1}})
        with lock:
            state["turn"] = False
    try:
        mcp.proc.stdin.close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
