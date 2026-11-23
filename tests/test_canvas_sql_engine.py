"""The local SQL engine offered to a canvas node's framed page (SPEC §53.3
NW-7): the `sql_engine` field of
GET /api/canvases/status, and the contract the `canvas_widget` template's
relay keeps, pinned by text (the template runs in a browser)."""

import importlib.metadata
import json
import os
import re

import fused_render.canvases as canvases_mod
import pytest
from fastapi.testclient import TestClient
from fused_render.server import create_app
from fused_render.sql import runner

FUSED = {"X-Fused": "1"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(
    ROOT, "fused_render", "templates", "canvas_widget", "template.html"
)
NODE_JS = os.path.join(ROOT, "fused_render", "templates", "shared", "canvas-node.js")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CANVASES_DIR", str(tmp_path / "canvases"))
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    monkeypatch.delenv("FUSED_RENDER_WORKBENCH_ENV", raising=False)
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(tmp_path / "creds.json"))
    canvases_mod._cli_cache_clear()
    yield TestClient(create_app(start_dir=str(tmp_path)))
    canvases_mod._cli_cache_clear()


def _status(client):
    res = client.get("/api/canvases/status")
    assert res.status_code == 200
    return res.json()


# ------------------------------------------------------------- preference


def test_the_removed_preferences_are_unknown_keys(client, tmp_path):
    for key in ("canvases_sql_engine", "canvases_node_seed"):
        res = client.put("/api/prefs", json={key: True}, headers=FUSED)
        assert res.status_code == 400
        assert key not in res.json()["error"]
    # A key a prefs file still holds is ignored.
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / "prefs.json").write_text(
        '{"canvases_sql_engine": false, "canvases_node_seed": false}', encoding="utf-8"
    )
    assert client.get("/api/prefs").status_code == 200


# ----------------------------------------------------------------- status


def test_status_offers_the_engine_with_the_preference_on(client, monkeypatch):
    pytest.importorskip("duckdb")
    monkeypatch.setattr(runner, "available", lambda: True)
    assert _status(client)["sql_engine"] == {
        "name": "duckdb",
        "version": importlib.metadata.version("duckdb"),
    }


def test_status_offers_nothing_when_the_engine_is_not_installed(client, monkeypatch):
    monkeypatch.setattr(runner, "available", lambda: False)
    assert _status(client)["sql_engine"] is None


def test_status_never_imports_or_starts_the_engine(client, monkeypatch):
    monkeypatch.setattr(runner, "available", lambda: True)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")

    def boom(*a, **k):
        raise AssertionError("the status read started the engine")

    for name in ("open_session", "start"):
        if hasattr(runner, name):
            monkeypatch.setattr(runner, name, boom)
    assert _status(client)["sql_engine"] == {"name": "duckdb", "version": "9.9.9"}


# --------------------------------------------- node-js passes it through


def test_canvas_node_workbench_passes_the_offer_through():
    js = open(NODE_JS, encoding="utf-8").read()
    assert re.search(r"sqlEngine:\s*status\.sql_engine", js)


# ----------------------------------------------- template relay contract


@pytest.fixture(scope="module")
def html():
    return open(TEMPLATE, encoding="utf-8").read()


def _body_of(html, header):
    """The text of the brace-balanced block opened by `header`."""
    start = html.index(header)
    depth = 0
    for i in range(html.index("{", start), len(html)):
        depth += html[i] == "{"
        depth -= html[i] == "}"
        if depth == 0:
            return html[start : i + 1]
    raise AssertionError(header)


def test_the_capability_is_offered_only_for_an_engine_and_an_unproxied_frame(html):
    send = _body_of(html, "async function sendToken")
    assert "const offer = !!wb.sqlEngine && wb.origin === wb.publicOrigin;" in send
    assert "caps: []" in send
    assert 'payload.caps = ["sql", "udf", "udf-read"]' in send
    # caps and sqlEngine are only ever set inside the `if (offer)` branch.
    assert send.count('"sql"') == 1 and send.count("payload.sqlEngine") == 1
    branch = _body_of(send, "if (offer)")
    assert 'payload.caps = ["sql", "udf", "udf-read"]' in branch and "payload.sqlEngine" in branch


def test_sql_messages_are_ignored_unless_the_offer_was_made(html):
    handler = _body_of(html, 'window.addEventListener("message"')
    assert re.search(
        r'"fused-embed-sql"\)\s*\{\s*if \(sqlOffered\) void sqlMessage\(data\);',
        handler,
    )
    assert "sqlOffered = false" in _body_of(html, "function loadFrame")


