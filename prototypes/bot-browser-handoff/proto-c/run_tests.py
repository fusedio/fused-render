"""T1..T7 for approach C. Leaves Chrome running + session.json for T8 (t8.py)."""
import json, os, statistics, sys, time, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from relay import Relay

R = {}; LOG = os.path.join(OUT, "run_tests.json")
def save(): json.dump(R, open(LOG, "w"), indent=1, default=str)
def rec(k, v): R[k] = v; print(k, json.dumps(v, default=str)[:600], flush=True); save()

PAGE = serve_page() + "?h=bot"
relay = Relay()
sess, ext_id, ok, dt = launch_bridge("C-1", relay=relay)
CPID = sess["pid"]
rec("setup", {"port": sess["port"], "pid": CPID, "ext_id": ext_id, "bridge_connected": ok, "launch_to_bridge_s": round(dt, 2), "hello": relay.hello})

def target_of(tab):
    for t in relay.call("targets"):
        if t.get("tabId") == tab: return t["id"]

def ready(tab, url=PAGE):
    relay.cdp(tab, "Page.enable"); relay.cdp(tab, "Page.navigate", url=url); time.sleep(0.6)
    relay.evaluate(tab, "document.getElementById('t') && document.getElementById('t').focus()")

def val(tab): return relay.evaluate(tab, "document.getElementById('t').value")

# ---------- bot A window ----------
wa = relay.call("createWindow", url="about:blank", focused=False, width=800, height=600, left=40, top=60)
A = wa["tabId"]; WA = wa["windowId"]
relay.call("attach", tabId=A); ready(A)
TA = target_of(A)

# ---------- T1 ----------
try:
    t0 = time.time(); type_text(relay, A, TEXT); press(relay, A, "Enter"); v1 = val(A); t_type = time.time() - t0
    press(relay, A, "ArrowLeft", 3); press(relay, A, "Backspace"); v2 = val(A)
    exp2 = (TEXT + "\n")[:-4] + (TEXT + "\n")[-3:]
    select_all(relay, A); type_text(relay, A, "q"); v3 = val(A)
    rec("T1", {"pass": v1 == TEXT + "\n" and v2 == exp2 and v3 == "q", "v1": v1, "v1_exact": v1 == TEXT + "\n", "v2": v2, "v2_ok": v2 == exp2, "v3": v3, "type_s": round(t_type, 2)})
except Exception as e:
    rec("T1", {"pass": False, "err": traceback.format_exc()})

# ---------- T2 ----------
try:
    # 2a: take over WITHOUT detaching (debugger still attached, infobar visible)
    relay.call("activate", tabId=A); relay.evaluate(A, "document.getElementById('t').focus()"); time.sleep(0.5)
    h = human_input(CPID, sess['port'], TA, HUMAN); time.sleep(0.5)
    va = val(A); screencap(os.path.join(OUT, "t2_attached.png"))
    # 2b: take over WITH detach
    t0 = time.time(); relay.call("detach", tabId=A); relay.call("activate", tabId=A); flip = time.time() - t0
    time.sleep(0.5)
    h2 = human_input(CPID, sess['port'], TA, HUMAN); time.sleep(0.5); screencap(os.path.join(OUT, "t2_detached.png"))
    relay.call("attach", tabId=A); vb = val(A)
    rec("T2", {"pass": va == "q" + HUMAN and vb == "q" + HUMAN * 2, "attached_val": va, "detached_val": vb, "human_osa": [h, h2], "detach+activate_s": round(flip, 3)})
except Exception as e:
    rec("T2", {"pass": False, "err": traceback.format_exc()})

# ---------- T3 ----------
try:
    relay.evaluate(A, "(()=>{const t=document.getElementById('t');t.focus();t.selectionStart=t.selectionEnd=t.value.length})()")
    type_text(relay, A, "-bot"); v = val(A); TA2 = target_of(A)
    alive = sess["proc"].poll() is None
    rec("T3", {"pass": v.endswith(HUMAN + "-bot") and TA2 == TA and alive, "val": v, "tabId": A, "targetId_before": TA, "targetId_after": TA2, "chrome_pid_alive_same": alive})
except Exception as e:
    rec("T3", {"pass": False, "err": traceback.format_exc()})

