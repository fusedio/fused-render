"""Can a MINIMIZED or APP-HIDDEN (Cmd-H) headed Chrome window screenshot after a navigation?
Variants of post-navigation remedies: none | re-toggle focus emulation | setWebLifecycleState active |
setDeviceMetricsOverride | fromSurface=false | screencast frame instead of captureScreenshot."""
import sys, os, time, hashlib, subprocess, socket
sys.path.insert(0, os.path.dirname(__file__))
from cdp import launch, pages, WS, kill
S = os.path.dirname(os.path.dirname(__file__))
FLAGS = ["--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-background-timer-throttling"]
P1 = "data:text/html,<body style='background:red'><script>let i=0;function f(){i++;document.title=i;requestAnimationFrame(f)}f()</script>P1"
P2 = "data:text/html,<body style='background:blue'><script>let i=0;function f(){i++;document.title=i;requestAnimationFrame(f)}f()</script>P2"

def shot(ws, **kw):
    ws.sock.settimeout(4)
    t = time.time()
    try:
        r = ws.call("Page.captureScreenshot", format="jpeg", quality=40, **kw)
        return f"ok {len(r['data'])}B {(time.time()-t)*1000:.0f}ms", hashlib.md5(r["data"].encode()).hexdigest()[:6]
    except Exception as e:
        return f"HANG/{type(e).__name__}", "-"
    finally:
        ws.sock.settimeout(30)

def frames(ws, secs=2):
    ws.call("Page.startScreencast", format="jpeg", quality=30, maxWidth=400, maxHeight=300)
    n = 0; end = time.time() + secs
    while time.time() < end:
        try:
            ev = ws.wait_event("Page.screencastFrame", timeout=max(0.05, end - time.time()))
            n += 1; ws.notify("Page.screencastFrameAck", sessionId=ev["sessionId"])
        except Exception:
            break
    try: ws.call("Page.stopScreencast")
    except Exception: pass
    return n

def hide_app(on):
    subprocess.run(["osascript", "-e", f'tell application "System Events" to set visible of process "Google Chrome" to {"false" if on else "true"}'], capture_output=True)

def run(hide, remedy):
    sess = launch(f"{S}/profiles/hn-{hide}-{remedy}", headless=False, extra=FLAGS, url=P1, window=(600, 400))
    try:
        time.sleep(1.0)
        t = [p for p in pages(sess["port"]) if p["url"].startswith("data:")][0]
        ws = WS(t["webSocketDebuggerUrl"]); ws.call("Page.enable")
        ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
        win = ws.call("Browser.getWindowForTarget", targetId=t["id"])["windowId"]
        if hide == "min":
            ws.call("Browser.setWindowBounds", windowId=win, bounds={"windowState": "minimized"})
        else:
            hide_app(True)
        time.sleep(0.8)
        s0, h0 = shot(ws)
        ws.call("Page.navigate", url=P2); time.sleep(1.2)
        if remedy == "retoggle":
            ws.call("Emulation.setFocusEmulationEnabled", enabled=False); ws.call("Emulation.setFocusEmulationEnabled", enabled=True); time.sleep(0.3)
        elif remedy == "lifecycle":
            ws.call("Page.setWebLifecycleState", state="active"); time.sleep(0.3)
        elif remedy == "metrics":
            ws.call("Emulation.setDeviceMetricsOverride", width=600, height=400, deviceScaleFactor=1, mobile=False); time.sleep(0.3)
        vis = ws.evaluate("document.visibilityState")
        if remedy == "nosurface":
            s1, h1 = shot(ws, fromSurface=False)
        else:
            s1, h1 = shot(ws)
        n = frames(ws)
        print(f"{hide:4} {remedy:10} pre-nav shot={s0} | post-nav vis={vis} shot={s1} differs={h0 != h1} frames/2s={n}", flush=True)
        ws.close()
    finally:
        if hide == "cmdh": hide_app(False)
        kill(sess)

for hide in ["min", "cmdh"]:
    for remedy in ["none", "retoggle", "lifecycle", "metrics", "nosurface"]:
        run(hide, remedy)
