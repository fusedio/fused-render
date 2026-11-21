"""fused.workbench.udf()'s page half (runtime.js, SPEC §54.5), run under node.

The `fused-workbench:start` … `:end` slice of runtime.js is lifted out and run
against a fake WebSocket whose server side each scenario scripts (`__server`:
a function from the sent request to the frames to answer with). `udfAsk` (the
runtime's own dialog) is replaced by a scripted answer list, so a test sees
that a question was asked and what saying yes or no does."""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

PRELUDE = """
var IS_THUMBNAIL = false;
globalThis.window = { location: { protocol: "http:", host: "127.0.0.1:1" } };
globalThis.__sent = [];
globalThis.__asked = [];
globalThis.__answers = [];
class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 0);
  }
  send(text) {
    const msg = JSON.parse(text);
    globalThis.__sent.push(msg);
    const frames = globalThis.__server(msg) || [];
    let delay = 0;
    for (const f of frames) {
      delay += 1;
      setTimeout(() => this.onmessage({ data: JSON.stringify(Object.assign({ id: msg.id }, f)) }), delay);
    }
  }
  close() {}
}
globalThis.WebSocket = FakeSocket;
globalThis.fetch = async (url) => ({ ok: true, status: 200,
  text: async () => "body of " + url, json: async () => ({ url }),
  arrayBuffer: async () => new ArrayBuffer(3), blob: async () => null });
"""

AFTER = """
udfAsk = async (title, body, no, yes) => {
  globalThis.__asked.push(title);
  return globalThis.__answers.length ? globalThis.__answers.shift() : true;
};
function out(v) { console.log(JSON.stringify(v)); }
const R = (over) => Object.assign({ token: "t1", url: "/api/udf/results/t1", content_type: "text/html",
  size: 5, saved_at: 1, seconds: 0.5, stdout: "", stderr: "", fused_cached: true, source: "fused-cache" }, over || {});
"""


def _slice() -> str:
    import fused_render

    path = os.path.join(os.path.dirname(fused_render.__file__), "static", "runtime.js")
    src = open(path, encoding="utf-8").read()
    start = src.index("  // fused-workbench:start")
    return src[start:src.index("  // fused-workbench:end", start)]