def test_origin_and_source_checks_precede_the_sql_handler(html):
    handler = _body_of(html, 'window.addEventListener("message"')
    check = "event.origin !== wb.origin || event.source !== frame.contentWindow"
    assert check in handler
    assert handler.index(check) < handler.index('"fused-embed-sql"')
    # No second listener could reach the relay around those checks.
    assert html.count('addEventListener("message"') == 1


def test_replies_go_through_post_and_never_to_every_origin(html):
    assert '"*"' not in html.replace('Exact targetOrigin, never "*"', "")
    assert "win.postMessage(payload, wb.origin, transfer || [])" in html
    message = _body_of(html, "async function sqlMessage")
    assert 'post({ type: "fused-embed-sql-result"' in message
    assert (
        "postMessage"
        not in html.split("function post(")[1].split("async function sendToken")[1]
    )


def test_a_failing_handler_still_answers_the_frame(html):
    message = _body_of(html, "async function sqlMessage")
    assert "catch (error)" in message
    assert 'code: known ? error.code : "engine_error"' in message


def test_every_sql_fetch_carries_the_guard_header(html):
    relay = html[
        html.index("LOCAL SQL ENGINE RELAY") : html.index(
            'window.addEventListener("message"'
        )
    ]
    assert 'const SQL_GUARD = { "X-Fused": "1" };' in relay
    # The one place the relay calls the network for a route, plus pagehide,
    # plus the UDF read (a plain GET of a result: no guard header there).
    assert len(re.findall(r"\bfetch\(", relay)) == 3
    assert "headers: { ...SQL_GUARD, ...(init.headers || {}) }" in _body_of(
        relay, "async function sqlFetch"
    )
    assert re.search(
        r"keepalive: true,\s*headers: SQL_GUARD",
        _body_of(relay, "function sqlEnd"),
    )
    assert 'window.addEventListener("pagehide", sqlEnd);' in relay
    # A new document in the frame starts on a session of its own.
    assert "sqlEnd();" in _body_of(html, "function loadFrame")


def test_the_scope_is_the_nodes_environment_and_nothing_from_the_frame(html):
    session = _body_of(html, "function sqlSessionId")
    assert "JSON.stringify({ scope: node.env })" in session
    assert "data" not in session.split("JSON.stringify")[1].split(")")[0]


def test_the_session_is_created_once_and_forgotten_on_loss(html):
    session = _body_of(html, "function sqlSessionId")
    assert "if (!sqlSession)" in session and "sqlSession = made" in session
    call = _body_of(html, "async function sqlCall")
    assert 'error.status === 404 && error.message === "no such session"' in call
    assert "the SQL session ended" in call
    assert "SQL_KEEPALIVE_MS = 5 * 60 * 1000" in html
    assert "/keepalive" in html


def test_names_tables_and_path_parts_are_checked_before_a_call(html):
    assert r"const SQL_NAME_RE = /^[A-Za-z0-9._-]{1,128}$/;" in html
    assert r"const SQL_TABLE_RE = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;" in html
    op = _body_of(html, "async function sqlOp")
    assert re.search(
        r'value !== "\."\s*&&\s*value !== "\.\."', _body_of(html, "function sqlName")
    )
    # Every path part built from a message is encoded.
    paths = re.findall(r'"/(?:file|arrow)/"\s*\+\s*(\S+)', op)
    assert len(paths) == 4
    for raw in paths:
        assert raw.startswith("encodeURIComponent("), raw


def test_the_relay_logs_nothing(html):
    assert "console." not in html


def test_the_template_still_does_not_import_fused_render(html):
    code = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    assert not re.search(r"^\s*(import|from)\s+fused_render", code, re.MULTILINE)


# ------------------------------------------------ the UDF relay (NW-9)


def test_the_udf_relay_is_offered_exactly_with_sql(html):
    send = _body_of(html, "async function sendToken")
    assert send.count('"udf"') == 1 and send.count('"udf-read"') == 1
    assert 'payload.caps = ["sql", "udf", "udf-read"]' in _body_of(send, "if (offer)")
    assert "udfOffered = offer;" in send
    assert "udfOffered = false" in _body_of(html, "function loadFrame")


def test_udf_messages_are_ignored_unless_offered(html):
    handler = _body_of(html, 'window.addEventListener("message"')
    assert re.search(
        r'"fused-embed-udf"\)\s*\{\s*if \(udfOffered\) udfMessage\(data\);', handler
    )
    assert re.search(
        r'"fused-embed-udf-cancel"\)\s*\{\s*if \(udfOffered\) udfCancel\(data\);',
        handler,
    )
    # The same origin and source checks as every other message.
    assert handler.index("event.source !== frame.contentWindow") < handler.index(
        "fused-embed-udf"
    )


