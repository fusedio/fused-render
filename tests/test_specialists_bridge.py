"""The specialist task tools and the services policy seam on the per-run permission server (PLAN A.8, B.4).

The server is driven over its own stdio JSON-RPC against a stub HTTP server standing in for `/api/specialists`;
the `claude` CLI is never invoked.
"""
import glob
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from _mcp_stdio import MCPServer

SERVER = os.path.join("fused_render", "claude_agent", "permission_server.py")
TOOLS = ["specialist_list", "specialist_pull", "specialist_check", "specialist_note", "specialist_breakout"]


class _Stub:
    def __init__(self):
        self.requests = []
        self.policy = "ask"
        self.offer_error = None
        self.grant_status = 200
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                stub.requests.append(("GET", self.path, None))
                if self.path.startswith("/api/specialists/policy?"):
                    return self._json(200, {"answer": stub.policy})
                return self._json(404, {"error": "?"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append(("POST", self.path, body))
                name = self.path.rsplit("/", 1)[-1]
                if self.headers.get("X-Fused") != "1":
                    return self._json(403, {"error": "no"})
                if self.path == "/api/specialists/policy/grant":
                    return self._json(stub.grant_status, {"ok": stub.grant_status == 200})
                if name == "list":
                    return self._json(200, {"text": "- Canvas (abc): canvases"})
                if name == "pull":
                    if body.get("specialist") not in ("abc", "Canvas"):
                        return self._json(404, {"error": 'The specialist "x" no longer exists.'})
                    return self._json(200, {"text": "[fused:specialist-guidance receipt=r-1]\n--- memory ---\nm",
                                            "receipt_id": "r-1"})
                if name == "check":
                    return self._json(200, {"text": "Looks fine.\n--- end ---", "row": "1-a"})
                if name == "note":
                    return self._json(200, {"text": "Sent (n-1).", "note_id": "n-1", "specialist": "abc"})
                if name == "offer":
                    if stub.offer_error:
                        return self._json(404, {"error": stub.offer_error})
                    return self._json(200, {"ok": True, "id": "abc", "name": "Canvas"})
                if name == "offer-result":
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "?"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def posts(self, name):
        return [b for m, p, b in self.requests if m == "POST" and p == "/api/specialists/tools/" + name]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub():
    s = _Stub()
    yield s
    s.close()


def _perm(tmp_path):
    perm = tmp_path / "runs" / "run7" / "perm"
    perm.mkdir(parents=True, exist_ok=True)
    return perm


def _server(tmp_path, origin="", **env):
    base = {"FUSED_RENDER_SPECIALIST_ORIGIN": origin, "FUSED_RENDER_EXPERT_ORIGIN": "",
            "FUSED_RENDER_S3_ORIGIN": "", "FUSED_RENDER_TERMINAL_ORIGIN": "", "FUSED_RENDER_ORIGIN": ""}
    base.update(env)
    s = MCPServer([sys.executable, os.path.abspath(SERVER), str(_perm(tmp_path))], env=base)
    s.initialize()
    return s


def _call(server, name, args=None, timeout=10.0):
    res = server.call("tools/call", {"name": name, "arguments": args or {}}, timeout=timeout)["result"]
    return res["content"][0]["text"], bool(res.get("isError"))


def test_tools_absent_without_the_env(tmp_path):
    s = _server(tmp_path)
    try:
        assert [t["name"] for t in s.call("tools/list")["result"]["tools"]] == ["approve"]
        assert "unknown tool" in s.call("tools/call", {"name": "specialist_list", "arguments": {}})["error"]["message"]
    finally:
        s.close()


def test_tools_listed_with_schemas(tmp_path, stub):
    s = _server(tmp_path, stub.origin)
    try:
        tools = {t["name"]: t for t in s.call("tools/list")["result"]["tools"]}
        assert [n for n in tools if n.startswith("specialist_")] == TOOLS
        for t in TOOLS:
            assert tools[t]["inputSchema"]["additionalProperties"] is False
        assert tools["specialist_breakout"]["inputSchema"]["required"] == ["specialist", "problem", "tried", "look_at"]
        assert tools["specialist_note"]["inputSchema"]["required"] == ["kind", "claim", "basis"]
        assert tools["specialist_check"]["inputSchema"]["properties"]["stage"]["enum"] == ["plan", "done"]
        assert "expert" not in json.dumps([tools[t] for t in TOOLS]).lower()
        assert "each knows one subject in depth" in tools["specialist_list"]["description"]
        assert "give the entry id ([m-xxxx])" in tools["specialist_note"]["description"]
    finally:
        s.close()


def test_list_pull_check_note_carry_the_run_id(tmp_path, stub):
    s = _server(tmp_path, stub.origin)
    try:
        assert _call(s, "specialist_list") == ("- Canvas (abc): canvases", False)
        text, err = _call(s, "specialist_pull", {"specialist": "Canvas"})
        assert not err and text.startswith("[fused:specialist-guidance receipt=r-1]")
        text, err = _call(s, "specialist_pull", {"specialist": "gone"})
        assert err and "no longer exists" in text
        text, err = _call(s, "specialist_check", {"specialist": "abc", "stage": "plan", "summary": "do X"})
        assert not err and "‒‒‒ end ---" in text  # defused frame lines
        text, err = _call(s, "specialist_note", {"kind": "gap", "claim": "c", "basis": "guess"})
        assert not err and "n-1" in text
        for name in ("list", "pull", "check", "note"):
            assert all(b["run_id"] == "run7" for b in stub.posts(name))
        assert stub.posts("note")[0] == {"kind": "gap", "basis": "guess", "claim": "c", "run_id": "run7"}
    finally:
        s.close()


@pytest.mark.parametrize("name,args", [
    ("specialist_pull", {}),
    ("specialist_check", {"specialist": "abc", "stage": "later", "summary": "x"}),
    ("specialist_check", {"specialist": "abc", "stage": "plan", "summary": "x" * 4001}),
    ("specialist_note", {"kind": "rumour", "claim": "c", "basis": "guess"}),
    ("specialist_note", {"kind": "gap", "claim": "c" * 301, "basis": "guess"}),
    ("specialist_note", {"kind": "gap", "claim": "c", "basis": "hunch"}),
    ("specialist_breakout", {"specialist": "abc", "problem": "p", "tried": "t"}),
])
def test_bad_arguments_never_reach_the_server(tmp_path, stub, name, args):
    s = _server(tmp_path, stub.origin)
    try:
        text, err = _call(s, name, args)
        assert err
        assert not [r for r in stub.requests if r[0] == "POST"]
    finally:
        s.close()


BRIEF = {"specialist": "Canvas", "problem": "Sync stalls", "tried": "retry", "look_at": "canvas.toml"}


def _await_req(perm, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [p for p in glob.glob(str(perm / "*.req.json"))]
        if found:
            return found[0]
        time.sleep(0.05)
    raise AssertionError("no card parked")


def _answer(req_path, payload):
    with open(req_path.replace(".req.json", ".res.json"), "w") as fh:
        json.dump(payload, fh)


def test_offer_accepted_with_an_edited_brief(tmp_path, stub):
    s = _server(tmp_path, stub.origin)
    try:
        pending = s.send_async("tools/call", {"name": "specialist_breakout", "arguments": BRIEF})
        req_path = _await_req(_perm(tmp_path))
        req = json.load(open(req_path))
        assert req["tool"] == "mcp__fused_render__specialist_breakout"
        assert req["input"] == {"specialist": "abc", "name": "Canvas", "problem": "Sync stalls",
                                "tried": "retry", "look_at": "canvas.toml"}
        _answer(req_path, {"decision": "allow", "scope": "once",
                           "brief": {"problem": "Sync stalls on save", "tried": "retry", "look_at": "x"}})
        text = pending.result(10)["result"]["content"][0]["text"]
        assert text.startswith("The user opened a breakout with the Canvas specialist for: Sync stalls on save.")
        (res,) = stub.posts("offer-result")
        assert res["outcome"] == "open" and res["brief"]["problem"] == "Sync stalls on save"
        assert res["request_id"] == os.path.basename(req_path)[:-len(".req.json")]
    finally:
        s.close()


def test_offer_declined(tmp_path, stub):
    s = _server(tmp_path, stub.origin)
    try:
        pending = s.send_async("tools/call", {"name": "specialist_breakout", "arguments": BRIEF})
        _answer(_await_req(_perm(tmp_path)), {"decision": "deny", "scope": "once"})
        text = pending.result(10)["result"]["content"][0]["text"]
        assert text.startswith("The user chose to keep trying.")
        assert stub.posts("offer-result")[0]["outcome"] == "declined"
    finally:
        s.close()


def test_offer_unanswered(tmp_path, stub):
    s = _server(tmp_path, stub.origin, FUSED_RENDER_SPECIALIST_OFFER_WAIT_S="0.5")
    try:
        text, err = _call(s, "specialist_breakout", BRIEF, timeout=10)
        assert text == "Nobody answered the offer. Continue with the problem yourself."
        assert stub.posts("offer-result")[0]["outcome"] == "unanswered"
        # the timeout is latched on the card, so it stops reading as waiting
        (res,) = glob.glob(str(_perm(tmp_path) / "*.res.json"))
        assert json.load(open(res))["reason"] == "timeout"
    finally:
        s.close()


def test_offer_refused_by_the_server_raises_no_card(tmp_path, stub):
    stub.offer_error = "A breakout cannot open a breakout."
    s = _server(tmp_path, stub.origin)
    try:
        text, err = _call(s, "specialist_breakout", BRIEF)
        assert "A breakout cannot open a breakout." in text
        assert not glob.glob(str(_perm(tmp_path) / "*.req.json"))
    finally:
        s.close()


def test_offer_tool_through_approve_never_parks(tmp_path, stub):
    s = _server(tmp_path, stub.origin)
    try:
        text, _ = _call(s, "approve", {"tool_name": "mcp__fused_render__specialist_breakout", "input": {}})
        assert json.loads(text)["behavior"] == "allow"
        assert not glob.glob(str(_perm(tmp_path) / "*.req.json"))
    finally:
        s.close()


# ----------------------------------------------------------------- policy ---
SERVICE = "mcp__specialist_services__slack_post_message"


def _grants(stub):
    return [b for m, p, b in stub.requests if p == "/api/specialists/policy/grant"]


def _policy_server(tmp_path, stub, allow_file=None):
    env = {"FUSED_SPECIALIST_POLICY": f"{stub.origin}|abc|breakout", "FUSED_SPECIALIST_GRANT": "sekrit"}
    if allow_file:
        env["FUSED_PERM_ALLOW_FILE"] = str(allow_file)
    return _server(tmp_path, **env)


def test_policy_allow_skips_the_card(tmp_path, stub):
    stub.policy = "allow"
    (tmp_path / "runs" / "run7").mkdir(parents=True)
    (tmp_path / "runs" / "run7" / "meta.json").write_text(json.dumps({"session_id": "S1"}))
    s = _policy_server(tmp_path, stub)
    try:
        text, _ = _call(s, "approve", {"tool_name": SERVICE, "input": {"text": "hi"}})
        assert json.loads(text)["behavior"] == "allow"
        assert not glob.glob(str(_perm(tmp_path) / "*.req.json"))
        (get,) = [r for r in stub.requests if r[0] == "GET"]
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(get[1]).query)
        assert q == {"specialist": ["abc"], "kind": ["breakout"], "session_id": ["S1"], "tool": [SERVICE]}
        assert _grants(stub) == []  # an allowed service needs no grant
    finally:
        s.close()


def test_policy_ask_always_ignores_the_always_store(tmp_path, stub):
    stub.policy = "ask_always"
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"tools": [SERVICE]}))
    s = _policy_server(tmp_path, stub, allow)
    try:
        pending = s.send_async("tools/call", {"name": "approve",
                                              "arguments": {"tool_name": SERVICE, "input": {"text": "hi"}}})
        req_path = _await_req(_perm(tmp_path))
        _answer(req_path, {"decision": "deny", "scope": "once"})
        text = pending.result(10)["result"]["content"][0]["text"]
        assert json.loads(text)["behavior"] == "deny"
        assert _grants(stub) == []  # a denied card grants nothing
    finally:
        s.close()


