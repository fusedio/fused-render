import sys, os, time, json
S = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, f"{S}/lib"); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import *
from keys import keyparams, CHAR2CODE

def send(ws, kp):
    if "insert" in kp: ws.call("Input.insertText", text=kp["insert"]); return
    ws.call("Input.dispatchKeyEvent", **kp["down"]); ws.call("Input.dispatchKeyEvent", **kp["up"])

def press(ws, key, code="", **mods):
    send(ws, keyparams(key, code or key, **mods))

def type_str(ws, s):
    for ch in s:
        if ch == "\n": press(ws, "Enter"); continue
        c = CHAR2CODE.get(ch)
        if c: send(ws, keyparams(ch, c[0], shift=c[1]))
        else: send(ws, {"insert": ch})

def val(ws, sel="#t"): return ws.evaluate(f"document.querySelector('{sel}').value")
