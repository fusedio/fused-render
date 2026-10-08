import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import *
sess = launch(f"{S}/profiles/B3", headless=True)
try:
    ws = WS(pages(sess["port"])[0]["webSocketDebuggerUrl"]); ws.call("Page.enable")
    ws.call("Page.navigate", url="data:text/html,<textarea id=t autofocus></textarea>"); time.sleep(.7)
    ws.call("Page.startScreencast", format="jpeg", maxWidth=640, maxHeight=400); time.sleep(.5)
    def vs(l): time.sleep(.3); print(l, ws.evaluate("document.visibilityState"))
    base = dict(type="rawKeyDown", key="Shift", code="ShiftLeft", windowsVirtualKeyCode=16, modifiers=8)
    for label, extra in [("plain", {}), ("autoRepeat=False", {"autoRepeat": False}), ("native=16", {"nativeVirtualKeyCode": 16}), ("native+autoRepeat", {"nativeVirtualKeyCode": 16, "autoRepeat": False})]:
        ws.call("Input.dispatchKeyEvent", **base, **extra); vs(label)
        ws.call("Input.dispatchKeyEvent", type="keyUp", key="Shift", code="ShiftLeft", windowsVirtualKeyCode=16, modifiers=0)
finally: kill(sess)
