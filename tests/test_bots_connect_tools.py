"""The bots bridge to connected services (SPEC-connect-oauth §11 "Bots bridge").

`fused_render.connect` is replaced by a stub package in `sys.modules` (a
`registry` with `providers()`/`get()`, a `tools` with `list_tools`/`call_tool`
and a `ToolSpec` with the contract's fields), so these tests pin the bridge to
the contract whether or not the real package is present in this checkout.
"""
from _bots_conftest import *  # noqa: F401,F403 — app_home and friends
import json
import os
import sys
import textwrap
import types
from dataclasses import dataclass

import pytest

from fused_render.bots import apptools
from fused_render.bots import bot as botmod
from fused_render.bots import tools as bot_tools

os.environ.setdefault("OPENFUSED_APP_SERVE_PYTHON", sys.executable)


@dataclass
class ToolSpec:
    provider: str
    name: str
    description: str
    input_schema: dict
    read_only: bool
    destructive: bool


@dataclass
class Provider:
    key: str
    name: str
    dir: str = ""
    auth: list = None
    tools: dict = None
    privacy: str = ""
    icon: str = None


SEARCH = ToolSpec("notion", "search", "Search the workspace.",
                  {"type": "object", "properties": {"limit": {"type": "integer", "default": 10},
                                                    "query": {"type": "string"},
                                                    "filter": {"type": ["object", "null"]}},
                   "required": ["query"]}, read_only=True, destructive=False)
CREATE = ToolSpec("notion", "notion-create-pages", "Make pages.",
                  {"type": "object", "properties": {"title": {"type": "string"}, "tags": {"type": "array"},
                                                    "draft": {"type": "boolean"}, "w": {"type": "number"},
                                                    "x": {}}, "required": ["title"]},
                  read_only=False, destructive=False)


class FakeConnect:
    """The stub connect package's state: what list_tools serves and what call_tool saw."""

    def __init__(self):
        self.providers = [Provider("github", "GitHub"), Provider("notion", "Notion")]
        self.tools = {"notion": [SEARCH, CREATE]}  # github: not connected → []
        self.calls = []
        self.reply = {"ok": True, "text": "found 2 pages", "content": [], "is_error": False}
        self.list_raises = set()

    def list_tools(self, key):
        if key in self.list_raises:
            raise RuntimeError("tools/list failed")
        return list(self.tools.get(key, []))

    def call_tool(self, key, name, args):
        self.calls.append((key, name, dict(args)))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def connect(monkeypatch):
    fc = FakeConnect()
    pkg = types.ModuleType("fused_render.connect")
    pkg.__path__ = []
    reg = types.ModuleType("fused_render.connect.registry")
    reg.providers = lambda: list(fc.providers)
    reg.get = lambda key: next((p for p in fc.providers if p.key == key), None)
    tl = types.ModuleType("fused_render.connect.tools")
    tl.ToolSpec = ToolSpec
    tl.list_tools = fc.list_tools
    tl.call_tool = fc.call_tool
    pkg.registry, pkg.tools = reg, tl
    monkeypatch.setitem(sys.modules, "fused_render.connect", pkg)
    monkeypatch.setitem(sys.modules, "fused_render.connect.registry", reg)
    monkeypatch.setitem(sys.modules, "fused_render.connect.tools", tl)
    return fc


@pytest.fixture
def no_local(tmp_path, monkeypatch):
    """An empty apps root: the registry holds connected tools only."""
    monkeypatch.setattr(apptools, "ROOTS", [str(tmp_path / "apps")])
    monkeypatch.setattr(apptools, "REGISTRY_FILES", [])
    monkeypatch.setattr(apptools, "_cache_at", 0.0)