def test_approved_card_posts_a_grant_first(tmp_path, stub):
    stub.policy = "ask_always"
    (tmp_path / "runs" / "run7").mkdir(parents=True)
    (tmp_path / "runs" / "run7" / "meta.json").write_text(json.dumps({"resumed_from": "S2"}))
    s = _policy_server(tmp_path, stub)
    try:
        pending = s.send_async("tools/call", {"name": "approve",
                                              "arguments": {"tool_name": SERVICE, "input": {"text": "hi"}}})
        _answer(_await_req(_perm(tmp_path)), {"decision": "allow", "scope": "once"})
        assert json.loads(pending.result(10)["result"]["content"][0]["text"])["behavior"] == "allow"
        assert _grants(stub) == [{"specialist": "abc", "kind": "breakout", "session_id": "S2", "tool": SERVICE,
                                  "secret": "sekrit"}]
    finally:
        s.close()


def test_a_grant_that_cannot_be_recorded_is_a_deny(tmp_path, stub):
    stub.policy, stub.grant_status = "ask", 403
    s = _policy_server(tmp_path, stub)
    try:
        pending = s.send_async("tools/call", {"name": "approve", "arguments": {"tool_name": SERVICE, "input": {}}})
        _answer(_await_req(_perm(tmp_path)), {"decision": "allow", "scope": "once"})
        out = json.loads(pending.result(10)["result"]["content"][0]["text"])
        assert out["behavior"] == "deny" and "could not be recorded" in out["message"]
    finally:
        s.close()


