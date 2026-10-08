"""Find a 'hidden' state where screenshot + screencast still work under chrome.debugger."""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from relay import Relay
kill_c(); time.sleep(1)
relay = Relay(); PAGE = serve_page() + "?h=hidden"
sess, ext_id, ok, dt = launch_bridge("C-t7", relay=relay)
bws = cdp.WS(cdp.http(sess["port"], "/json/version")["webSocketDebuggerUrl"])
R = {}

def cast(tab, secs=3):
    out = {}
    try:
        t0 = time.time(); s = relay.call("cdp", tabId=tab, method="Page.captureScreenshot", params={"format": "png"}, timeout=6)["data"]
        out["shot"] = png_stats(s); out["shot_s"] = round(time.time() - t0, 2)
    except Exception as e: out["shot_err"] = repr(e)
    try:
        n0 = len(relay.events); relay.cdp(tab, "Page.startScreencast", format="jpeg", quality=40)
        relay.evaluate(tab, "window.__i && clearInterval(window.__i); window.__i=setInterval(()=>{document.body.style.background='hsl('+(Date.now()/10%360)+',60%,70%)'},50)")
        frames = 0; t0 = time.time(); i = n0
        while time.time() - t0 < secs:
            while i < len(relay.events):
                e = relay.events[i]; i += 1
                if e.get("method") == "Page.screencastFrame" and e.get("tabId") == tab:
                    frames += 1; relay.cdp(tab, "Page.screencastFrameAck", sessionId=e["params"]["sessionId"])
            time.sleep(0.02)
        relay.cdp(tab, "Page.stopScreencast"); out["frames"] = frames
    except Exception as e: out["cast_err"] = repr(e)
    try: out["vis"] = relay.evaluate(tab, "document.visibilityState")
    except Exception as e: out["vis_err"] = repr(e)
    return out

def mkbot():
    w = relay.call("createWindow", url="about:blank", focused=False, width=700, height=500, left=100, top=100)
    t = w["tabId"]; relay.call("attach", tabId=t); relay.cdp(t, "Page.enable"); relay.cdp(t, "Page.navigate", url=PAGE); time.sleep(0.8)
    tid = [x["id"] for x in relay.call("targets") if x.get("tabId") == t][0]
    return t, w["windowId"], tid

t, w, tid = mkbot()
R["normal_front"] = cast(t)
relay.call("windowState", windowId=w, update={"state": "minimized"}); time.sleep(0.6)
R["minimized"] = cast(t)
relay.cdp(t, "Emulation.setFocusEmulationEnabled", enabled=True)
R["minimized+focusEmulation"] = cast(t)
relay.call("windowState", windowId=w, update={"state": "normal"}); time.sleep(0.6)
R["restored_after_min"] = cast(t)

# CDP Browser.setWindowBounds can push further offscreen than chrome.windows.update (50% rule)?
bw = bws.call("Browser.getWindowForTarget", targetId=tid)["windowId"]
for left, top in [(-690, 100), (3000, 2000), (-10000, -10000)]:
    try:
        bws.call("Browser.setWindowBounds", windowId=bw, bounds={"left": left, "top": top, "width": 700, "height": 500, "windowState": "normal"}); time.sleep(0.6)
        got = bws.call("Browser.getWindowBounds", windowId=bw)["bounds"]
        R[f"offscreen_{left}_{top}"] = {"asked": [left, top], "got": got, **cast(t)}
    except Exception as e:
        R[f"offscreen_{left}_{top}"] = {"err": repr(e)}
bws.call("Browser.setWindowBounds", windowId=bw, bounds={"left": 100, "top": 100})

# background tab in a visible window
bg = relay.call("createTab", windowId=w, url="about:blank", active=True); time.sleep(0.6)
R["background_tab"] = cast(t)
relay.cdp(t, "Emulation.setFocusEmulationEnabled", enabled=True)
R["background_tab+focusEmulation"] = cast(t)
relay.call("closeTab", tabId=bg["tabId"])
json.dump(R, open(os.path.join(OUT, "t7probe.json"), "w"), indent=1)
for k, v in R.items(): print(k, json.dumps(v))
cdp.kill(sess); relay.close()
