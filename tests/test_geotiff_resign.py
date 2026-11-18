"""The geotiff page's re-signing of an expiring streamed URL (D1488).

`signFor` / `scheduleResign` in templates/geotiff/template.html, run under
node with stubbed signing and hand-driven timers: a URL is re-signed before
it lapses, a failed re-sign retries, and an answer for a file the viewer has
already left never touches the open file's URL or its refresh timer.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import fused_render

TEMPLATE = (Path(fused_render.__file__).parent / "templates" / "geotiff"
            / "template.html").read_text(encoding="utf-8")
BLOCK = "let signed = null;" + TEMPLATE.split("let signed = null;", 1)[1].split(
    "// ---------------- daemon + state", 1)[0]

_PRELUDE = r"""
let CUR = "s3://b/a.tif";
const P = { file: () => CUR };
let tileMode = true, loadedQuery = null, SETS = 0;
function renderQuery() { return "q"; }
function setRasterTiles() { SETS++; }
const isRemote = f => /^s3:\/\//i.test(f || "");
// hand-driven timers
const TIMERS = new Map(); let NEXT = 1;
function setTimeout(fn, ms) { const id = NEXT++; TIMERS.set(id, {fn, ms}); return id; }
function clearTimeout(id) { TIMERS.delete(id); }
async function fire(id) { const t = TIMERS.get(id); TIMERS.delete(id); await t.fn(); }
const last = () => Math.max(...TIMERS.keys());
// signing: each call waits on a deferred the test settles
const PENDING = [];
const fused = { s3: { sign: (addr) => new Promise((ok, no) => PENDING.push({addr, ok, no})) } };
const soon = () => new Date(Date.now() + 120000).toISOString();
async function signNow(addr) {
  const p = signFor(addr);
  PENDING.pop().ok({ url: "https://x/" + addr.slice(7) + "?v" + NEXT, expires_at: soon() });
  return p;
}
const tick = () => new Promise(r => setImmediate(r));
// time passes: the URL is now inside its last minute
const age = () => { signed.expires_at = new Date(Date.now() + 30000).toISOString(); };
function out(v) { console.log(JSON.stringify(v)); }
"""


def _node(body: str):
    if not shutil.which("node"):
        pytest.skip("node is needed to run the template's signing code")
    script = _PRELUDE + BLOCK + "\n(async () => {\n" + body + "\n})();"
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_an_expiring_url_is_re_signed_and_the_tiles_swap():
    r = _node("""
      await signNow("s3://b/a.tif");
      const first = signed.url, timer = last();
      age();
      const run = fire(timer); await tick();
      PENDING.shift().ok({url: "https://x/b/a.tif?new", expires_at: soon()});
      await run;
      out({first, now: signed.url, sets: SETS, rearmed: TIMERS.size});
    """)
    assert r["now"] == "https://x/b/a.tif?new" != r["first"]
    assert r["sets"] == 1 and r["rearmed"] == 1


def test_a_failed_re_sign_retries_while_the_file_is_open():
    r = _node("""
      await signNow("s3://b/a.tif");
      age();
      const run = fire(last()); await tick();
      PENDING.shift().no(new Error("server unreachable"));
      await run;
      const t = TIMERS.get(last());
      out({timers: TIMERS.size, ms: t && t.ms});
    """)
    assert r == {"timers": 1, "ms": 15000}


@pytest.mark.parametrize("outcome", ["ok", "no"])
def test_a_late_answer_for_a_left_file_leaves_the_open_file_alone(outcome):
    r = _node(f"""
      await signNow("s3://b/a.tif");
      age();
      const runA = fire(last()); await tick();          // A's re-sign in flight
      CUR = "s3://b/b.tif";
      await signNow("s3://b/b.tif");                    // B opens and is signed
      const urlB = signed.url, timerB = last();
      const late = PENDING.shift();
      {"late.ok({url: 'https://x/b/a.tif?late', expires_at: soon()});" if outcome == "ok"
       else "late.no(new Error('boom'));"}
      await runA;
      out({{address: signed.address, sameUrl: signed.url === urlB,
            timerB: TIMERS.has(timerB), timers: TIMERS.size, sets: SETS}});
    """)
    assert r == {"address": "s3://b/b.tif", "sameUrl": True, "timerB": True,
                 "timers": 1, "sets": 0}
