"""Prove the VK bug: dispatch '.' with windowsVirtualKeyCode = ord('.') (46 = VK_DELETE) vs 190 (US Period)."""
import sys, time, os
sys.path.insert(0, os.path.dirname(__file__))
from cdp import launch, pages, WS, kill
S = os.path.dirname(os.path.dirname(__file__))
sess = launch(f"{S}/profiles/vk", headless=True)
try:
    t = pages(sess["port"])[0]; ws = WS(t["webSocketDebuggerUrl"])
    ws.call("Page.enable"); ws.call("Page.navigate", url="data:text/html,<textarea id=t autofocus></textarea>"); time.sleep(0.8)
    ws.evaluate("document.getElementById('t').focus(); 'ok'")
    def key(ch, vk, code):
        ws.call("Input.dispatchKeyEvent", type="keyDown", key=ch, code=code, windowsVirtualKeyCode=vk, text=ch, unmodifiedText=ch)
        ws.call("Input.dispatchKeyEvent", type="keyUp", key=ch, code=code, windowsVirtualKeyCode=vk)
    US = {".": 190, ",": 188, "-": 189, "/": 191, ";": 186, "=": 187, "[": 219, "]": 221, "'": 222, "`": 192, "\\": 220}
    for ch, code in [(".", "Period"), (",", "Comma"), ("-", "Minus"), ("/", "Slash"), (";", "Semicolon"), ("[", "BracketLeft"), ("'", "Quote")]:
        for label, vk in [("ord()", ord(ch.upper())), ("US-layout", US[ch])]:
            ws.evaluate("document.getElementById('t').value='ab'; document.getElementById('t').setSelectionRange(1,1); 'ok'")
            key(ch, vk, code)
            got = ws.evaluate("document.getElementById('t').value")
            want = "a" + ch + "b"
            print(f"{ch!r:5} vk={vk:>3} ({label:9}) -> {got!r:8} {'OK' if got == want else 'BROKEN'}")
    ws.evaluate("document.getElementById('t').value=''; 'ok'")
    ws.call("Input.dispatchKeyEvent", type="keyDown", key=".", code="Period", text=".", unmodifiedText=".")
    ws.call("Input.dispatchKeyEvent", type="keyUp", key=".", code="Period")
    print("no-vk '.' ->", repr(ws.evaluate("document.getElementById('t').value")))
    ws.evaluate("document.getElementById('t').value=''; 'ok'")
    ws.call("Input.insertText", text=".")
    print("insertText '.' ->", repr(ws.evaluate("document.getElementById('t').value")))
finally:
    kill(sess)