def test_the_udf_ref_is_this_nodes_own_and_checks_the_request(html):
    relay = _body_of(html, "function udfMessage")
    assert (
        "fused.workbench.udf({ canvas: node.name, name: m.name, env: node.env })" in relay
    )
    assert relay.count("fused.workbench.udf(") == 1
    # Nothing but the name, the params and the fresh flag is taken from the frame.
    assert re.findall(r"\bm\.(\w+)", relay) and set(re.findall(r"\bm\.(\w+)", relay)) <= {
        "req",
        "name",
        "params",
        "fresh",
        "format",
        "tile",
    }
    assert "SQL_NAME_RE.test(m.req)" in relay
    assert 'm.name.includes("/")' in relay and 'm.name.endsWith(".py")' in relay
    assert '"[object Object]"' in relay
    assert 'opts.cache = "fresh"' in relay and 'format: m.format || "parquet"' in relay


def test_no_confirmation_flag_is_forwarded_to_the_runtime(html):
    relay = _body_of(html, "function udfMessage")
    assert "confirm" not in relay.lower()
    assert "signal" not in relay


def test_the_result_token_is_validated_before_a_handle_is_sent(html):
    assert "const UDF_TOKEN_RE = /^(?:[0-9a-f]{64}|t[0-9a-f]{32})$/;" in html
    token = _body_of(html, "function udfToken")
    assert "UDF_TOKEN_RE.test" in token
    relay = _body_of(html, "function udfMessage")
    assert 'handle: "udf:" + token' in relay
    assert 'fail("failed", "the result could not be registered")' in relay


def test_udf_replies_go_through_post_guarded_by_frame_gen(html):
    relay = _body_of(html, "function udfMessage")
    assert "const gen = frameGen;" in relay
    assert re.search(
        r'if \(gen === frameGen\) post\(\{ type: "fused-embed-udf-result", req', relay
    )
    assert "postMessage" not in relay
    assert relay.count("fused-embed-udf-result") == 1


def test_the_early_saved_answer_is_not_final_and_one_final_ends_the_request(html):
    relay = _body_of(html, "function udfMessage")
    assert "answer(r, false)" in relay and "answer(r, true)" in relay
    assert "run.saved.then" in relay and "!finished" in relay
    assert relay.count("answer(r, true)") == 1
    assert 'cached: r.source !== "fresh"' in relay


def test_a_new_document_abandons_the_old_documents_runs(html):
    load = _body_of(html, "function loadFrame")
    assert load.index("frameGen++") < load.index("udfAbort();")
    assert 'window.addEventListener("pagehide", udfAbort);' in html
    assert "run.cancel()" in _body_of(html, "function udfAbort")
    assert "run.cancel()" in _body_of(html, "function udfCancel")


def test_udf_format_and_tile_are_validated_and_forwarded(html):
    relay = _body_of(html, "function udfMessage")
    assert 'const UDF_FORMATS = ["parquet", "png", "json", "html"];' in html
    assert "UDF_FORMATS.includes(m.format)" in relay
    assert "Number.isInteger(n) && n >= 0" in relay and "t.z > 30" in relay
    assert "tile = { x: t.x, y: t.y, z: t.z };" in relay
    assert "opts.tile = tile;" in relay and "udf.run(params, opts)" in relay
    assert "keep" not in relay


def test_udf_read_is_ignored_unless_offered_and_only_reads_issued_handles(html):
    handler = _body_of(html, 'window.addEventListener("message"')
    assert re.search(
        r'"fused-embed-udf-read"\)\s*\{\s*if \(udfOffered\) void udfRead\(data\);', handler
    )
    assert handler.index("event.source !== frame.contentWindow") < handler.index(
        "fused-embed-udf-read"
    )
    read = _body_of(html, "async function udfRead")
    assert "SQL_NAME_RE.test(m.req)" in read
    assert re.search(r'fail\("invalid_args"', read)
    assert "!udfIssued.has(token)" in read and 'fail("not_found"' in read
    assert read.index("udfIssued.has(token)") < read.index("fetch(")
    assert 'fetch("/api/udf/results/" + token' in read
    # The token is issued when a handle is sent, and forgotten with the document.
    assert "udfIssued.add(token);" in _body_of(html, "function udfMessage")
    load = _body_of(html, "function loadFrame")
    assert "udfIssued.clear();" in load and load.index("frameGen++") < load.index("udfIssued.clear()")


def test_udf_read_replies_go_through_post_with_a_transfer_list(html):
    read = _body_of(html, "async function udfRead")
    assert "const gen = frameGen;" in read
    assert re.search(
        r'if \(gen === frameGen\) post\(\{ type: "fused-embed-udf-read-result", req, \.\.\.extra \}, transfer\)',
        read,
    )
    assert "[bytes]" in read and "postMessage" not in read
