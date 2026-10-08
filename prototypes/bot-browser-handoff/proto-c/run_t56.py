"""T5 + T6 with the layout T7 says works: all bots = tabs in one bots window, focus emulation on every bot tab.
Leaves Chrome running + session.json for t8.py."""
import hashlib, json, os, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from relay import Relay
kill_c(); time.sleep(1)
R = {}; LOG = os.path.join(OUT, "run_t56.json")
def rec(k, v): R[k] = v; print(k, json.dumps(v, default=str)[:900], flush=True); json.dump(R, open(LOG, "w"), indent=1, default=str)
relay = Relay(); BASE = serve_page()
sess, ext_id, ok, dt = launch_bridge("C-56", relay=relay); CPID = sess["pid"]; PORT = sess["port"]
def target_of(tab): return next((t["id"] for t in relay.call("targets") if t.get("tabId") == tab), None)
def val(tab): return relay.evaluate(tab, "document.getElementById('t').value")
def ready(tab, h):
    relay.cdp(tab, "Page.navigate", url=BASE + f"?h={h}"); time.sleep(0.6); relay.evaluate(tab, "document.getElementById('t').focus()")
def mkbot(win=None, h="bot"):
    if win is None:
        w = relay.call("createWindow", url="about:blank", focused=False, width=800, height=600, left=60, top=60); t, win = w["tabId"], w["windowId"]
    else:
        t = relay.call("createTab", windowId=win, url="about:blank", active=False)["tabId"]
    relay.call("attach", tabId=t); relay.cdp(t, "Page.enable"); relay.cdp(t, "Emulation.setFocusEmulationEnabled", enabled=True); ready(t, h)
    return t, win

# ---------- T5 ----------
try:
    A, W = mkbot(h="A"); B, _ = mkbot(W, h="B"); TA = target_of(A)
    relay.call("detach", tabId=A); relay.call("activate", tabId=A)  # human owns A; B is now a background tab
    shots, vals = [], []
    for i in range(5):
        ready(B, f"B{i}")
        s = relay.cdp(B, "Page.captureScreenshot", format="png")["data"]; shots.append((hashlib.md5(s.encode()).hexdigest()[:8], png_stats(s)))
        type_text(relay, B, f"b{i}.'"); vals.append(val(B))
        if i == 2: hA = human_input(CPID, PORT, TA, "human-in-A")  # human typing in A concurrently-ish
    relay.call("attach", tabId=A); relay.cdp(A, "Emulation.setFocusEmulationEnabled", enabled=True); vA = val(A)
    vis_B = relay.evaluate(B, "document.visibilityState")
    ok5 = all(s[1][2] > 2 for s in shots) and len({s[0] for s in shots}) == 5 and vals == [f"b{i}.'" for i in range(5)] and vA == "human-in-A"
    rec("T5", {"pass": ok5, "B_shots(md5,(w,h,uniq))": shots, "B_vals": vals, "B_visibility": vis_B, "A_val": vA, "human": hA, "A_target_same": target_of(A) == TA})
except Exception:
    rec("T5", {"pass": False, "err": traceback.format_exc()})

# ---------- T6 ----------
def user_close(tab):
    fp = frontmost_pid()
    o = osa('tell application "System Events" to keystroke "w" using command down') if str(fp) == str(CPID) else ("", f"frontmost={fp}")
    if not o[1]: return {"path": "osascript cmd-W", "osa": o}
    tid = target_of(tab)
    bws = cdp.WS(cdp.http(PORT, "/json/version")["webSocketDebuggerUrl"]); r = bws.call("Target.closeTarget", targetId=tid); bws.close()
    return {"path": "cdp Target.closeTarget fallback (osascript TCC-denied)", "osascript_err": o[1], "r": r}
try:
    out = {}
    # 6a: taken over (detached) and user closes it
    relay.call("detach", tabId=A); relay.call("activate", tabId=A); time.sleep(0.5); n0 = len(relay.events)
    out["close_detached"] = user_close(A); time.sleep(1)
    out["events_detached"] = [e for e in relay.events[n0:] if e.get("type") in ("tabRemoved", "detached")]
    # 6b: user closes a tab the bot is still attached to (take-over without detach)
    relay.call("activate", tabId=B); time.sleep(0.5); n1 = len(relay.events)
    out["close_attached"] = user_close(B); time.sleep(1)
    out["events_attached"] = [e for e in relay.events[n1:] if e.get("type") in ("tabRemoved", "detached")]
    try: relay.evaluate(B, "1"); out["cdp_after_close"] = "worked?!"
    except Exception as e: out["cdp_after_close"] = str(e)
    # recovery without relaunch
    t0 = time.time(); N, NW = mkbot(h="recovered"); type_text(relay, N, "recovered.'"); out["recover_val"] = val(N); out["recover_s"] = round(time.time() - t0, 2)
    out["chrome_same_pid"] = sess["proc"].poll() is None
    out["pass"] = (out["recover_val"] == "recovered.'" and out["chrome_same_pid"]
                   and any(e.get("type") == "tabRemoved" for e in out["events_detached"])
                   and any(e.get("type") == "detached" for e in out["events_attached"]))
    rec("T6", out)
    write_session({"port": PORT, "pid": CPID, "ext_id": ext_id, "bot_tab": N, "bot_window": NW, "bot_target": target_of(N), "page": BASE})
except Exception:
    rec("T6", {"pass": False, "err": traceback.format_exc()})
relay.close()
print("chrome left running pid", CPID)
