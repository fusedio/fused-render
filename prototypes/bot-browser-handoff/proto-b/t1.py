import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import *
sess = launch(f"{S}/profiles/B-1", headless=True)
try:
    ws = WS(pages(sess["port"])[0]["webSocketDebuggerUrl"]); ws.call("Page.enable")
    ws.call("Page.navigate", url="data:text/html,<textarea id=t autofocus></textarea>"); time.sleep(.8)
    ws.evaluate("document.querySelector('#t').focus()")
    s = """a.b,c-d/e;f'g[h]i Hello World! (x) {y} <z> ~!@#$%^&*_+=|:"?"""
    type_str(ws, s); press(ws, "Enter")
    v = val(ws); print("exact:", v == s + "\n", repr(v))
    press(ws, "ArrowLeft"); press(ws,"ArrowLeft"); press(ws,"ArrowLeft"); press(ws,"Backspace")
    v2 = val(ws); print("after left x3+bksp:", repr(v2[-8:]))
    press(ws, "a", "KeyA", meta=True); type_str(ws, "q"); print("select-all q:", repr(val(ws)))
    type_str(ws, "xyz"); press(ws, "a", "KeyA", ctrl=True); type_str(ws, "r"); print("ctrl-A:", repr(val(ws)))
    ws.evaluate("document.querySelector('#t').value=''")
    type_str(ws, "héllo 日本 😀"); print("unicode:", repr(val(ws)))
    ws.call("Input.insertText", text="pasted\ttext\nline2"); print(repr(val(ws)))
finally: kill(sess)
