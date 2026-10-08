"""T8: orchestrator dies, Chrome lives. argv[1] = child1 (attach, type, then hang until SIGKILLed) | child2 (re-attach, verify, cleanup)"""
import sys, os, time, json, signal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import pages, http
from util import *

sf = json.load(open(f"{OUT}/session.json"))
fh = open(f"{OUT}/t8.log", "a")
port, tid, wid, pid = sf["port"], sf["bot_tid"], sf["wid"], sf["pid"]
log(fh, sys.argv[1], "attach port", port, "pid alive:", os.system(f"kill -0 {pid}") == 0)
t0 = time.time()
ids = [p["id"] for p in pages(port)]
ws = page_ws(port, tid); ws.call("Page.enable")
fe_before = ws.evaluate("document.visibilityState")
ws.call("Emulation.setFocusEmulationEnabled", enabled=True)  # per-session: must be re-applied on every reconnect
log(fh, sys.argv[1], "found tab:", tid in ids, "reattach ms", round((time.time() - t0) * 1000), "vis before re-enabling focus emu:", fe_before)
ws.evaluate("var t=document.getElementById('t');t.focus();t.setSelectionRange(t.value.length,t.value.length);1")
if sys.argv[1] == "child1":
    type_text(ws, "-t8a"); log(fh, "child1 value", ws.evaluate("document.getElementById('t').value"))
    open(f"{OUT}/t8_child1.ready", "w").write("1")
    time.sleep(120)
else:
    type_text(ws, "-t8b"); v = ws.evaluate("document.getElementById('t').value")
    bws = browser_ws(port)
    shot_ok = shot(ws, save="t8_after_reattach")
    log(fh, "child2 value", v, "pid alive", os.system(f"kill -0 {pid}") == 0, "same window", win(bws, tid)["windowId"] == wid, "shot", json.dumps(shot_ok),
        "PASS", v.endswith("-t8a-t8b"))