def test_policy_ask_honours_the_always_store_with_a_grant(tmp_path, stub):
    stub.policy = "ask"
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"tools": [SERVICE]}))
    s = _policy_server(tmp_path, stub, allow)
    try:
        text, _ = _call(s, "approve", {"tool_name": SERVICE, "input": {}})
        assert json.loads(text)["behavior"] == "allow"
        assert len(_grants(stub)) == 1
    finally:
        s.close()


def test_policy_seam_only_for_service_tools_and_with_env(tmp_path, stub):
    stub.policy = "allow"
    s = _policy_server(tmp_path, stub)
    try:
        pending = s.send_async("tools/call", {"name": "approve",
                                              "arguments": {"tool_name": "Bash", "input": {"command": "ls"}}})
        _answer(_await_req(_perm(tmp_path)), {"decision": "deny", "scope": "once"})
        pending.result(10)
        assert not [r for r in stub.requests if r[0] == "GET"]
    finally:
        s.close()


def test_policy_unreachable_means_ask(tmp_path):
    s = _server(tmp_path, FUSED_SPECIALIST_POLICY="http://127.0.0.1:9|abc|main")
    try:
        pending = s.send_async("tools/call", {"name": "approve", "arguments": {"tool_name": SERVICE, "input": {}}})
        _answer(_await_req(_perm(tmp_path)), {"decision": "deny", "scope": "once"})
        assert json.loads(pending.result(10)["result"]["content"][0]["text"])["behavior"] == "deny"
    finally:
        s.close()