# ---------- T4 ----------
try:
    take, back, fails = [], [], []
    for i in range(20):
        try:
            t0 = time.time(); relay.call("detach", tabId=A); relay.call("activate", tabId=A); take.append(time.time() - t0)
            t0 = time.time(); relay.call("attach", tabId=A); relay.call("windowState", windowId=WA, update={"state": "minimized"})
            assert relay.evaluate(A, "1+1") == 2; back.append(time.time() - t0)
        except Exception as e:
            fails.append((i, str(e)))
            try: relay.call("attach", tabId=A)
            except Exception: pass
    q = lambda xs: {"p50_ms": round(statistics.median(xs) * 1000, 1), "p95_ms": round(sorted(xs)[int(len(xs) * .95) - 1] * 1000, 1)} if xs else None
    v = val(A)
    rec("T4", {"pass": not fails and v.endswith("-bot"), "fails": fails, "take_over": q(take), "hand_back": q(back), "val_still": v[-10:], "targetId_same": target_of(A) == TA})
except Exception as e:
    rec("T4", {"pass": False, "err": traceback.format_exc()})

# ---------- T5 ----------
try:
    wb = relay.call("createWindow", url="about:blank", focused=False, width=700, height=500, left=900, top=60)
    B = wb["tabId"]; WB = wb["windowId"]; relay.call("attach", tabId=B); relay.call("windowState", windowId=WB, update={"state": "minimized"})
    relay.call("detach", tabId=A); relay.call("activate", tabId=A)  # A taken over
    shots = []
    for i in range(5):
        ready(B, PAGE.replace("h=bot", f"h=B{i}"))
        t0 = time.time(); s = relay.cdp(B, "Page.captureScreenshot", format="png")["data"]; shots.append((png_stats(s), round(time.time() - t0, 3)))
        type_text(relay, B, f"b{i}.'")
        assert val(B) == f"b{i}.'", val(B)
    hA = human_input(CPID, sess['port'], TA, 'Z'); time.sleep(0.4)  # human still drives A meanwhile
    relay.call("attach", tabId=A); vA = val(A)
    rec("T5", {"pass": all(s[0][2] > 2 for s in shots) and vA.endswith("-botZ"), "B_shots(w,h,uniq),s": shots, "B_window": "minimized", "A_val_tail": vA[-8:], "human": hA})
except Exception as e:
    rec("T5", {"pass": False, "err": traceback.format_exc()})

# ---------- T7 (before T6 so we still have both bots) ----------
def shot_and_cast(tab, label):
    out = {}
    try:
        t0 = time.time(); s = relay.cdp(tab, "Page.captureScreenshot", format="png")["data"]; out["shot"] = png_stats(s); out["shot_s"] = round(time.time() - t0, 3)
    except Exception as e: out["shot_err"] = str(e)
    try:
        n0 = len(relay.events); relay.cdp(tab, "Page.startScreencast", format="jpeg", quality=50, everyNthFrame=1)
        relay.evaluate(tab, "window.__i && clearInterval(window.__i); window.__i=setInterval(()=>{document.body.style.background='hsl('+(Date.now()/10%360)+',60%,70%)'},50)")
        frames = 0; seen = set(); t0 = time.time()
        while time.time() - t0 < 3:
            for e in relay.events[n0:]:
                if e.get("method") == "Page.screencastFrame" and e.get("tabId") == tab and id(e) not in seen:
                    seen.add(id(e)); frames += 1
                    relay.cdp(tab, "Page.screencastFrameAck", sessionId=e["params"]["sessionId"])
            time.sleep(0.02)
        relay.cdp(tab, "Page.stopScreencast"); relay.evaluate(tab, "clearInterval(window.__i)")
        out["frames_3s"] = frames
    except Exception as e: out["cast_err"] = str(e)
    out["visibility"] = relay.evaluate(tab, "document.visibilityState")
    return out
