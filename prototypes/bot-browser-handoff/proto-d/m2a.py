"""Model 2: Chrome launched by us (lib/cdp.launch headed), Playwright connect_over_cdp."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from playwright.sync_api import sync_playwright

sess = cdp.launch(SCR + "/profiles/d-2", headless=False, window=(900, 700), url="about:blank")
port = sess["port"]
json.dump({"port": port, "pid": sess["pid"]}, open(OUT + "/m2_session.json", "w"))
print("chrome pid", sess["pid"], "port", port)
with sync_playwright() as p:
    t = time.time()
    br = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    print("connect ms", round((time.time() - t) * 1000), "contexts", len(br.contexts), "pages", [x.url for x in br.contexts[0].pages])
    ctx = br.contexts[0]
    page = ctx.pages[0]
    page.goto(URL); page.focus("#t")
    page.keyboard.type(S); page.keyboard.press("Enter")
    print("T1 exact", val(page) == S + "\n")
    tid = cdp.pages(port)[0]["id"]
    # --- fight-the-human tests while bot is idle ---
    # (a) dialog opened by page while bot has NO dialog handler: Playwright auto-dismisses?
    page.evaluate("setTimeout(()=>{window.__r = confirm('human sees this?')}, 300)")
    time.sleep(2)
    print("dialog auto-handled? confirm returned:", page.evaluate("window.__r"), "(false = auto-dismissed by Playwright; undefined = still open)")
    # (b) user navigates tab by hand (via side CDP) while bot idle, then bot uses same Page
    ws = cdp.WS(f"ws://127.0.0.1:{port}/devtools/page/{tid}")
    ws.call("Page.navigate", url="data:text/html,<textarea id=t></textarea><p>human nav</p>")
    time.sleep(0.5)
    page.focus("#t"); page.keyboard.type("after-nav")
    print("after human navigate bot types ->", repr(val(page)), "url", page.url[:30])
    # (c) idle 35 s (> 30s default timeout) then use
    time.sleep(35)
    page.keyboard.type("+idle35"); print("after 35s idle:", repr(val(page)))
    # (d) T6 in connect mode
    ws.close()
    w2 = cdp.WS(cdp.http(port, "/json/version")["webSocketDebuggerUrl"])
    w2.call("Target.closeTarget", targetId=tid)
    time.sleep(0.5)
    print("T6 is_closed", page.is_closed(), "ctx.pages", len(ctx.pages))
    try:
        page.keyboard.type("x")
    except Exception as e:
        print("T6 exc", type(e).__name__, str(e)[:100])
    try:
        pn = ctx.new_page(); pn.goto(URL); pn.focus("#t"); pn.keyboard.type("new"); print("T6 new page in same ctx:", val(pn))
        print("chrome pid unchanged:", subprocess.run(["pgrep", "-f", f"remote-debugging-port={port}"], capture_output=True, text=True).stdout.split()[0] == str(sess["pid"]))
    except Exception as e:
        print("T6 new_page fail", type(e).__name__, e)
        pn = None
    # window mgmt on connect model
    if pn:
        s = ctx.new_cdp_session(pn)
        print("window bounds", s.send("Browser.getWindowForTarget"))
    # leave state for T8: type a marker
    pn.keyboard.type("-m2a")
    json.dump({"port": port, "pid": sess["pid"], "tid": cdp.pages(port)[-1]["id"]}, open(OUT + "/m2_session.json", "w"))
    # disconnect (no close) and exit python
print("python exiting WITHOUT closing chrome; pids alive:", cdp.http(port, "/json/version")["Browser"])
