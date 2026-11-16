"""The S3 MCP tools (`s3_search`, `s3_read`) on the per-run permission server,
and how agent.py wires them into every backend.

Modelled on test_claude_terminal_tools.py: the server is driven over its own
stdio JSON-RPC against a stub HTTP server standing in for fused-render's
`/api/remote*` and `/api/remote-index/search` routes. The `claude` CLI is never
invoked.

Rules pinned here: the tools appear only when the run was given
`FUSED_RENDER_S3_ORIGIN`, which agent.py stamps only with a server origin AND
the Canvases flag on; both are pre-allowed for Claude together with a Read rule
on the remote cache root; a range is inline only as small UTF-8, otherwise a
file under the stamped scratch folder; a whole object over the limit is refused
without `allow_large`; with the flag off the spawn is unchanged.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from _mcp_stdio import MCPServer
from test_claude_app_state import SERVER, _load, _spawn

URL = "s3://fused-asset/data/a.csv"
CSV = b"id,name\n1,alpha\n2,beta\n"
PARQUET = b"PAR1" + bytes(range(256)) * 300 + b"M#\x02\x00PAR1"
HITS = {"bucket": "fused-asset", "mode": "search",
        "hits": [{"url": "s3://fused-asset/data/", "is_dir": True, "size": None,
                  "modified": None}], "truncated": False, "cursor": None}


class _Stub:
    def __init__(self):
        self.requests = []
        self.objects = {URL: CSV, "s3://fused-asset/data/b.parquet": PARQUET,
                        "s3://fused-asset/data/big.txt": b"x" * 40000}
        self.search_status = 200
        self.short = False  # declare more bytes than are sent, then hang up
        self.stat = {"kind": "file", "size": 500 * 1024 * 1024, "cached": False,
                     "path": None, "mtime": 1700000000.0, "etag": "e1",
                     "warn_bytes": 100 * 1024 * 1024}
        self.polls = 0
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
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                stub.requests.append(("GET", url.path, q, dict(self.headers)))
                if url.path == "/api/remote-index/search":
                    if stub.search_status == 503:
                        return self._json(503, {"error": "The index is still loading.",
                                                "code": "index_not_ready"})
                    return self._json(200, HITS)
                if url.path == "/api/remote/read":
                    data = stub.objects.get(q.get("url"))
                    if data is None:
                        return self._json(404, {"error": "does not exist", "code": "not_found"})
                    off = int(q.get("offset", 0))
                    start = max(0, len(data) + off) if off < 0 else off
                    if start >= len(data):
                        return self._json(416, {"error": "offset outside the %d-byte object"
                                                % len(data), "code": "range_not_satisfiable"})
                    end = len(data) if "length" not in q else min(len(data), start + int(q["length"]))
                    body = data[start:end]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    if stub.short:
                        body = body[:len(body) // 2]
                        self.close_connection = True
                    self.send_header("X-Remote-Size", str(len(data)))
                    self.send_header("X-Remote-Etag", "abc")
                    self.send_header("X-Remote-Modified", "2026-04-26T02:05:41Z")
                    self.send_header("X-Remote-Range", "%d-%d" % (start, end))
                    self.end_headers()
                    self.wfile.write(body)
                    return None
                if url.path == "/api/remote/stat":
                    return self._json(200, dict(stub.stat, url=q.get("url")))
                if url.path == "/api/remote/fetch/status":
                    stub.polls += 1
                    if stub.polls < 2:
                        return self._json(200, {"state": "running", "done": 1, "total": 2})
                    return self._json(200, {"state": "done", "path": "/cache/x.bin"})
                return self._json(404, {"error": "?"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append(("POST", self.path, body, dict(self.headers)))
                return self._json(200, {"id": "sys:remote-fetch:1", "state": "running",
                                        "path": None})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub():
    s = _Stub()
    yield s
    s.close()


def _server(tmp_path, env=None):
    perm = tmp_path / "perm"
    perm.mkdir(exist_ok=True)
    s = MCPServer([sys.executable, os.path.abspath(SERVER), str(perm)], env=env)
    s.initialize()
    return s


def _env(tmp_path, stub):
    return {"FUSED_RENDER_S3_ORIGIN": stub.origin,
            "FUSED_RENDER_S3_SCRATCH": str(tmp_path / "cache" / ".ranges" / "run1"),
            "FUSED_RENDER_TERMINAL_ORIGIN": ""}


def _call(server, name, args=None):
    res = server.call("tools/call", {"name": name, "arguments": args or {}})["result"]
    payload = json.loads(res["content"][0]["text"])
    return payload, bool(res.get("isError"))


def test_tools_absent_without_an_s3_origin(tmp_path):
    s = _server(tmp_path, {"FUSED_RENDER_S3_ORIGIN": "", "FUSED_RENDER_TERMINAL_ORIGIN": ""})
    try:
        names = [t["name"] for t in s.call("tools/list")["result"]["tools"]]
        assert names == ["approve"]
        err = s.call("tools/call", {"name": "s3_read", "arguments": {"url": URL}})
        assert "unknown tool" in err["error"]["message"]
    finally:
        s.close()


def test_tools_listed_after_the_terminals_with_their_schemas(tmp_path, stub):
    s = _server(tmp_path, {"FUSED_RENDER_S3_ORIGIN": stub.origin,
                           "FUSED_RENDER_TERMINAL_ORIGIN": stub.origin})
    try:
        tools = s.call("tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == [
            "approve", "terminal_list", "terminal_read", "terminal_send",
            "s3_search", "s3_read"]
        search, read = tools[-2], tools[-1]
        assert set(search["inputSchema"]["properties"]) == {"query", "path", "limit", "cursor"}
        assert "required" not in search["inputSchema"]
        assert set(read["inputSchema"]["properties"]) == {
            "url", "offset", "length", "save", "allow_large"}
        assert read["inputSchema"]["required"] == ["url"]
        for text in (search["description"], read["description"]):
            assert "untrusted data" in text and "never curl" in text
            assert "fused_asset" not in text
        assert "three hits per file name" in search["description"]
        assert "LIST" in search["description"] and "dot" in search["description"]
        assert "NEGATIVE" in read["description"] and "allow_large" in read["description"]
        assert "-(L+8)" in read["description"] and "offset: -8" not in read["description"]
        assert "`hex`" in read["description"]
    finally:
        s.close()


def test_search_relays_the_route_with_the_fused_header(tmp_path, stub):
    s = _server(tmp_path, _env(tmp_path, stub))
    try:
        payload, is_error = _call(s, "s3_search", {"query": "era5"})
        assert not is_error and payload == HITS
        _m, path, q, headers = stub.requests[-1]
        assert path == "/api/remote-index/search"
        assert q == {"q": "era5", "limit": "20"} and headers.get("X-Fused") == "1"
        _call(s, "s3_search", {"path": "s3://fused-asset/data/", "cursor": "c1",
                               "limit": 999})
        assert stub.requests[-1][2] == {"path": "s3://fused-asset/data/",
                                        "cursor": "c1", "limit": "200"}
    finally:
        s.close()


def test_search_validates_and_maps_not_ready(tmp_path, stub):
    s = _server(tmp_path, _env(tmp_path, stub))
    try:
        for bad in ({}, {"query": "a", "path": "s3://fused-asset/"}, {"query": "a", "cursor": "c"}):
            payload, is_error = _call(s, "s3_search", bad)
            assert is_error, bad
        assert not stub.requests
        stub.search_status = 503
        payload, is_error = _call(s, "s3_search", {"query": "a"})
        assert is_error and payload["code"] == "index_not_ready"
        assert "call the tool again" in payload["error"]
    finally:
        s.close()


def test_a_small_text_range_comes_back_inline(tmp_path, stub):
    s = _server(tmp_path, _env(tmp_path, stub))
    try:
        payload, is_error = _call(s, "s3_read", {"url": URL, "length": 8})
        assert not is_error
        assert payload["content"] == "id,name\n" and payload["bytes"] == 8
        assert payload["range"] == {"start": 0, "end": 8} and "path" not in payload
        assert payload["size"] == len(CSV) and payload["etag"] == "abc"
        assert payload["modified"] == "2026-04-26T02:05:41Z"
    finally:
        s.close()


def test_binary_large_or_saved_ranges_go_to_the_scratch_folder(tmp_path, stub):
    env = _env(tmp_path, stub)
    s = _server(tmp_path, env)
    try:
        # The tail of a parquet file: binary, so auto-saved.
        payload, is_error = _call(s, "s3_read", {"url": "s3://fused-asset/data/b.parquet",
                                                 "offset": -100})
        assert not is_error and "content" not in payload
        path = payload["path"]
        assert os.path.dirname(path) == env["FUSED_RENDER_S3_SCRATCH"]
        assert open(path, "rb").read() == PARQUET[-100:]
        assert payload["range"] == {"start": len(PARQUET) - 100, "end": len(PARQUET)}
        assert path.endswith("-%d-%d-b.parquet" % (len(PARQUET) - 100, len(PARQUET)))
        assert "UTF-8" in payload["note"] and payload["hex"] == PARQUET[-100:].hex()
        # Valid UTF-8 but holding a NUL (a footer length, "M#\x02\x00PAR1"): binary.
        payload, _ = _call(s, "s3_read", {"url": "s3://fused-asset/data/b.parquet",
                                          "offset": -8})
        assert "content" not in payload and payload["hex"] == PARQUET[-8:].hex()
        # Text, but over the inline limit.
        payload, _ = _call(s, "s3_read", {"url": "s3://fused-asset/data/big.txt", "offset": 0})
        assert payload["bytes"] == 40000 and "inline limit" in payload["note"]
        assert "hex" not in payload
        assert os.path.getsize(payload["path"]) == 40000
        # Small text with save: a file anyway.
        payload, _ = _call(s, "s3_read", {"url": URL, "offset": 0, "save": True})
        assert open(payload["path"], "rb").read() == CSV and "as asked" in payload["note"]
        assert not [n for n in os.listdir(env["FUSED_RENDER_S3_SCRATCH"]) if n.endswith(".part")]
    finally:
        s.close()


def test_range_errors_are_tool_errors(tmp_path, stub):
    s = _server(tmp_path, _env(tmp_path, stub))
    try:
        payload, is_error = _call(s, "s3_read", {"url": URL, "offset": 10_000})
        assert is_error and payload["code"] == "range_not_satisfiable"
        assert "outside" in payload["error"]
        payload, is_error = _call(s, "s3_read", {"url": "s3://fused-asset/nope", "length": 1})
        assert is_error and payload["code"] == "not_found"
        for bad in ({}, {"url": URL, "length": 0}, {"url": URL, "offset": "x"}):
            assert _call(s, "s3_read", bad)[1], bad
    finally:
        s.close()


def test_a_cut_short_read_is_an_error_and_leaves_no_file(tmp_path, stub):
    env = _env(tmp_path, stub)
    stub.short = True
    s = _server(tmp_path, env)
    try:
        for args in ({"url": URL, "length": 8},  # would be inline
                     {"url": "s3://fused-asset/data/b.parquet", "offset": -5000}):
            payload, is_error = _call(s, "s3_read", args)
            assert is_error and "cut short" in payload["error"], args
        scratch = env["FUSED_RENDER_S3_SCRATCH"]
        assert not os.path.isdir(scratch) or not os.listdir(scratch)
    finally:
        s.close()


def test_parallel_identical_ranges_both_succeed(tmp_path, stub):
    s = _server(tmp_path, _env(tmp_path, stub))
    try:
        args = {"name": "s3_read", "arguments": {"url": "s3://fused-asset/data/big.txt",
                                                  "offset": 0}}
        pend = [s.send_async("tools/call", args) for _ in range(4)]
        for p in pend:
            res = p.result()["result"]
            payload = json.loads(res["content"][0]["text"])
            assert not res.get("isError") and os.path.getsize(payload["path"]) == 40000
    finally:
        s.close()


def test_a_cancelled_download_wait_ends_at_once(tmp_path, stub):
    stub.stat.update(size=10)
    stub.polls = -10_000  # never done
    s = _server(tmp_path, dict(_env(tmp_path, stub), FUSED_RENDER_S3_FETCH_WAIT="30"))
    try:
        import time
        pending = s.send_async("tools/call", {"name": "s3_read", "arguments": {"url": URL}})
        while not [r for r in stub.requests if r[1] == "/api/remote/fetch/status"]:
            time.sleep(0.05)
        t0 = time.monotonic()
        s.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                       "params": {"requestId": pending.id}}) + "\n")
        s.proc.stdin.flush()
        res = pending.result(timeout=5)["result"]
        assert res["isError"] and "cancelled" in res["content"][0]["text"]
        assert time.monotonic() - t0 < 3
    finally:
        s.close()


def test_whole_object_over_the_limit_is_refused_without_a_download(tmp_path, stub):
    s = _server(tmp_path, _env(tmp_path, stub))
    try:
        payload, is_error = _call(s, "s3_read", {"url": URL})
        assert is_error and "500.0 MiB" in payload["error"]
        assert "allow_large" in payload["error"] and "-65536" in payload["error"]
        assert "-8" not in payload["error"]
        assert not [r for r in stub.requests if r[0] == "POST"]
        payload, is_error = _call(s, "s3_read", {"url": URL, "allow_large": True})
        assert not is_error and payload["path"] == "/cache/x.bin"
        assert payload["range"] is None and payload["modified"] == "2023-11-14T22:13:20Z"
        post = [r for r in stub.requests if r[0] == "POST"][0]
        assert post[1] == "/api/remote/fetch" and post[2] == {"url": URL}
        assert post[3].get("X-Fused") == "1"
    finally:
        s.close()


def test_whole_object_already_cached_is_returned_without_a_fetch(tmp_path, stub):
    stub.stat.update(cached=True, path="/cache/a.csv")
    s = _server(tmp_path, _env(tmp_path, stub))
    try:
        payload, is_error = _call(s, "s3_read", {"url": URL})
        assert not is_error and payload["path"] == "/cache/a.csv"
        assert not [r for r in stub.requests if r[0] == "POST"]
        stub.stat.update(kind="dir", cached=False)
        payload, is_error = _call(s, "s3_read", {"url": URL})
        assert is_error and "s3_search" in payload["error"]
    finally:
        s.close()


def test_the_download_wait_is_bounded(tmp_path, stub):
    stub.stat.update(size=10)
    stub.polls = -10_000  # never done
    env = dict(_env(tmp_path, stub), FUSED_RENDER_S3_FETCH_WAIT="1")
    s = _server(tmp_path, env)
    try:
        payload, is_error = _call(s, "s3_read", {"url": URL})
        assert is_error and "still downloading" in payload["error"]
        assert "call s3_read again" in payload["error"]
    finally:
        s.close()


# -- agent.py wiring ----------------------------------------------------------

ORIGIN = "http://127.0.0.1:4321"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    mod = _load("agent")
    mod.RUNS = str(tmp_path / "runs")
    mod.SHOTS = str(tmp_path / "shots")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(home))
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", ORIGIN)
    return mod


def _canvases(on: bool):
    path = os.path.join(os.environ["FUSED_RENDER_HOME_DIR"], "prefs.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"canvases_enabled": on}, fh)


def _folder(tmp_path, name):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    (d / "a.pdf").write_bytes(b"%PDF-1.4\n")
    return d


def _claude(agent, monkeypatch, tmp_path, name):
    cmd, run_dir = _spawn(agent, monkeypatch, _folder(tmp_path, name))
    cfg = json.load(open(os.path.join(run_dir, "mcp.json"), encoding="utf-8"))
    env = cfg["mcpServers"][agent.PERMISSION_SERVER]["env"]
    allowed = cmd[cmd.index("--allowed-tools") + 1].split(",")
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    return cmd, run_dir, env, allowed, prompt


def test_claude_spawn_with_the_flag_on_differs_only_by_the_s3_pieces(
        agent, tmp_path, monkeypatch):
    _canvases(False)
    _cmd, _rd, env_off, allowed_off, prompt_off = _claude(agent, monkeypatch, tmp_path, "f")
    assert "FUSED_RENDER_S3_ORIGIN" not in env_off
    assert not [a for a in allowed_off if "s3_" in a or "cache/remote" in a]
    assert "s3_search" not in prompt_off
    assert not os.path.exists(agent._s3_cache_root())

    _canvases(True)
    _cmd, run_dir, env_on, allowed_on, prompt_on = _claude(agent, monkeypatch, tmp_path, "f")
    root = agent._s3_cache_root()
    assert env_on["FUSED_RENDER_S3_ORIGIN"] == ORIGIN
    assert env_on["FUSED_RENDER_S3_SCRATCH"] == os.path.join(
        root, ".ranges", os.path.basename(run_dir))
    assert {k: v for k, v in env_on.items() if not k.startswith("FUSED_RENDER_S3_")} == env_off
    tools = ["mcp__fused_approvals__s3_search", "mcp__fused_approvals__s3_read"]
    rules = [agent._read_rule(d) for d in agent._both_spellings([root])]
    assert rules and all("/cache/remote/**" in r for r in rules)
    added = [a for a in allowed_on if a not in allowed_off]
    assert added == tools + rules
    assert [a for a in allowed_on if a not in tools + rules] == allowed_off
    note = agent._s3_note("", "")
    assert note and note in prompt_on and prompt_on.replace(note, "") == prompt_off
    assert "fused_asset" not in note and "never curl" in note


def test_claude_spawn_without_an_origin_has_no_s3_tools(agent, tmp_path, monkeypatch):
    _canvases(True)
    monkeypatch.delenv("FUSED_RENDER_ORIGIN")
    _cmd, _rd, env, allowed, prompt = _claude(agent, monkeypatch, tmp_path, "f")
    assert "FUSED_RENDER_S3_ORIGIN" not in env
    assert not [a for a in allowed if "s3_" in a] and "s3_read" not in prompt


def test_the_note_names_the_skill_only_where_the_backend_has_it(agent, tmp_path, monkeypatch):
    _canvases(True)
    plugin = tmp_path / "workbench"
    (plugin / "skills" / "s3-files").mkdir(parents=True)
    (plugin / "skills" / "s3-files" / "SKILL.md").write_text("x")
    monkeypatch.setenv("FUSED_RENDER_WORKBENCH_PLUGIN_DIR", str(plugin))
    monkeypatch.setattr(agent, "_in_workbench_root", lambda f: True)
    assert "`workbench:s3-files` skill" in agent._s3_note("/x/a.py", "claude")
    assert "`s3-files` skill" in agent._s3_note("/x/a.py", "copilot")
    assert "skill" not in agent._s3_note("/x/a.py", "codex")
    monkeypatch.setattr(agent, "_in_workbench_root", lambda f: False)
    assert "skill" not in agent._s3_note("/x/a.py", "claude")


def _bins(agent, monkeypatch):
    for name in ("_copilot_bin", "_codex_bin", "_gemini_bin", "_opencode_bin"):
        monkeypatch.setattr(agent, name, lambda n=name: "/bin/" + n)


@pytest.mark.parametrize("on", [True, False])
def test_every_other_backend_gets_the_server_and_the_read_grant(
        agent, tmp_path, monkeypatch, on):
    _canvases(on)
    _bins(agent, monkeypatch)
    # No pane and an ordinary folder: only the S3/terminal clauses could
    # hand the server over, and the origin is there either way.
    target = str(_folder(tmp_path, "f"))
    root = agent._s3_cache_root()

    def run(backend):
        rd = tmp_path / "runs" / backend
        rd.mkdir(parents=True)
        argv = getattr(agent, "_%s_argv" % backend)(str(rd), False, None, "", "", "", [], target)
        return argv, str(rd)

    argv, rd = run("copilot")
    assert ("--add-dir" in argv and root in argv) is on
    cfg = json.load(open(os.path.join(rd, "mcp.json"), encoding="utf-8"))
    env = cfg["mcpServers"]["fused_approvals"]["env"]
    assert ("FUSED_RENDER_S3_ORIGIN" in env) is on
    agent_md = open(os.path.join(rd, "plugin", "agents", "fused.agent.md")).read()
    assert ("fused_approvals-s3_read" in agent_md) is on
    assert "mcp__fused_approvals__s3" not in agent_md

    argv, rd = run("gemini")
    assert (root in argv) is on
    body = open(os.path.join(rd, "gemini", "GEMINI.md")).read()
    assert ("mcp_fused_approvals_s3_search" in body) is on

    argv, rd = run("opencode")
    body = open(os.path.join(rd, "opencode", "AGENTS.md")).read()
    assert (root in body) is on and ("fused_approvals_s3_read" in body) is on

    facts = json.load(open(os.path.join(rd, "opencode", "spawn.json")))
    assert facts.get("mcp_timeout_ms") == (int(agent.S3_CALL_CEILING * 1000) if on else None)

    argv, rd = run("codex")
    env_arg = [a for a in argv if a.startswith("mcp_servers.fused_approvals.env=")]
    assert env_arg and ("FUSED_RENDER_S3_ORIGIN" in env_arg[0]) is on
    ceiling = [a for a in argv if "tool_timeout_sec" in a]
    assert ceiling == (["mcp_servers.fused_approvals.tool_timeout_sec=%.1f"
                        % agent.S3_CALL_CEILING] if on else [])


def test_breakout_may_call_the_s3_tools():
    ps = _load("permission_server")
    for tool in ("s3_search", "s3_read"):
        assert ps._breakout_policy("mcp__fused_approvals__" + tool, {}, "/h") is None


def test_hosts_know_the_tool_names():
    for host in ("gemini_host", "opencode_host"):
        mod = _load(host)
        assert {"s3_search", "s3_read"} <= mod._FUSED_TOOLS