try:
    t7 = {}
    relay.call("windowState", windowId=WB, update={"state": "minimized"}); time.sleep(0.5)
    t7["minimized_window"] = shot_and_cast(B, "min")
    relay.call("windowState", windowId=WB, update={"state": "normal"})
    bg = relay.call("createTab", windowId=WB, url="data:text/html,<h1>other</h1>", active=True); time.sleep(0.5)
    t7["background_tab"] = shot_and_cast(B, "bg")
    relay.call("closeTab", tabId=bg["tabId"])
    relay.call("windowState", windowId=WB, update={"state": "normal", "left": 40, "top": 60, "width": 700, "height": 500})
    relay.call("activate", tabId=A); time.sleep(0.5)  # A's window on top of B's -> B occluded
    t7["occluded_window"] = shot_and_cast(B, "occ")
    try:
        relay.call("windowState", windowId=WB, update={"left": 5000, "top": 3000}); time.sleep(0.5)
        t7["offscreen_attempt"] = shot_and_cast(B, "off")
    except Exception as e:
        R["T7_offscreen_note"] = str(e)
    ok7 = all(x.get("shot", (0, 0, 0))[2] > 2 and x.get("frames_3s", 0) > 0 for x in t7.values())
    rec("T7", {"pass": ok7, **t7})
except Exception as e:
    rec("T7", {"pass": False, "err": traceback.format_exc()})

# ---------- T6 ----------
def user_close(tab):
    fp = frontmost_pid()
    o = osa('tell application "System Events" to keystroke "w" using command down') if str(fp) == str(CPID) else ("", "not frontmost")
    if not o[1]: return {"path": "osascript cmd-W", "osa": o}
    tid = target_of(tab)  # fallback: Target.closeTarget over a separate port session (what the harness allows)
    bws = cdp.WS(cdp.http(sess["port"], "/json/version")["webSocketDebuggerUrl"]); r = bws.call("Target.closeTarget", targetId=tid); bws.close()
    return {"path": "cdp Target.closeTarget fallback", "osascript_err": o[1], "r": r}
try:
    relay.call("windowState", windowId=WB, update={"state": "minimized"})
    relay.call("detach", tabId=A); relay.call("activate", tabId=A); time.sleep(0.6)
    n0 = len(relay.events)
    osa(f'tell application "System Events" to set frontmost of (first process whose unix id is {CPID}) to true'); time.sleep(0.3)
    fp = frontmost_pid()
    closed = user_close(A)
    time.sleep(1.0)
    ev_detached = [e for e in relay.events[n0:] if e.get("type") in ("tabRemoved", "detached")]
    try: relay.call("getTab", tabId=A); gone = False
    except Exception as e: gone = str(e)
    # attached variant: bot B's tab attached, human closes it
    relay.call("activate", tabId=B); time.sleep(0.6); n1 = len(relay.events)
    fp2 = frontmost_pid()
    closed2 = user_close(B)
    time.sleep(1.0)
    ev_attached = [e for e in relay.events[n1:] if e.get("type") in ("tabRemoved", "detached")]
    try: relay.cdp(B, "Runtime.evaluate", expression="1"); b_err = None
    except Exception as e: b_err = str(e)
    # recovery: fresh window for the bot
    t0 = time.time()
    nw = relay.call("createWindow", url="about:blank", focused=False, width=800, height=600, left=40, top=60); A2 = nw["tabId"]
    relay.call("attach", tabId=A2); ready(A2); type_text(relay, A2, "recovered.'"); vr = val(A2); rec_s = time.time() - t0
    relay.call("windowState", windowId=nw["windowId"], update={"state": "minimized"})
    alive = sess["proc"].poll() is None
    rec("T6", {"pass": bool(gone) and bool(ev_detached) and vr == "recovered.'" and alive, "cmdW_detached": closed, "events_detached_case": ev_detached,
               "getTab_after": gone, "cmdW_attached": closed2, "events_attached_case": ev_attached, "cdp_on_closed_tab_err": b_err,
               "recover_val": vr, "recover_s": round(rec_s, 2), "chrome_same_pid": alive, "new_tabId": A2})
    write_session({"port": sess["port"], "pid": CPID, "ext_id": ext_id, "bot_tab": A2, "bot_window": nw["windowId"], "bot_target": target_of(A2)})
except Exception as e:
    rec("T6", {"pass": False, "err": traceback.format_exc()})

relay.close()
print("done; chrome left running for t8, pid", CPID)