def _make_local_app(root, folder):
    d = os.path.join(root, folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w") as f:
        f.write('<!doctype html><html><head><meta name="fused-app" /><title>Fake</title></head><body></body></html>')
    with open(os.path.join(d, "t.py"), "w") as f:
        f.write("def main(x: int = 1):\n    return {'x': x}\n")
    with open(os.path.join(d, "mcp.toml"), "w") as f:
        f.write(textwrap.dedent('''
            [[tool]]
            name = "fake_echo"
            description = "Echoes x back. Read-only."
            file = "t.py"
        '''))
    return d


# ---- registry ----------------------------------------------------------------------
def test_registry_holds_connected_tools_alone(connect, no_local):
    recs = apptools.registry(force=True)
    assert [(r.app, r.app_dir, r.name, r.tools_in_app) for r in recs] == [
        ("notion", "connect:notion", "search", 2), ("notion", "connect:notion", "notion-create-pages", 2)]
    assert recs[0].spec is SEARCH and recs[0].description == "Search the workspace."
    assert all(apptools.is_connect(r) for r in recs)


def test_params_from_input_schema(connect, no_local):
    search, create = apptools.registry(force=True)
    assert [(p.name, p.annotation, p.has_default, p.default) for p in search.params] == [
        ("query", str, False, None), ("limit", int, True, 10), ("filter", dict, True, None)]
    assert [(p.name, p.annotation) for p in create.params] == [
        ("title", str), ("tags", list), ("draft", bool), ("w", float), ("x", None)]
    assert apptools._sig(search) == "query: str, limit: int=10, filter: dict=null"


def test_registry_lists_local_and_connected(connect, tmp_path, monkeypatch):
    root = str(tmp_path / "apps")
    _make_local_app(root, "fake-docs")
    monkeypatch.setattr(apptools, "ROOTS", [root])
    monkeypatch.setattr(apptools, "REGISTRY_FILES", [])
    recs = apptools.registry(force=True)
    assert [(r.app, r.name) for r in recs] == [
        ("fake-docs", "fake_echo"), ("notion", "search"), ("notion", "notion-create-pages")]
    assert not apptools.is_connect(recs[0])


def test_connected_tools_are_not_held_by_the_local_cache(connect, no_local):
    assert len(apptools.registry(force=True)) == 2
    connect.tools["notion"] = [SEARCH]  # disconnected one tool: within the local TTL, still seen at once
    assert [r.name for r in apptools.registry()] == ["search"]


def test_connected_tools_ignore_local_availability(connect, no_local, monkeypatch):
    monkeypatch.setattr(apptools, "_HAVE", False)
    monkeypatch.setattr(apptools, "_NATIVE", False)
    assert apptools.available() is False
    assert [r.name for r in apptools.registry(force=True)] == ["search", "notion-create-pages"]


def test_find_resolves_a_connected_tool(connect, no_local):
    recs = apptools.registry(force=True)
    assert apptools.find(recs, "notion", "search") is recs[0]
    assert apptools.find(recs, "Notion", "notion-create-pages") is recs[1]
    assert apptools.find(recs, "github", "search") is None


def test_connect_import_error_means_no_records(no_local, monkeypatch):
    monkeypatch.setitem(sys.modules, "fused_render.connect", None)  # import → ImportError
    assert apptools.connect_records() == []
    assert apptools.registry(force=True) == []
    assert apptools.connect_title("notion") == "notion"


def test_connect_failures_are_swallowed(connect, no_local, monkeypatch):
    connect.list_raises.add("notion")
    assert apptools.registry(force=True) == []
    connect.list_raises.clear()

    def boom():
        raise RuntimeError("bad pack")
    monkeypatch.setattr(sys.modules["fused_render.connect.registry"], "providers", boom)
    assert apptools.registry(force=True) == []


# ---- run_tool -----------------------------------------------------------------------
def test_run_tool_dispatches_with_filtered_args(connect, no_local):
    rec = apptools.find(apptools.registry(force=True), "notion", "search")
    res = apptools.run_tool(rec, {"query": "roadmap", "bogus": 1})
    assert connect.calls == [("notion", "search", {"query": "roadmap"})]
    assert (res.ok, res.text, res.dropped) == (True, "found 2 pages", ["bogus"])


def test_run_tool_maps_is_error_and_not_ok(connect, no_local):
    rec = apptools.find(apptools.registry(force=True), "notion", "search")
    connect.reply = {"ok": True, "text": "invalid filter", "content": [], "is_error": True}
    res = apptools.run_tool(rec, {"query": "x"})
    assert res.ok is False and res.text == "error: invalid filter"
    connect.reply = {"ok": False, "text": "error: not connected", "content": [], "is_error": False}
    res = apptools.run_tool(rec, {"query": "x"})
    assert res.ok is False and res.text == "error: not connected"
    connect.reply = RuntimeError("network down")
    res = apptools.run_tool(rec, {"query": "x"})
    assert res.ok is False and res.text == "error: network down"
    connect.reply = "not a dict"
    assert apptools.run_tool(rec, {"query": "x"}).ok is False


def test_run_tool_args_not_object(connect, no_local):
    rec = apptools.find(apptools.registry(force=True), "notion", "search")
    assert apptools.run_tool(rec, ["x"]).text.startswith("error: `args` must be a JSON object")
    assert connect.calls == []


# ---- approval -----------------------------------------------------------------------
def test_is_write_follows_read_only(connect, no_local):
    search, create = apptools.registry(force=True)
    assert apptools.is_write(search) is False
    assert apptools.is_write(create) is True
    # the annotation wins over the name heuristic both ways
    sneaky = apptools.ToolRec("notion", "connect:notion", "fetch", "Fetches.", spec=CREATE)
    assert apptools.is_write(sneaky) is True
    reads = apptools.ToolRec("notion", "connect:notion", "delete_preview", "Deletes nothing.", spec=SEARCH)
    assert apptools.is_write(reads) is False


class _GateBot:
    def __init__(self, trusted=(), connections=(), approval="ask"):
        self.meta = {"trusted_apps": list(trusted), "connections": list(connections), "approval": approval}


WRITE = {"app": "notion", "name": "notion-create-pages", "args": {"title": "x"}}
READ = {"app": "notion", "name": "search", "args": {"query": "x"}}


def test_gate_asks_on_every_call_until_the_service_is_enabled_for_the_bot(connect, no_local):
    """bot.json `connections` (docs §2): a service not ticked asks on every call, read-only or not; ticked, it never
    asks, risky or not. `trusted_apps` never opens a service: a local app folder may share its name."""
    apptools.registry(force=True)
    for d in (WRITE, READ, dict(READ, risky=True)):
        why = bot_tools.risk(_GateBot(), "tool", d, {})
        assert "It uses Notion (" in why and "no access to yet" in why and "Always approve" in why
    assert "Notion (search)" in bot_tools.risk(_GateBot(trusted=["notion"]), "tool", READ, {})
    for d in (WRITE, READ, dict(READ, risky=True)):
        assert bot_tools.risk(_GateBot(connections=["Notion"]), "tool", d, {}) == ""
    assert bot_tools.describe(_GateBot(), "tool", WRITE, {}).startswith("call notion › notion-create-pages with ")


def test_access_card_shows_under_never_ask_too(connect, no_local):
    """must_ask: the gate the engines read before `approval` — access to a signed-in service is the user's yes."""
    apptools.registry(force=True)
    assert bot_tools.must_ask(_GateBot(approval="auto"), "tool", READ)
    assert not bot_tools.must_ask(_GateBot(approval="auto", connections=["notion"]), "tool", READ)
    assert not bot_tools.must_ask(_GateBot(), "tool", {"app": "nope", "name": "x"})
    assert bot_tools.must_ask(_GateBot(), "bot_create", {}) and bot_tools.must_ask(_GateBot(), "terminal_run", {})
    assert not bot_tools.must_ask(_GateBot(), "click", {})


def test_always_approve_ticks_the_service_not_trusted_apps(connect, no_local):
    apptools.registry(force=True)
    rule = bot_tools.forever_rule(_GateBot(), "tool", WRITE, {})
    assert rule == {"kind": "connect", "key": "notion", "label": "uses Notion"}
    assert not bot_tools.forever_match(_GateBot(), rule)
    assert bot_tools.forever_match(_GateBot(connections=["notion"]), rule)
    assert not bot_tools.forever_match(_GateBot(trusted=["notion"]), rule)
    meta = {}
    assert apptools.allow_connection(meta, "Notion") and meta["connections"] == ["notion"]
    assert not apptools.allow_connection(meta, "notion")
    apptools.set_connections(meta, add=["github"], remove=["notion"])
    assert meta["connections"] == ["github"]
    assert bot_tools.clean_allow_rules([rule]) == []  # never a rule row: the switch is the record


# ---- prompt -------------------------------------------------------------------------
def test_prompt_section_heading_and_approval(connect, no_local):
    recs = apptools.registry(force=True)
    sec = apptools.prompt_section(recs)
    assert "and connected services (Notion, GitHub, Slack…) whose tools run on the service's own servers" in sec
    assert "- search(query: str, limit: int=10, filter: dict=null): Search the workspace. [approval]\n" in sec
    assert "- notion-create-pages(title: str, tags: list=null, draft: bool=null, w: float=null, x=null): Make pages. [approval]" in sec
    assert "\nnotion (connected service) (not enabled for this bot: every call asks first)\n" in sec
    assert "Search the workspace. [approval]\n" in sec  # not enabled: the read-only tool asks too
    trusted = apptools.prompt_section(recs, apptools.trusted_set({"trusted_apps": ["notion"]}))
    assert "every call asks first" in trusted  # trusted_apps never opens a service
    on = apptools.prompt_section(recs, connections=apptools.connections_set({"connections": ["Notion"]}))
    assert "notion (connected service) (enabled for you: its tools never ask)" in on
    assert "[approval]" not in on.split("\nnotion", 1)[1] and on.endswith("Make pages.")


def test_prompt_header_unchanged_without_services(connect, tmp_path, monkeypatch):
    connect.tools.clear()
    root = str(tmp_path / "apps")
    _make_local_app(root, "fake-docs")
    monkeypatch.setattr(apptools, "ROOTS", [root])
    monkeypatch.setattr(apptools, "REGISTRY_FILES", [])
    sec = apptools.prompt_section(apptools.registry(force=True))
    assert "APP TOOLS (local apps on this Mac; call one" in sec
    assert "connected service" not in sec


def test_local_app_and_service_of_one_name_get_two_headings():
    local = apptools.ToolRec("notion", "/x/notion", "fake_echo", "Echoes.")
    svc = apptools.ToolRec("notion", "connect:notion", "search", "Searches.", spec=SEARCH)
    body = apptools._render([local, svc], None)
    assert body.splitlines()[0] == "notion" and any(l.startswith("notion (connected service)") for l in body.splitlines())


# ---- bot.run_tool / _deliver --------------------------------------------------------
class _StubBot:
    """The slice of bots.bot.Bot that run_tool / _deliver touch."""
    _tool_calls = 0
    run_tool = botmod.Bot.run_tool
    _deliver = botmod.Bot._deliver

    def __init__(self, tmp):
        self.events = []
        self.tmp = tmp

    def emit(self, kind, text, **kw):
        self.events.append((kind, text, kw))

    def save_file(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p


def test_bot_run_tool_note_without_app_card(connect, no_local, tmp_path):
    b = _StubBot(str(tmp_path))
    label, text = b.run_tool("notion", "search", {"query": "roadmap", "bogus": 1})
    assert label == "tool notion › search"
    assert text == ("RESULT:\nfound 2 pages\n(ignored unknown args: bogus; the tool takes only the parameters listed)")
    (kind, line, kw), = b.events
    assert kind == "note" and line == "Used Notion › search"
    assert "app" not in kw and json.loads(kw["detail"]) == {"query": "roadmap", "bogus": 1}
    b.run_tool("notion", "search", {"query": "again"})  # a second call: still no card
    assert all("app" not in e[2] for e in b.events)


def test_bot_run_tool_title_falls_back_to_key(connect, no_local, tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules["fused_render.connect.registry"], "get", lambda key: None)
    b = _StubBot(str(tmp_path))
    b.run_tool("notion", "search", {"query": "x"})
    assert b.events[0][1] == "Used notion › search"


def test_bot_run_tool_error_and_cap(connect, no_local, tmp_path):
    b = _StubBot(str(tmp_path))
    connect.reply = {"ok": False, "text": "error: reconnect Notion", "content": [], "is_error": True}
    assert b.run_tool("notion", "search", {"query": "x"})[1] == "error: reconnect Notion"
    connect.reply = {"ok": True, "text": "y" * (apptools.RESULT_CAP + 50), "content": [], "is_error": False}
    _, text = b.run_tool("notion", "search", {"query": "x"})
    assert text.startswith("RESULT:\n" + "y" * apptools.RESULT_CAP + "\n(truncated; full result saved to Inbox as ")
    assert any(n.startswith("tool-notion-search-") for n in os.listdir(tmp_path))


def test_bot_run_tool_needs_no_bundled_module_for_services(connect, no_local, tmp_path, monkeypatch):
    monkeypatch.setattr(apptools, "_HAVE", False)
    monkeypatch.setattr(apptools, "_NATIVE", False)
    b = _StubBot(str(tmp_path))
    assert b.run_tool("notion", "search", {"query": "x"})[1] == "RESULT:\nfound 2 pages"
    _, text = b.run_tool("fake-docs", "fake_echo", {})
    assert text.startswith("error: app tools are not available in this copy of the app")


def test_bot_run_tool_unknown_lists_services(connect, no_local, tmp_path):
    _, text = _StubBot(str(tmp_path)).run_tool("notion", "nope", {})
    assert text.startswith("error: no such tool. Apps with tools: notion.")
