"""Isolate: does Emulation.setFocusEmulationEnabled make a MINIMIZED / occluded / background-tab page live? argv[1]=antibg 0|1"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import launch, kill, pages
from util import *

antibg = sys.argv[1] == "1"
fh = open(f"{OUT}/focusemu_antibg{int(antibg)}.log", "w")
sess = launch(f"{S}/profiles/A-fe-{int(antibg)}", headless=False, url=None, extra=["--no-startup-window"], antibg=antibg, window=(900, 600))
try:
    bws = browser_ws(sess["port"])
    tid = bws.call("Target.createTarget", url=PAGE, newWindow=True, background=True)["targetId"]
    wid = win(bws, tid)["windowId"]
    ws = page_ws(sess["port"], tid); ws.call("Page.enable")
    def m(label):
        time.sleep(1.0)
        r = {"state": bws.call("Browser.getWindowBounds", windowId=wid)["bounds"]["windowState"], "vis": ws.evaluate("document.visibilityState"),
             "raf": raf_rate(ws, 1)}
        ws.evaluate("document.body.style.background='#22d';1"); r["shot"] = shot(ws, save=f"fe_{label}_{int(antibg)}"); ws.evaluate("document.body.style.background='#d22';1")
        r["cast"] = screencast(ws, 3); log(fh, label, json.dumps(r)); return r
    set_state(bws, wid, "minimized"); m("minimized_plain")
    ws.call("Emulation.setFocusEmulationEnabled", enabled=True); m("minimized_focusemu")
    # does it survive across a second CDP session / reconnect? new WS connection on the same target
    ws.close(); ws = page_ws(sess["port"], tid); ws.call("Page.enable"); m("minimized_after_reconnect_no_emu")
    ws.call("Emulation.setFocusEmulationEnabled", enabled=True); m("minimized_focusemu_again")
    ws.call("Emulation.setFocusEmulationEnabled", enabled=False); m("minimized_focusemu_off")
    # occluded by another window (normal state)
    set_state(bws, wid, "normal")
    occ = bws.call("Target.createTarget", url="data:text/html,<body style='background:%2300f'>occ", newWindow=True)["targetId"]
    ow = win(bws, occ)["windowId"]; bws.call("Browser.setWindowBounds", windowId=ow, bounds={"left": 0, "top": 30, "width": 1400, "height": 900})
    page_ws(sess["port"], occ).call("Page.bringToFront")
    m("occluded_plain")
    ws.call("Emulation.setFocusEmulationEnabled", enabled=True); m("occluded_focusemu")
    ws.call("Emulation.setFocusEmulationEnabled", enabled=False)
    bws.call("Target.closeTarget", targetId=occ)
    # background tab in same window
    ws.call("Page.bringToFront")
    fg = bws.call("Target.createTarget", url="data:text/html,fg")["targetId"]; page_ws(sess["port"], fg).call("Page.bringToFront")
    m("bgtab_plain")
    try:
        ws.call("Emulation.setFocusEmulationEnabled", enabled=True); m("bgtab_focusemu")
    except Exception as e:
        import traceback; log(fh, "bgtab_focusemu ERR", repr(e), traceback.format_exc()[-600:])
finally:
    kill(sess); log(fh, "killed")
