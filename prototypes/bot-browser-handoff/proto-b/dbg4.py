import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import *
import urllib.request, urllib.parse
sess = launch(f"{S}/profiles/B4", headless=True)
try:
    a = pages(sess["port"])[0]; wa = WS(a["webSocketDebuggerUrl"]); wa.call("Page.enable")
    wa.call("Page.navigate", url="data:text/html,<body><div id=n></div><script>let i=0;setInterval(()=>n.textContent=++i,16)</script>"); time.sleep(.5)
    req = urllib.request.Request(f"http://127.0.0.1:{sess['port']}/json/new?about:blank", method="PUT"); b = json.loads(urllib.request.urlopen(req).read())
    wb = WS(b["webSocketDebuggerUrl"]); wb.call("Page.bringToFront"); time.sleep(.5)
    print("A after B front:", wa.evaluate("document.visibilityState"))
    wa.call("Emulation.setFocusEmulationEnabled", enabled=True); time.sleep(.5)
    print("A after focus emulation:", wa.evaluate("document.visibilityState"))
    wa.call("Page.startScreencast", format="jpeg", maxWidth=640, maxHeight=400); time.sleep(3)
    n = 0; t0 = time.time()
    while time.time() - t0 < 3:
        try:
            p = wa.wait_event("Page.screencastFrame", timeout=0.5); n += 1; wa.call("Page.screencastFrameAck", sessionId=p["sessionId"])
        except Exception: pass
    print("A frames in 3s (B in front):", n)
finally: kill(sess)
