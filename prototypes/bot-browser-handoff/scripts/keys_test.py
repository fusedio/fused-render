"""Empirical check of CDP key forwarding recipes on headed Chrome (macOS)."""
import asyncio, json, os, subprocess, sys, tempfile, urllib.parse
import websockets

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAGE = "data:text/html," + urllib.parse.quote(
    "<input id=i autofocus style='font-size:30px'><script>window.log=[];"
    "for(const t of ['keydown','keypress','input','keyup'])"
    "i.addEventListener(t,e=>log.push(t+':'+(e.key||e.data)+':'+(e.keyCode||'')))</script>")


class CDP:
    def __init__(s, ws): s.ws, s.id, s.p = ws, 0, {}
    async def reader(s):
        async for m in s.ws:
            m = json.loads(m)
            if m.get("id") in s.p: s.p.pop(m["id"]).set_result(m)
    async def send(s, method, params=None, sid=None):
        s.id += 1; f = asyncio.get_event_loop().create_future(); s.p[s.id] = f
        msg = {"id": s.id, "method": method, "params": params or {}}
        if sid: msg["sessionId"] = sid
        await s.ws.send(json.dumps(msg)); r = await asyncio.wait_for(f, 10)
        return r.get("result", r.get("error"))


async def main():
    udd = tempfile.mkdtemp(prefix="cdp-keys-")
    proc = subprocess.Popen([CHROME, f"--user-data-dir={udd}", "--remote-debugging-port=0",
                             "--no-first-run", "--no-default-browser-check", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pf = os.path.join(udd, "DevToolsActivePort")
    while not (os.path.exists(pf) and "\n" in open(pf).read()): await asyncio.sleep(0.1)
    port, path = open(pf).read().split("\n")[:2]
    async with websockets.connect(f"ws://127.0.0.1:{port}{path}") as ws:
        c = CDP(ws); asyncio.ensure_future(c.reader())
        tid = (await c.send("Target.createTarget", {"url": PAGE}))["targetId"]
        sid = (await c.send("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
        await asyncio.sleep(1.0)
        await c.send("Runtime.evaluate", {"expression": "i.focus()"}, sid)

        async def val():
            return (await c.send("Runtime.evaluate", {"expression": "i.value"}, sid))["result"]["value"]
        async def reset():
            await c.send("Runtime.evaluate", {"expression": "i.value='abc';i.setSelectionRange(3,3);log=[]"}, sid)
        async def key(t, **kw):
            await c.send("Input.dispatchKeyEvent", {"type": t, **kw}, sid)

        # 1. buggy: VK = ord('.') = 46, keyDown with text (our old forwarder shape, guessed)
        await reset()
        await key("rawKeyDown", key=".", code="Period", windowsVirtualKeyCode=46)
        await key("char", text=".")
        await key("keyUp", key=".", code="Period", windowsVirtualKeyCode=46)
        print("1 VK46 rawKeyDown+char '.' ->", repr(await val()))
        # 1b. VK 46 with key='Delete'-less, no key/code (pure VK)
        await reset()
        await key("rawKeyDown", windowsVirtualKeyCode=46, nativeVirtualKeyCode=46)
        await key("keyUp", windowsVirtualKeyCode=46)
        print("1b VK46 rawKeyDown only (no key/code) ->", repr(await val()), "(caret at end; Delete is forward-delete)")
        await c.send("Runtime.evaluate", {"expression": "i.value='abc';i.setSelectionRange(1,1)"}, sid)
        await key("rawKeyDown", windowsVirtualKeyCode=46, nativeVirtualKeyCode=46)
        await key("keyUp", windowsVirtualKeyCode=46)
        print("1c VK46 rawKeyDown caret after 'a' ->", repr(await val()))
        # 2. correct puppeteer shape
        await reset()
        await key("keyDown", key=".", code="Period", windowsVirtualKeyCode=190, text=".", unmodifiedText=".")
        await key("keyUp", key=".", code="Period", windowsVirtualKeyCode=190)
        print("2 VK190 keyDown text '.' ->", repr(await val()))
        # 3. Cmd+A then type 'x' WITHOUT commands
        await reset()
        await key("rawKeyDown", key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=4)
        await key("keyUp", key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=4)
        await key("keyDown", key="x", code="KeyX", windowsVirtualKeyCode=88, text="x")
        await key("keyUp", key="x", code="KeyX", windowsVirtualKeyCode=88)
        print("3 Cmd+A (no commands) then x ->", repr(await val()))
        # 4. Cmd+A WITH commands
        await reset()
        await key("rawKeyDown", key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=4, commands=["selectAll"])
        await key("keyUp", key="a", code="KeyA", windowsVirtualKeyCode=65, modifiers=4)
        await key("keyDown", key="x", code="KeyX", windowsVirtualKeyCode=88, text="x")
        await key("keyUp", key="x", code="KeyX", windowsVirtualKeyCode=88)
        print("4 Cmd+A commands=[selectAll] then x ->", repr(await val()))
        # 5. insertText unicode
        await reset()
        await c.send("Input.insertText", {"text": "é🙂日本"}, sid)
        print("5 insertText ->", repr(await val()))
        # 6. setIgnoreInputEvents blocks CDP input?
        await reset()
        await c.send("Input.setIgnoreInputEvents", {"ignore": True}, sid)
        await key("keyDown", key="z", code="KeyZ", windowsVirtualKeyCode=90, text="z")
        await key("keyUp", key="z", code="KeyZ", windowsVirtualKeyCode=90)
        await c.send("Input.setIgnoreInputEvents", {"ignore": False}, sid)
        print("6 setIgnoreInputEvents(true) then CDP 'z' ->", repr(await val()))
        await c.send("Browser.close")
    proc.wait(5)

asyncio.run(main())
