"""Launch-window options, app-hidden state, screenshot polling while minimized, lifecycle tricks."""
import sys, os, time, json, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import launch, kill, pages, http
from util import *

fh = open(f"{OUT}/explore.log", "w")

# 1) --no-startup-window
log(fh, "== launch --no-startup-window (url=None) ; frontmost before:", frontmost())
t0 = time.time()
sess = launch(f"{S}/profiles/A-explore", headless=False, url=None, extra=["--no-startup-window"], window=(900, 600))
log(fh, "up in ms", round((time.time()-t0)*1000), "pages:", [(p["id"][:8], p["url"][:40]) for p in pages(sess["port"])])
time.sleep(1.5)
log(fh, "frontmost after launch:", frontmost())
try:
    bws = browser_ws(sess["port"])
    log(fh, "windows via osascript:", osa(f'tell application "System Events" to get name of every window of (every process whose unix id is {sess["pid"]})'))
    t0 = time.time()
    tid = bws.call("Target.createTarget", url=PAGE, newWindow=True, background=True)["targetId"]
    log(fh, "createTarget(newWindow,background) ms", round((time.time()-t0)*1000))
    time.sleep(1.0); log(fh, "frontmost after createTarget:", frontmost())
    w = win(bws, tid); wid = w["windowId"]
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "minimized"})
    time.sleep(1.0); log(fh, "frontmost after minimize:", frontmost())
    ws = page_ws(sess["port"], tid); ws.call("Page.enable")

    # can a window be created ALREADY minimized? (createTarget has no state param) -> measure flash: time between create and minimize
    t0 = time.time()
    tid2 = bws.call("Target.createTarget", url="about:blank", newWindow=True, background=True)["targetId"]
    w2 = win(bws, tid2)["windowId"]
    bws.call("Browser.setWindowBounds", windowId=w2, bounds={"windowState": "minimized"})
    log(fh, "create+minimize total ms (window visible this long, plus genie anim):", round((time.time()-t0)*1000))
    time.sleep(0.8)
    # create offscreen-ish via bounds right after creation
    bws.call("Target.closeTarget", targetId=tid2)

    # 2) screenshot polling rate while minimized
    t0 = time.time(); n = 0; fresh = 0
    while time.time() - t0 < 3:
        r = ws.call("Page.captureScreenshot", format="jpeg", quality=50); n += 1
    log(fh, "minimized: captureScreenshot jpeg polling = %.1f fps" % (n / 3))
    log(fh, "minimized raf/s:", raf_rate(ws, 1))

    # 3) lifecycle / focus emulation tricks while minimized
    for name, fn in [("Emulation.setFocusEmulationEnabled", lambda: ws.call("Emulation.setFocusEmulationEnabled", enabled=True)),
                     ("Page.setWebLifecycleState active", lambda: ws.call("Page.setWebLifecycleState", state="active"))]:
        try:
            fn(); log(fh, name, "-> vis", ws.evaluate("document.visibilityState"), "raf/s", raf_rate(ws, 1), "cast", screencast(ws, 3))
        except Exception as e:
            log(fh, name, "ERR", e)

    # 4) app hidden (Cmd-H equivalent) with window normal, flags on
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "normal"})
    time.sleep(1.0)
    log(fh, "normal: vis", ws.evaluate("document.visibilityState"), "raf", raf_rate(ws, 1))
    log(fh, "hide app:", osa(f'tell application "System Events" to set visible of (every process whose unix id is {sess["pid"]}) to false'))
    time.sleep(1.2)
    log(fh, "app hidden: bounds", bws.call("Browser.getWindowBounds", windowId=wid)["bounds"], "vis", ws.evaluate("document.visibilityState"),
        "raf", raf_rate(ws, 1), "shot", shot(ws, save="explore_app_hidden"), "cast", screencast(ws, 3))
    type_text(ws, "q"); log(fh, "typed while hidden ->", ws.evaluate("document.getElementById('t').value"))
    t0 = time.time()
    log(fh, "unhide app:", osa(f'tell application "System Events" to set visible of (every process whose unix id is {sess["pid"]}) to true'), round((time.time()-t0)*1000), "ms")
    time.sleep(0.5); log(fh, "frontmost after unhide:", frontmost())
finally:
    kill(sess); log(fh, "killed")
