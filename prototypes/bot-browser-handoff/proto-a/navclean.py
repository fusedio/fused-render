"""Navigate-then-screenshot in each hidden state, fresh Chrome per case. argv[1]=case argv[2]=antibg"""
import sys, os, time, json, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import launch, kill, pages
from util import *

case, antibg = sys.argv[1], sys.argv[2] == "1"
fh = open(f"{OUT}/navclean.log", "a")
sess = launch(f"{S}/profiles/A-nc-{case}", headless=False, url=None, extra=["--no-startup-window"], window=(900, 600), antibg=antibg)
try:
    bws = browser_ws(sess["port"])
    tid = bws.call("Target.createTarget", url=PAGE, newWindow=True, background=True)["targetId"]
    wid = win(bws, tid)["windowId"]; ws = page_ws(sess["port"], tid); ws.call("Page.enable")
    ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"left": 200, "top": 150, "width": 900, "height": 600})
    occ = None
    if case == "minimized": set_state(bws, wid, "minimized")
    elif case == "occluded":
        occ = bws.call("Target.createTarget", url="data:text/html,<body style='background:%2300f'>occ", newWindow=True)["targetId"]
        bws.call("Browser.setWindowBounds", windowId=win(bws, occ)["windowId"], bounds={"left": 0, "top": 30, "width": 1500, "height": 1000})
        page_ws(sess["port"], occ).call("Page.bringToFront")
    elif case == "offscreen":
        bws.call("Browser.setWindowBounds", windowId=wid, bounds={"left": 5000, "top": 3000})
    elif case == "offscreen_tiny":
        ws.call("Emulation.setDeviceMetricsOverride", width=900, height=520, deviceScaleFactor=2, mobile=False)
        bws.call("Browser.setWindowBounds", windowId=wid, bounds={"width": 100, "height": 60})
        bws.call("Browser.setWindowBounds", windowId=wid, bounds={"left": 5000, "top": 3000})
    elif case == "apphidden":
        osa(f'tell application "System Events" to set visible of (every process whose unix id is {sess["pid"]}) to false')
    time.sleep(1.2)
    out = {"case": case, "antibg": antibg, "bounds": bws.call("Browser.getWindowBounds", windowId=wid)["bounds"]}
    for i, url in enumerate([PAGE.replace("%23d22", "%2322d"), "https://example.com/", PAGE.replace("%23d22", "%2322d")]):
        ws.call("Page.navigate", url=url); time.sleep(1.5)
        ws.sock.settimeout(6); t0 = time.time()
        try:
            r = ws.call("Page.captureScreenshot", format="png"); st = png_stats(r["data"]); st["ms"] = round((time.time() - t0) * 1000)
        except Exception as e:
            st = {"ERR": repr(e)}
            ws.close(); ws = page_ws(sess["port"], tid); ws.call("Page.enable"); ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
        ws.sock.settimeout(30)
        st["vis"] = ws.evaluate("document.visibilityState"); st["raf"] = raf_rate(ws, 1)
        out[f"nav{i}"] = st
    out["cast"] = screencast(ws, 3)
    log(fh, json.dumps(out))
except Exception:
    log(fh, case, "FATAL", traceback.format_exc())
finally:
    if case == "apphidden": osa(f'tell application "System Events" to set visible of (every process whose unix id is {sess["pid"]}) to true')
    kill(sess)
