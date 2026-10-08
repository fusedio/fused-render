import sys, os, base64
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from playwright.sync_api import sync_playwright

flags = sys.argv[1] == "flags"
extra = ["--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
         "--disable-background-timer-throttling"] if flags else []
print("flags" if flags else "plain")


def probe(ctx, page, label):
    w_s = ctx.new_cdp_session(page)
    out = {}
    try:
        t = time.time(); png = page.screenshot(timeout=5000); out["pw_shot"] = (len(png), round((time.time() - t) * 1000))
    except Exception as e:
        out["pw_shot"] = "TIMEOUT " + type(e).__name__
    try:
        t = time.time()
        cs = w_s.send("Page.captureScreenshot", {"format": "png"})  # may hang; playwright cdp has no timeout -> guard via thread
        raw = base64.b64decode(cs["data"]); out["cdp_shot"] = (len(raw), round((time.time() - t) * 1000))
        open(f"{OUT}/t7_{label}.png", "wb").write(raw)
    except Exception as e:
        out["cdp_shot"] = "ERR " + str(e)[:80]
    frames = []
    w_s.on("Page.screencastFrame", lambda e: (frames.append(1), w_s.send("Page.screencastFrameAck", {"sessionId": e["sessionId"]})))
    w_s.send("Page.startScreencast", {"format": "jpeg", "quality": 60})
    end = time.time() + 3; n = 0
    while time.time() < end:
        try:
            page.evaluate(f"document.body.style.background='hsl({n*20%360},80%,50%)'")
        except Exception as e:
            out["eval"] = str(e)[:80]
        n += 1; page.wait_for_timeout(100)
    w_s.send("Page.stopScreencast")
    out["frames"] = len(frames)
    print(label, out)


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        SCR + "/profiles/d-1", channel="chrome", headless=False, viewport=None, args=["--window-size=900,700", "--window-position=50,50"] + extra)
    page = ctx.pages[0]
    page.goto("data:text/html,<body>hello T7<textarea id=t></textarea>")
    s = ctx.new_cdp_session(page)
    w = s.send("Browser.getWindowForTarget")["windowId"]
    probe(ctx, page, "normal")
    # offscreen: move window far off the screen
    s.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"left": -5000, "top": -5000}})
    time.sleep(1)
    print("offscreen bounds", s.send("Browser.getWindowBounds", {"windowId": w}))
    probe(ctx, page, "offscreen")
    s.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"left": 50, "top": 50}})
    # second window (new window) covering the first = occluded; just test background tab
    pb = ctx.new_page(); pb.goto("data:text/html,<body>B")
    pb.bring_to_front()
    probe(ctx, page, "background_tab")
    # minimized (last: may hang)
    pb.close(); page.bring_to_front()
    s.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"windowState": "minimized"}})
    time.sleep(1)
    import threading
    th = threading.Thread(target=lambda: None)
    try:
        t = time.time(); page.screenshot(timeout=5000); print("minimized pw shot ok", round((time.time() - t) * 1000))
    except Exception as e:
        print("minimized pw shot:", type(e).__name__, str(e)[:100])
    try:
        raw = s.send("Page.captureScreenshot", {"format": "png"})
        print("minimized raw cdp shot bytes", len(raw["data"]))
    except Exception as e:
        print("minimized raw cdp shot err", str(e)[:100])
    # restore from minimized via CDP and via bring_to_front
    s.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"windowState": "normal"}})
    ctx.close()
