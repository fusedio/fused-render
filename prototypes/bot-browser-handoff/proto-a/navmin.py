"""Isolate T5 hang: navigate a MINIMIZED window, then captureScreenshot. Variants: emu before/after nav, other window front or not, data: vs about:blank."""
import sys, os, time, json, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import launch, kill, pages
from util import *

fh = open(f"{OUT}/navmin.log", "w")
sess = launch(f"{S}/profiles/A-navmin", headless=False, url=None, extra=["--no-startup-window"], window=(900, 600))
try:
    bws = browser_ws(sess["port"])
    def mk():
        tid = bws.call("Target.createTarget", url=PAGE, newWindow=True, background=True)["targetId"]
        wid = win(bws, tid)["windowId"]; ws = page_ws(sess["port"], tid); ws.call("Page.enable")
        return tid, wid, ws
    def try_shot(ws, label):
        ws.sock.settimeout(6)
        t0 = time.time()
        try:
            r = ws.call("Page.captureScreenshot", format="png"); st = png_stats(r["data"]); st["ms"] = round((time.time()-t0)*1000)
        except Exception as e:
            st = {"ERR": repr(e), "ms": round((time.time()-t0)*1000)}
            ws.close()
        log(fh, label, json.dumps(st)); return st
    # case 1: minimized, emu ON before navigate
    tid, wid, ws = mk(); ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
    set_state(bws, wid, "minimized"); time.sleep(1)
    ws.call("Page.navigate", url=PAGE.replace("%23d22", "%2322d")); time.sleep(0.8)
    log(fh, "case1 vis", ws.evaluate("document.visibilityState"), "raf", raf_rate(ws, 1))
    try_shot(ws, "case1 min + emu-before-nav")
    # case 2: minimized, emu OFF, navigate
    tid2, wid2, ws2 = mk(); set_state(bws, wid2, "minimized"); time.sleep(1)
    ws2.call("Page.navigate", url=PAGE.replace("%23d22", "%2322d")); time.sleep(0.8)
    log(fh, "case2 vis", ws2.evaluate("document.visibilityState"))
    try_shot(ws2, "case2 min, no emu, nav")
    # case 3: minimized, emu turned on AFTER nav (what T5 did)
    tid3, wid3, ws3 = mk(); ws3.call("Emulation.setFocusEmulationEnabled", enabled=True); set_state(bws, wid3, "minimized"); time.sleep(1)
    ws3.call("Page.navigate", url=PAGE.replace("%23d22", "%2322d")); time.sleep(0.8)
    ws3.call("Emulation.setFocusEmulationEnabled", enabled=True)
    log(fh, "case3 vis", ws3.evaluate("document.visibilityState"))
    try_shot(ws3, "case3 min, emu set twice (before+after nav)")
    # case 4: a DIFFERENT window brought to front (as in T5 take-over of A), then navigate+shot the minimized one
    tidA, widA, wsA = mk(); wsA.call("Page.bringToFront"); activate_pid(sess["pid"]); time.sleep(0.8)
    tid4, wid4, ws4 = mk(); ws4.call("Emulation.setFocusEmulationEnabled", enabled=True); set_state(bws, wid4, "minimized"); time.sleep(1)
    ws4.call("Page.navigate", url=PAGE.replace("%23d22", "%2322d")); time.sleep(0.8)
    log(fh, "case4 vis", ws4.evaluate("document.visibilityState"))
    try_shot(ws4, "case4 other window front, min+emu, nav")
    # case 5: same as 4 but no navigation
    tid5, wid5, ws5 = mk(); ws5.call("Emulation.setFocusEmulationEnabled", enabled=True); set_state(bws, wid5, "minimized"); time.sleep(1)
    try_shot(ws5, "case5 other window front, min+emu, NO nav")
    # case 6: navigation to a cross-site http url (new renderer process) while minimized
    tid6, wid6, ws6 = mk(); ws6.call("Emulation.setFocusEmulationEnabled", enabled=True); set_state(bws, wid6, "minimized"); time.sleep(1)
    ws6.call("Page.navigate", url="https://example.com/"); time.sleep(2)
    log(fh, "case6 vis", ws6.evaluate("document.visibilityState"), ws6.evaluate("location.href"))
    try_shot(ws6, "case6 min+emu, nav to https://example.com")
except Exception:
    log(fh, "FATAL", traceback.format_exc())
finally:
    kill(sess); log(fh, "killed")