def run_js(scenario: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to run the runtime's JS")
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False, encoding="utf-8") as f:
        f.write(PRELUDE + _slice() + AFTER + scenario)
        harness = f.name
    try:
        res = subprocess.run([node, harness], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(harness)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_saved_then_fresh():
    got = run_js("""
globalThis.__server = (m) => [{ type: "saved", result: R({ source: "saved", token: "k" }) },
                              { type: "started", kind: "none" }, { type: "done", result: R() }];
const run = workbench.udf({ file: "/a/b.py" }).run({ n: 1 }, { format: "json" });
const saved = await run.saved;
const fresh = await run;
out({ saved: saved.source, savedUrl: saved.url, fresh: fresh.source, text: await fresh.text(),
      sent: globalThis.__sent[0] });
""")
    assert got["saved"] == "saved" and got["savedUrl"] == "/api/udf/results/t1"
    assert got["fresh"] == "fused-cache" and got["text"] == "body of /api/udf/results/t1"
    sent = got["sent"]
    assert sent["op"] == "run" and sent["ref"] == {"file": "/a/b.py", "env": None}
    assert sent["params"] == {"n": 1} and sent["format"] == "json"
    assert sent["cache"] == "auto" and sent["confirmed"] is False


def test_no_saved_copy_resolves_saved_to_null():
    got = run_js("""
globalThis.__server = () => [{ type: "done", result: R() }];
const run = workbench.udf("canvas_1/hello").run();
out({ saved: await run.saved, ref: workbench.udf("canvas_1/hello", { env: "prod" }).ref, ok: !!(await run) });
""")
    assert got["saved"] is None and got["ok"] is True
    assert got["ref"] == {"canvas": "canvas_1", "name": "hello", "env": "prod"}


def test_a_batch_udf_is_asked_about_and_runs_on_yes():
    got = run_js("""
globalThis.__server = (m) => m.confirmed
  ? [{ type: "done", result: R() }]
  : [{ type: "error", code: "needs_confirmation", error: "x", title: "Confirm map operation",
       reason: "Auto-execution is disabled for map operations" }];
const r = await workbench.udf({ file: "/a.py" }).run();
out({ asked: globalThis.__asked, confirmed: globalThis.__sent.map((m) => m.confirmed), ok: r.source });
""")
    assert got["asked"] == ["Confirm map operation"]
    assert got["confirmed"] == [False, True] and got["ok"] == "fused-cache"


def test_no_to_a_batch_udf_is_declined_and_confirm_false_never_asks():
    got = run_js("""
globalThis.__server = () => [{ type: "error", code: "needs_confirmation", error: "x",
  title: "Confirm batch job run", reason: "Auto-execution is disabled for batch job UDFs" }];
globalThis.__answers = [false];
let a, b;
try { await workbench.udf({ file: "/a.py" }).run(); } catch (e) { a = [e.code, e.reason]; }
try { await workbench.udf({ file: "/a.py" }).run(undefined, { confirm: false }); } catch (e) { b = e.code; }
out({ a, b, asked: globalThis.__asked.length, sent: globalThis.__sent.length });
""")
    assert got["a"] == ["declined", "Auto-execution is disabled for batch job UDFs"]
    assert got["b"] == "declined" and got["asked"] == 1 and got["sent"] == 2


def test_a_large_result_is_asked_about():
    got = run_js("""
globalThis.__server = () => [{ type: "done", result: R({ size: 60 * 1024 * 1024 }) }];
globalThis.__answers = [false, true];
let first;
try { await workbench.udf({ file: "/a.py" }).run(); } catch (e) { first = e.code; }
const second = await workbench.udf({ file: "/a.py" }).run();
out({ first, second: second.size, asked: globalThis.__asked });
""")
    assert got["first"] == "declined" and got["second"] == 60 * 1024 * 1024
    assert got["asked"] == ["Large data warning", "Large data warning"]


def test_errors_carry_their_code_and_details_and_logs_are_heard():
    got = run_js("""
globalThis.__server = () => [{ type: "error", code: "udf_error", error: "boom", line: 4,
                               stdout: "printed", stderr: "" }];
const logs = [];
let err;
try { await workbench.udf({ file: "/a.py" }).run().onLog((l) => logs.push(l.stdout)); }
catch (e) { err = [e.code, e.message, e.line, e.stdout]; }
out({ err, logs });
""")
    assert got["err"] == ["udf_error", "boom", 4, "printed"] and got["logs"] == ["printed"]


def test_cancel_rejects_and_tells_the_server():
    got = run_js("""
globalThis.__server = () => [];
const run = workbench.udf({ file: "/a.py" }).run();
await new Promise((r) => setTimeout(r, 5));
run.cancel();
let code;
try { await run; } catch (e) { code = e.code; }
const saved = await run.saved;
out({ code, saved, ops: globalThis.__sent.map((m) => m.op) });
""")
    assert got["code"] == "cancelled" and got["saved"] is None
    assert got["ops"] == ["run", "cancel"]


def test_a_preview_thumbnail_runs_nothing():
    got = run_js("""
IS_THUMBNAIL = true;
globalThis.__server = () => [{ type: "done", result: R() }];
let code;
try { await workbench.udf({ file: "/a.py" }).run(); } catch (e) { code = e.code; }
out({ code, sent: globalThis.__sent.length });
""")
    assert got == {"code": "preview", "sent": 0}


def test_schema_and_bad_references():
    got = run_js("""
globalThis.__server = (m) => [{ type: "schema", schema: { parameters: [m.ref.name] } }];
const schema = await workbench.udf("c/n").schema();
const bad = [];
for (const ref of ["noslash", "/x", "x/", 5, {}]) {
  try { workbench.udf(ref); } catch (e) { bad.push(e.name); }
}
out({ schema, bad });
""")
    assert got["schema"] == {"parameters": ["n"]}
    assert got["bad"] == ["TypeError"] * 5


def test_a_run_cancelled_before_the_socket_opens_is_never_sent():
    got = run_js("""
globalThis.__server = () => [{ type: "done", result: R() }];
const run = workbench.udf({ file: "/a.py" }).run();
run.cancel();
let code;
try { await run; } catch (e) { code = e.code; }
await new Promise((r) => setTimeout(r, 10));
out({ code, sent: globalThis.__sent.length });
""")
    assert got == {"code": "cancelled", "sent": 0}


def test_nothing_is_asked_about_after_a_cancel():
    got = run_js("""
globalThis.__server = (m) => m.op === "run" ? [{ type: "started" }] : [];
const run = workbench.udf({ file: "/a.py" }).run();
await new Promise((r) => setTimeout(r, 5));
run.cancel();
// The server's answer lands after the cancel: a large result.
const sock = udfSock;
const id = globalThis.__sent[0].id;
sock.ws.onmessage({ data: JSON.stringify({ id, type: "done", result: R({ size: 60 * 1024 * 1024 }) }) });
await new Promise((r) => setTimeout(r, 5));
let code;
try { await run; } catch (e) { code = e.code; }
out({ code, asked: globalThis.__asked.length });
""")
    assert got == {"code": "cancelled", "asked": 0}

