"""Infobar presence inferred from outerHeight-innerHeight; isTrusted of CDP input; navigator.webdriver."""
import asyncio, json, os, subprocess, sys, tempfile, urllib.parse
import websockets

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAGE = "data:text/html," + urllib.parse.quote(
    "<button id=b style='width:300px;height:100px'>x</button><input id=i>"
    "<script>window.ev=[];b.onclick=e=>ev.push('click:'+e.isTrusted);"
    "i.onkeydown=e=>ev.push('key:'+e.isTrusted)</script>")


async def run(extra):
    udd = tempfile.mkdtemp(prefix="cdp-ib-")
    proc = subprocess.Popen([CHROME, f"--user-data-dir={udd}", "--remote-debugging-port=0", "--no-first-run",
                             "--no-default-browser-check", "--window-size=900,700", *extra, PAGE],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pf = os.path.join(udd, "DevToolsActivePort")
    while not (os.path.exists(pf) and "\n" in open(pf).read()): await asyncio.sleep(0.1)
    port, path = open(pf).read().split("\n")[:2]
    await asyncio.sleep(2.5)
    async with websockets.connect(f"ws://127.0.0.1:{port}{path}") as ws:
        n = 0
        async def send(m, p=None, sid=None):
            nonlocal n; n += 1; my = n
            msg = {"id": my, "method": m, "params": p or {}}
            if sid: msg["sessionId"] = sid
            await ws.send(json.dumps(msg))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == my: return r.get("result", r.get("error"))
        tg = [t for t in (await send("Target.getTargets"))["targetInfos"] if t["type"] == "page"][0]
        sid = (await send("Target.attachToTarget", {"targetId": tg["targetId"], "flatten": True}))["sessionId"]
        ev = lambda e: send("Runtime.evaluate", {"expression": e, "returnByValue": True}, sid)
        dims = (await ev("[outerHeight-innerHeight, navigator.webdriver]"))["result"]["value"]
        for t in ("mousePressed", "mouseReleased"):
            await send("Input.dispatchMouseEvent", {"type": t, "x": 100, "y": 50, "button": "left", "clickCount": 1}, sid)
        await ev("i.focus()")
        await send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65, "text": "a"}, sid)
        evs = (await ev("ev"))["result"]["value"]
        print(f"flags={extra or '(none)'}: chrome-height(outer-inner)={dims[0]}px navigator.webdriver={dims[1]} events={evs}")
        await send("Browser.close")
    proc.wait(5)


async def main():
    await run([])
    await run(["--enable-automation"])

asyncio.run(main())
