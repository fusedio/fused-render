"""T7: screenshot + screencast while the bot's window is hidden in various ways. Run: python3 -I t7.py <antibg 0|1>"""
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import launch, kill, pages
from util import *

antibg = sys.argv[1] == "1"
fh = open(f"{OUT}/t7_antibg{int(antibg)}.log", "w")
log(fh, "frontmost before launch:", frontmost())
sess = launch(f"{S}/profiles/A-t7-{int(antibg)}", headless=False, antibg=antibg, window=(900, 600))
log(fh, "launched pid", sess["pid"], "port", sess["port"], "antibg", antibg)
res = {}
try:
    time.sleep(1.0)
    bws = browser_ws(sess["port"])
    log(fh, "frontmost after launch:", frontmost())
    # bot window, background=true
    tid = bws.call("Target.createTarget", url=PAGE, newWindow=True, background=True)["targetId"]
    time.sleep(1.0)
    log(fh, "frontmost after createTarget(newWindow,background):", frontmost())
    ws = page_ws(sess["port"], tid); ws.call("Page.enable"); ws.call("Runtime.enable")
    w = win(bws, tid); wid = w["windowId"]; log(fh, "bot window", w)
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"left": 100, "top": 100, "width": 900, "height": 600})

    def measure(label):
        time.sleep(1.2)
        m = {"bounds": bws.call("Browser.getWindowBounds", windowId=wid)["bounds"],
             "vis": ws.evaluate("document.visibilityState"), "hasFocus": ws.evaluate("document.hasFocus()"),
             "raf_per_s": raf_rate(ws, 1.0)}
        m["shot"] = shot(ws, save=f"t7_{label}_antibg{int(antibg)}")
        ws.evaluate("document.body.style.background='#22d';1")
        m["shot_after_dom_change"] = shot(ws)  # fresh? expect blue>0.5
        ws.evaluate("document.body.style.background='#d22';1")
        m["ticks_per_s"] = (lambda a: (time.sleep(2), round((ws.evaluate('k') - a) / 2, 1))[1])(ws.evaluate('k'))
        m["cast"] = screencast(ws, 3)
        # bot can still type while hidden?
        ws.evaluate("document.getElementById('t').value='';document.getElementById('t').focus();1")
        type_text(ws, "x.y'z")
        m["typed"] = ws.evaluate("document.getElementById('t').value")
        res[label] = m; log(fh, label, json.dumps(m))

    measure("visible")
    set_state(bws, wid, "minimized"); measure("minimized")
    set_state(bws, wid, "normal"); time.sleep(1.0)
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"left": -10000, "top": 100})
    time.sleep(0.5); log(fh, "after left=-10000 from normal:", bws.call("Browser.getWindowBounds", windowId=wid)["bounds"])
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"left": 5000, "top": 3000})
    time.sleep(0.5); log(fh, "after left=5000,top=3000:", bws.call("Browser.getWindowBounds", windowId=wid)["bounds"])
    measure("offscreen")
    log(fh, "offscreen bounds actually:", bws.call("Browser.getWindowBounds", windowId=wid)["bounds"])
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"left": 100, "top": 100, "width": 900, "height": 600})
    # occlude: another window of ours on top, same bounds, focused
    occ = bws.call("Target.createTarget", url="data:text/html,<body style='background:%2300f'>occluder", newWindow=True)["targetId"]
    owid = win(bws, occ)["windowId"]
    bws.call("Browser.setWindowBounds", windowId=owid, bounds={"left": 50, "top": 50, "width": 1100, "height": 750})
    ows = page_ws(sess["port"], occ); ows.call("Page.bringToFront")
    measure("occluded")
    # background tab: new tab in the occluder window? Instead: open a foreground tab in the bot's window
    bws.call("Target.closeTarget", targetId=occ); ows.close()
    fg = bws.call("Target.createTarget", url="data:text/html,<body style='background:%230f0'>fg tab", windowId=wid) if False else None
    # createTarget has no windowId param; new tab (no newWindow) lands in the last-active window
    ws.call("Page.bringToFront")
    fg = bws.call("Target.createTarget", url="data:text/html,<body style='background:%230f0'>fg tab")["targetId"]
    log(fh, "fg tab window", win(bws, fg)["windowId"], "bot window", wid)
    fws = page_ws(sess["port"], fg); fws.call("Page.bringToFront")
    measure("background_tab")
    set_state(bws, wid, "minimized"); measure("background_tab_minimized")
    json.dump(res, open(f"{OUT}/t7_antibg{int(antibg)}.json", "w"), indent=1)
finally:
    kill(sess)
    log(fh, "killed")
