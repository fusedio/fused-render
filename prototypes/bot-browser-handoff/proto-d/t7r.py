import sys, os, base64
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from playwright.sync_api import sync_playwright

sess = cdp.launch(SCR + "/profiles/d-2", headless=False, window=(900, 700), url="data:text/html,<body><h1>RAW</h1><script>setInterval(()=>document.body.style.background=`hsl(${Date.now()/10%360},80%,50%)`,16)</script>")
port = sess["port"]
try:
    tid = cdp.pages(port)[0]["id"]
    ws = cdp.WS(f"ws://127.0.0.1:{port}/devtools/page/{tid}", timeout=8)
    bws = cdp.WS(cdp.http(port, "/json/version")["webSocketDebuggerUrl"], timeout=8)
    wid = bws.call("Browser.getWindowForTarget", targetId=tid)["windowId"]
    ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "minimized"})
    time.sleep(1)
    print("state", bws.call("Browser.getWindowBounds", windowId=wid)["bounds"]["windowState"], flush=True)
    # raw captureScreenshot
    try:
        t = time.time(); r = ws.call("Page.captureScreenshot", format="png"); raw = base64.b64decode(r["data"])
        open(OUT + "/t7_min_connect_raw.png", "wb").write(raw); print("raw shot bytes", len(raw), round((time.time() - t) * 1000), "ms", flush=True)
    except Exception as e:
        print("raw shot FAIL", type(e).__name__, e, flush=True)
    # screencast frames on raw session
    try:
        ws.call("Page.startScreencast", format="jpeg", quality=60)
        n = 0; end = time.time() + 3
        ws.sock.settimeout(1)
        while time.time() < end:
            try:
                m = ws.recv()
            except Exception:
                continue
            if m.get("method") == "Page.screencastFrame":
                n += 1; ws.notify("Page.screencastFrameAck", sessionId=m["params"]["sessionId"])
        print("raw frames in 3s", n, flush=True)
    except Exception as e:
        print("screencast FAIL", e, flush=True)
    # Playwright connect: its own session, then page.screenshot
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = br.contexts[0].pages[0]
        try:
            t = time.time(); png = page.screenshot(timeout=5000)
            open(OUT + "/t7_min_connect_pw.png", "wb").write(png); print("pw shot bytes", len(png), round((time.time() - t) * 1000), "ms", flush=True)
        except Exception as e:
            print("pw shot FAIL", type(e).__name__, flush=True)
        print("pw typing", page.evaluate("1+1"), flush=True)
finally:
    cdp.kill(sess)
