"""Model 1: launch_persistent_context. Human = raw CDP session on a side port (osascript TCC-blocked)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from playwright.sync_api import sync_playwright
import urllib.request

PORT = cdp.free_port()
R = {}


def human_type(port, text, tid):
    """human path: raw CDP Input.insertText into target via separate ws (no Playwright)"""
    ws = cdp.WS(f"ws://127.0.0.1:{port}/devtools/page/{tid}")
    ws.call("Runtime.evaluate", expression="document.querySelector('#t').focus()")
    ws.call("Input.insertText", text=text)
    ws.close()


with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        SCR + "/profiles/d-1", channel="chrome", headless=False, viewport=None,
        args=["--window-size=900,700", "--window-position=50,50", f"--remote-debugging-port={PORT}"])
    try:
        info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version").read())
        print("side port works with persistent ctx:", info["Browser"])
    except Exception as e:
        print("side port FAIL", e); ctx.close(); raise
    page = ctx.pages[0]
    page.goto(URL)
    page.focus("#t")
    page.keyboard.press("Meta+a"); page.keyboard.press("Backspace")
    page.keyboard.type("bot")
    # tid
    tid = [t for t in cdp.pages(PORT) if t["url"].startswith("data:")][0]["id"]
    s = ctx.new_cdp_session(page)
    pid = s.send("SystemInfo.getProcessInfo") if False else None
    # T2: take over: bring_to_front, bot stops; human types
    t = time.time(); page.bring_to_front(); bf = (time.time() - t) * 1000
    human_type(PORT, "human.text's,here", tid)
    time.sleep(0.5)
    # bot idle for 40s: does Playwright time out/steal anything? (default timeout 30s only applies to calls)
    page.screenshot(path=OUT + "/t2_view.png")
    v = val(page)
    print("T2 human typed ->", repr(v), "bring_to_front ms", round(bf))
    R["T2"] = "human.text's,here" in v
    # T3: same Page object continues
    tid_before = tid
    page.focus("#t"); page.keyboard.type("-bot")
    v = val(page); print("T3", repr(v))
    tid_after = [t for t in cdp.pages(PORT) if t["url"].startswith("data:")][0]["id"]
    chrome_pid = int(subprocess.run(["pgrep", "-f", f"remote-debugging-port={PORT}"], capture_output=True, text=True).stdout.split()[0])
    R["T3"] = v.endswith("-bot") and tid_after == tid_before
    print("T3 pass", R["T3"], "tid same", tid_after == tid_before, "chrome pid", chrome_pid)

    # T4: churn 20 cycles
    lat_to, lat_back, fails = [], [], 0
    for i in range(20):
        try:
            t = time.time()
            page.bring_to_front()  # take over
            human_type(PORT, f"[h{i}]", tid)
            lat_to.append((time.time() - t) * 1000)
            t = time.time()
            page.focus("#t"); page.keyboard.type(f"[b{i}]")  # hand back
            lat_back.append((time.time() - t) * 1000)
            v = val(page)
            if f"[h{i}]" not in v or not v.endswith(f"[b{i}]"):
                fails += 1
        except Exception as e:
            fails += 1; print("churn err", i, e)
    print("T4 fails", fails, "takeover p50/p95", round(pct(lat_to, 50)), round(pct(lat_to, 95)),
          "handback p50/p95", round(pct(lat_back, 50)), round(pct(lat_back, 95)))
    R["T4"] = fails

    # T5: two bots, one process
    pb = ctx.new_page(); pb.goto(URL); pb.focus("#t")
    page.bring_to_front()  # A taken over
    human_type(PORT, "A-human", tid)
    ok = True
    for i in range(5):
        pb.goto("data:text/html,<body style='background:red'><h1>B%d</h1><textarea id=t></textarea>" % i)
        pb.focus("#t"); pb.keyboard.type(f"B-typed-{i}")
        png = pb.screenshot()
        if len(png) < 1000 or val(pb) != f"B-typed-{i}":
            ok = False
    print("T5 B ok", ok, "A value", repr(val(page)))
    page.focus("#t"); page.keyboard.type("-Aback"); print("T5 A back", val(page).endswith("-Aback"))
    R["T5"] = ok
    pb.screenshot(path=OUT + "/t5_B.png")

    # T6: user closes tab A during take-over
    page.bring_to_front()
    ws = cdp.WS(cdp.http(PORT, "/json/version")["webSocketDebuggerUrl"])
    ws.call("Target.closeTarget", targetId=tid)
    time.sleep(0.5)
    print("T6 page.is_closed()", page.is_closed(), "ctx pages", len(ctx.pages))
    try:
        page.keyboard.type("x")
    except Exception as e:
        print("T6 exception type:", type(e).__module__ + "." + type(e).__name__, "|", str(e)[:200])
    try:
        page.evaluate("1")
    except Exception as e:
        print("T6 evaluate exc:", type(e).__name__, "|", str(e)[:200])
    try:
        pn = ctx.new_page(); pn.goto(URL); pn.focus("#t"); pn.keyboard.type("new"); print("T6 new page works", val(pn))
        R["T6"] = True
    except Exception as e:
        print("T6 new_page fail", e); R["T6"] = False
    ws.close()

    # T7: minimize window, screenshot + screencast
    s = ctx.new_cdp_session(pn)
    w = s.send("Browser.getWindowForTarget")
    print("window", w)
    s.send("Browser.setWindowBounds", {"windowId": w["windowId"], "bounds": {"windowState": "minimized"}})
    time.sleep(1)
    print("state", s.send("Browser.getWindowBounds", {"windowId": w["windowId"]}))
    png = pn.screenshot(path=OUT + "/t7_min_pw.png")
    print("T7 pw screenshot bytes", len(png))
    frames = []
    s.on("Page.screencastFrame", lambda e: (frames.append(1), s.send("Page.screencastFrameAck", {"sessionId": e["sessionId"]})))
    s.send("Page.startScreencast", {"format": "jpeg", "quality": 60})
    # make page change so frames emit
    t_end = time.time() + 3
    n = 0
    while time.time() < t_end:
        pn.evaluate(f"document.body.style.background='hsl({n*20%360},80%,50%)'"); n += 1; pn.wait_for_timeout(100)
    s.send("Page.stopScreencast")
    cs = s.send("Page.captureScreenshot", {"format": "png"})
    import base64
    raw = base64.b64decode(cs["data"]); open(OUT + "/t7_min_cdp.png", "wb").write(raw)
    print("T7 minimized screencast frames", len(frames), "captureScreenshot bytes", len(raw))
    R["T7"] = (len(frames), len(raw))
    s.send("Browser.setWindowBounds", {"windowId": w["windowId"], "bounds": {"windowState": "normal"}})
    print(R)
    # T8 part: python restart; does chrome survive context exit?
    open(OUT + "/m1_pid.txt", "w").write(str(chrome_pid))
    print("closing ctx without killing; process exit next")
    ctx.close()
time.sleep(1)
r = subprocess.run(["pgrep", "-f", f"remote-debugging-port={PORT}"], capture_output=True, text=True).stdout.split()
print("chrome alive after ctx.close()+playwright stop:", bool(r))
