"""Empirical: does a minimized / occluded headed Chrome window on macOS keep
producing Page.startScreencast frames and Page.captureScreenshot?
Also: does rAF keep running (document.visibilityState)?
Usage: python3 minimize_test.py [extra chrome flags...]
"""
import asyncio, json, os, subprocess, sys, tempfile, time, base64

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

try:
    import websockets
except ImportError:
    print("need websockets"); sys.exit(1)

import urllib.parse
PAGE = "data:text/html," + urllib.parse.quote(
    "<body style='margin:0'><div id=d style='width:100vw;height:100vh'></div><script>"
    "let n=0;function f(){n++;document.getElementById('d').style.background="
    "'hsl('+(n%360)+',80%,50%)';requestAnimationFrame(f)}f();window.raf=()=>n;</script>"
)


class CDP:
    def __init__(self, ws):
        self.ws = ws; self.id = 0; self.pending = {}; self.events = []

    async def reader(self):
        async for msg in self.ws:
            m = json.loads(msg)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
            else:
                self.events.append(m)
                if m.get("method") == "Page.screencastFrame":
                    asyncio.ensure_future(self.send("Page.screencastFrameAck",
                        {"sessionId": m["params"]["sessionId"]}, sid=m.get("sessionId")))

    async def send(self, method, params=None, sid=None):
        self.id += 1
        fut = asyncio.get_event_loop().create_future()
        self.pending[self.id] = fut
        msg = {"id": self.id, "method": method, "params": params or {}}
        if sid: msg["sessionId"] = sid
        await self.ws.send(json.dumps(msg))
        r = await asyncio.wait_for(fut, 10)
        if "error" in r: return {"error": r["error"]}
        return r.get("result", {})


async def measure(cdp, sid, label, secs=3.0):
    start = len([e for e in cdp.events if e.get("method") == "Page.screencastFrame"])
    raf0 = (await cdp.send("Runtime.evaluate", {"expression": "raf()"}, sid))["result"]["value"]
    vis = (await cdp.send("Runtime.evaluate", {"expression": "document.visibilityState+'/'+document.hasFocus()"}, sid))["result"]["value"]
    await asyncio.sleep(secs)
    raf1 = (await cdp.send("Runtime.evaluate", {"expression": "raf()"}, sid))["result"]["value"]
    frames = len([e for e in cdp.events if e.get("method") == "Page.screencastFrame"]) - start
    t = time.time()
    try:
        shot = await asyncio.wait_for(cdp.send("Page.captureScreenshot", {"format": "png"}, sid), 8)
        shot_ok = "data" in shot; shot_ms = int((time.time() - t) * 1000)
        shot_info = f"ok {shot_ms}ms" if shot_ok else f"err {shot}"
    except asyncio.TimeoutError:
        shot_info = "TIMEOUT"
    print(f"{label:28s} visibility={vis:16s} rAF/s={(raf1-raf0)/secs:6.1f} screencast frames/s={frames/secs:5.1f} screenshot={shot_info}")


async def main():
    extra = sys.argv[1:]
    udd = tempfile.mkdtemp(prefix="cdp-min-test-")
    args = [CHROME, f"--user-data-dir={udd}", "--remote-debugging-port=0", "--no-first-run",
            "--no-default-browser-check", "--window-size=900,700", *extra, "about:blank"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = os.path.join(udd, "DevToolsActivePort")
    for _ in range(100):
        if os.path.exists(port_file) and open(port_file).read().count("\n") >= 1: break
        await asyncio.sleep(0.1)
    port, path = open(port_file).read().split("\n")[:2]
    print("flags:", extra or "(none)", "| DevToolsActivePort ->", port, path)
    async with websockets.connect(f"ws://127.0.0.1:{port}{path}", max_size=50_000_000) as ws:
        cdp = CDP(ws); rt = asyncio.ensure_future(cdp.reader())
        tgt = await cdp.send("Target.createTarget", {"url": PAGE, "newWindow": True})
        tid = tgt["targetId"]
        sid = (await cdp.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
        await cdp.send("Page.enable", {}, sid)
        await asyncio.sleep(1.5)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 50, "maxWidth": 640, "maxHeight": 480}, sid)
        win = (await cdp.send("Browser.getWindowForTarget", {"targetId": tid}))["windowId"]
        await measure(cdp, sid, "normal (front)")
        r = await cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "minimized"}})
        await asyncio.sleep(1.0)
        st = await cdp.send("Browser.getWindowBounds", {"windowId": win})
        print("   setWindowBounds minimized ->", r, "state now", st.get("bounds", {}).get("windowState"))
        await measure(cdp, sid, "minimized")
        await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True}, sid)
        await measure(cdp, sid, "minimized + focusEmulation")
        st = await cdp.send("Browser.getWindowBounds", {"windowId": win})
        print("   state after focusEmulation:", st.get("bounds", {}).get("windowState"))
        # two screenshots 0.5s apart must differ if page is live
        a=(await cdp.send("Page.captureScreenshot", {"format":"png"}, sid))["data"]
        await asyncio.sleep(0.5)
        b=(await cdp.send("Page.captureScreenshot", {"format":"png"}, sid))["data"]
        print("   minimized+focusEmu screenshots differ (live content):", a!=b)
        await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": False}, sid)
        await asyncio.sleep(0.5)
        a=(await cdp.send("Page.captureScreenshot", {"format":"png"}, sid))["data"]
        await asyncio.sleep(0.5)
        b=(await cdp.send("Page.captureScreenshot", {"format":"png"}, sid))["data"]
        print("   minimized (no emu) screenshots differ (live content):", a!=b, "len", len(a))
        await measure(cdp, sid, "minimized, focusEmu off again")
        await cdp.send("Page.setWebLifecycleState", {"state": "active"}, sid)
        await cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "normal"}})
        await asyncio.sleep(1.0)
        await measure(cdp, sid, "restored normal")
        # offscreen position instead of minimizing
        await cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"left": -3000, "top": 100}})
        await asyncio.sleep(1.0)
        b = await cdp.send("Browser.getWindowBounds", {"windowId": win})
        print("   moved offscreen ->", b.get("bounds"))
        await measure(cdp, sid, "moved off-screen (-3000)")
        # occlusion: cover with another normal window at same bounds, focused
        await cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"left": 100, "top": 100, "width": 900, "height": 700}})
        cover = await cdp.send("Target.createTarget", {"url": "about:blank", "newWindow": True})
        cw = (await cdp.send("Browser.getWindowForTarget", {"targetId": cover["targetId"]}))["windowId"]
        await cdp.send("Browser.setWindowBounds", {"windowId": cw, "bounds": {"left": 50, "top": 50, "width": 1100, "height": 900}})
        await cdp.send("Target.activateTarget", {"targetId": cover["targetId"]})
        await asyncio.sleep(2.0)
        await measure(cdp, sid, "occluded by other window")
        await cdp.send("Browser.close")
        rt.cancel()
    try: proc.wait(5)
    except Exception: proc.kill()


asyncio.run(main())
