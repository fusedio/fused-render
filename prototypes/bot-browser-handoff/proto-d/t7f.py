import sys, os, base64
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(SCR + "/profiles/d-1", channel="chrome", headless=False, viewport=None,
                                               args=["--window-size=900,700", "--window-position=50,50"])
    page = ctx.pages[0]
    page.goto("data:text/html,<body>hello T7<textarea id=t></textarea>")
    s = ctx.new_cdp_session(page)
    w = s.send("Browser.getWindowForTarget")["windowId"]
    s.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
    s.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"windowState": "minimized"}})
    time.sleep(1)
    print("state", s.send("Browser.getWindowBounds", {"windowId": w})["bounds"]["windowState"], flush=True)
    try:
        t = time.time(); png = page.screenshot(timeout=5000); print("pw shot", len(png), round((time.time() - t) * 1000), "ms", flush=True)
        open(OUT + "/t7_min_focusemu.png", "wb").write(png)
    except Exception as e:
        print("pw shot FAIL", type(e).__name__, flush=True)
    frames = []
    s.on("Page.screencastFrame", lambda e: (frames.append(1), s.send("Page.screencastFrameAck", {"sessionId": e["sessionId"]})))
    s.send("Page.startScreencast", {"format": "jpeg", "quality": 60})
    end = time.time() + 3; n = 0
    while time.time() < end:
        page.evaluate(f"document.body.style.background='hsl({n*20%360},80%,50%)'"); n += 1; page.wait_for_timeout(100)
    s.send("Page.stopScreencast")
    print("frames", len(frames), flush=True)
    page.focus("#t"); page.keyboard.type(S); print("typing while minimized exact:", val(page) == S, flush=True)
    s.send("Browser.setWindowBounds", {"windowId": w, "bounds": {"windowState": "normal"}})
    ctx.close()
