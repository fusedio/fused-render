"""T7 clean: one fresh bot per hidden state; freshness = screenshot after red nav != after blue nav."""
import hashlib, os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from relay import Relay
kill_c(); time.sleep(1)
relay = Relay(); BASE = serve_page()
sess, ext_id, ok, dt = launch_bridge("C-t7c", relay=relay)
bws = cdp.WS(cdp.http(sess["port"], "/json/version")["webSocketDebuggerUrl"])
R = {}

def shot(tab):
    t0 = time.time()
    s = relay.call("cdp", tabId=tab, method="Page.captureScreenshot", params={"format": "png"}, timeout=8)["data"]
    return hashlib.md5(s.encode()).hexdigest()[:8], png_stats(s), round(time.time() - t0, 2)

def nav(tab, bg):
    relay.call("cdp", tabId=tab, method="Page.navigate", params={"url": BASE + "?h=x&bg=" + bg.replace("#", "%23")}, timeout=8); time.sleep(0.7)

def frames(tab, secs=3):
    n0 = len(relay.events); relay.cdp(tab, "Page.startScreencast", format="jpeg", quality=40)
    relay.evaluate(tab, "window.__i=setInterval(()=>{document.body.style.background='hsl('+(Date.now()/10%360)+',60%,70%)'},50)")
    n = 0; t0 = time.time(); i = n0
    while time.time() - t0 < secs:
        while i < len(relay.events):
            e = relay.events[i]; i += 1
            if e.get("method") == "Page.screencastFrame" and e.get("tabId") == tab:
                n += 1; relay.cdp(tab, "Page.screencastFrameAck", sessionId=e["params"]["sessionId"])
        time.sleep(0.02)
    relay.cdp(tab, "Page.stopScreencast"); relay.evaluate(tab, "clearInterval(window.__i)"); return n

def run(name, setup, focus_emu=False, born_hidden=False):
    out = {}
    try:
        w = relay.call("createWindow", url="about:blank", focused=False, width=700, height=500, left=150, top=120,
                       state="minimized" if born_hidden else "normal")
        t, wid = w["tabId"], w["windowId"]; relay.call("attach", tabId=t); relay.cdp(t, "Page.enable")
        tid = [x["id"] for x in relay.call("targets") if x.get("tabId") == t][0]
        if not born_hidden: nav(t, "#888")
        extra = setup(t, wid, tid) or {}; out.update(extra); time.sleep(0.6)
        if focus_emu: relay.cdp(t, "Emulation.setFocusEmulationEnabled", enabled=True)
        out["vis"] = relay.evaluate(t, "document.visibilityState")
        try:
            nav(t, "#f00"); a = shot(t); nav(t, "#00f"); b = shot(t)
            out["shot_red"], out["shot_blue"] = a, b; out["fresh"] = a[0] != b[0] and a[1][2] > 2
        except Exception as e: out["shot_err"] = repr(e)
        try: out["frames_3s"] = frames(t)
        except Exception as e: out["cast_err"] = repr(e)
        out["pass"] = bool(out.get("fresh")) and out.get("frames_3s", 0) > 0
        try: relay.call("windowState", windowId=wid, update={"state": "normal"}); relay.call("detach", tabId=t); relay.call("closeTab", tabId=t)
        except Exception as e: out["cleanup_err"] = repr(e)
    except Exception as e: out["err"] = repr(e)
    R[name] = out; print(name, json.dumps(out), flush=True)

def minimize(t, w, tid): relay.call("windowState", windowId=w, update={"state": "minimized"})
def bgtab(t, w, tid): relay.call("createTab", windowId=w, url="about:blank", active=True)
def offscreen(t, w, tid):
    bw = bws.call("Browser.getWindowForTarget", targetId=tid)["windowId"]
    bws.call("Browser.setWindowBounds", windowId=bw, bounds={"left": -10000, "top": -10000}); return {"bounds": bws.call("Browser.getWindowBounds", windowId=bw)["bounds"]}
def none(t, w, tid): pass

run("visible_front(control)", none)
run("minimized_after_paint", minimize)
run("minimized_after_paint+focusEmu", minimize, focus_emu=True)
run("born_minimized", none, born_hidden=True)
run("born_minimized+focusEmu", none, focus_emu=True, born_hidden=True)
run("background_tab", bgtab)
run("background_tab+focusEmu", bgtab, focus_emu=True)
run("offscreen_clamped", offscreen)
json.dump(R, open(os.path.join(OUT, "t7clean.json"), "w"), indent=1)
cdp.kill(sess); relay.close()
