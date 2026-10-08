"""Workarounds for: navigate while minimized -> captureScreenshot hangs."""
import sys, os, time, json, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import launch, kill, pages
from util import *

fh = open(f"{OUT}/navmin2.log", "w")
sess = launch(f"{S}/profiles/A-navmin", headless=False, url=None, extra=["--no-startup-window"], window=(900, 600))
NAV = PAGE.replace("%23d22", "%2322d")
try:
    bws = browser_ws(sess["port"])
    def mk(minimize=True):
        tid = bws.call("Target.createTarget", url=PAGE, newWindow=True, background=True)["targetId"]
        wid = win(bws, tid)["windowId"]; ws = page_ws(sess["port"], tid); ws.call("Page.enable")
        ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
        if minimize: set_state(bws, wid, "minimized"); time.sleep(1)
        return tid, wid, ws
    def try_shot(ws, label, **kw):
        ws.sock.settimeout(6); t0 = time.time()
        try:
            r = ws.call("Page.captureScreenshot", format="png", **kw); st = png_stats(r["data"]); st["ms"] = round((time.time()-t0)*1000)
        except Exception as e:
            st = {"ERR": repr(e), "ms": round((time.time()-t0)*1000)}
        ws.sock.settimeout(30)
        log(fh, label, json.dumps(st)); return st
    def nav(ws):
        ws.call("Page.navigate", url=NAV); time.sleep(0.8)

    # W1: captureBeyondViewport
    tid, wid, ws = mk(); nav(ws); try_shot(ws, "W1 captureBeyondViewport=True", captureBeyondViewport=True)
    # W2: fromSurface=False
    tid, wid, ws = mk(); nav(ws); try_shot(ws, "W2 fromSurface=False", fromSurface=False)
    # W3: device metrics override (forces resize)
    tid, wid, ws = mk(); nav(ws)
    ws.call("Emulation.setDeviceMetricsOverride", width=900, height=520, deviceScaleFactor=2, mobile=False); time.sleep(0.3)
    try_shot(ws, "W3 setDeviceMetricsOverride")
    log(fh, "W3 raf", raf_rate(ws, 1))
    ws.call("Emulation.clearDeviceMetricsOverride")
    # W4: navigate with window normal-but-occluded and APP HIDDEN (Cmd-H) — no window on screen at all
    tid, wid, ws = mk(minimize=False)
    log(fh, "hide app", osa(f'tell application "System Events" to set visible of (every process whose unix id is {sess["pid"]}) to false')); time.sleep(1)
    nav(ws); log(fh, "W4 vis", ws.evaluate("document.visibilityState"), "raf", raf_rate(ws, 1))
    try_shot(ws, "W4 app hidden, nav"); log(fh, "W4 cast", screencast(ws, 3))
    ws.call("Page.navigate", url="https://example.com/"); time.sleep(2); try_shot(ws, "W4b app hidden, nav https")
    log(fh, "unhide", osa(f'tell application "System Events" to set visible of (every process whose unix id is {sess["pid"]}) to true')); time.sleep(0.5)
    # W5: brief un-minimize after nav (flash) — how long until a shot works?
    tid, wid, ws = mk(); nav(ws)
    t0 = time.time(); set_state(bws, wid, "normal"); r = try_shot(ws, "W5 after normal"); set_state(bws, wid, "minimized")
    log(fh, "W5 unminimize+shot+minimize total ms", round((time.time()-t0)*1000))
    nav(ws); try_shot(ws, "W5b nav AGAIN while minimized (after one unminimize)")
    # W6: start screencast after nav while minimized
    tid, wid, ws = mk(); nav(ws); log(fh, "W6 screencast after nav while minimized", screencast(ws, 3))
except Exception:
    log(fh, "FATAL", traceback.format_exc())
finally:
    kill(sess); log(fh, "killed")
