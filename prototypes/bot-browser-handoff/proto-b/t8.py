import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env import *
R = f"{S}/results/B"; SF = f"{R}/session8.json"
phase = sys.argv[1]
if phase == "a":   # orchestrator #1: start everything, type, write session file, then DIE without killing Chrome
    e = Env("B8"); e.goto(TA); e.ws.evaluate("document.querySelector('#t').focus()"); e.bot_type("before-crash:")
    e.open_liveview()
    json.dump({"bot_port": e.bot["port"], "bot_pid": e.bot["pid"], "drv_port": e.drv["port"], "drv_pid": e.drv["pid"], "tid": e.tid, "hport": e.hport, "http_pid": e.http.pid, "orch_pid": os.getpid()}, open(SF, "w"))
    print("phase a wrote session; value:", val(e.ws), "orchestrator pid", os.getpid(), "exiting WITHOUT killing Chrome")
    os._exit(0)
elif phase == "b":  # orchestrator #2: fresh process, re-attach by port + target id
    s = json.load(open(SF)); print("orchestrator a alive?", os.path.exists(f"/proc/{s['orch_pid']}") or subprocess.run(["kill", "-0", str(s["orch_pid"])], capture_output=True).returncode == 0)
    t0 = time.time()
    tab = [p for p in pages(s["bot_port"]) if p["id"] == s["tid"]]
    assert tab, "tab not found"
    ws = WS(tab[0]["webSocketDebuggerUrl"]); ws.call("Page.enable"); ws.evaluate("document.querySelector('#t').focus()")
    print("reattach s:", round(time.time() - t0, 3), "value seen:", repr(val(ws)))
    type_str(ws, "after.restart'x"); v = val(ws); print("bot typed after reattach:", repr(v), "PASS" if v == "before-crash:after.restart'x" else "FAIL")
    # human path survives too: live view page in the driver Chrome kept its ws open?
    dws = WS(pages(s["drv_port"])[0]["webSocketDebuggerUrl"]); dws.call("Page.enable")
    print("liveview still streaming (page in driver Chrome, own ws to bot tab):", dws.evaluate("window.__open===true && !window.__closed && __stats.frames"))
    dws.evaluate("window.__input=true; document.getElementById('cap').focus(); 1")
    for ch in "H.i":
        cc = CHAR2CODE[ch.lower()];
        kp = keyparams(ch, cc[0], shift=ch.isupper());
        if ch.isupper(): dws.call("Input.dispatchKeyEvent", type="rawKeyDown", key="Shift", code="ShiftLeft", windowsVirtualKeyCode=16, modifiers=8)
        d = dict(kp["down"]); dws.call("Input.dispatchKeyEvent", **d); dws.call("Input.dispatchKeyEvent", **kp["up"])
        if ch.isupper(): dws.call("Input.dispatchKeyEvent", type="keyUp", key="Shift", code="ShiftLeft", windowsVirtualKeyCode=16)
    time.sleep(.3); v2 = val(ws); print("human path after orchestrator restart:", repr(v2), "PASS" if v2.endswith("H.i") else "FAIL")
    for pid in (s["bot_pid"], s["drv_pid"]):
        try: os.killpg(pid, signal.SIGTERM)
        except Exception as ex: print("kill", pid, ex)
    try: os.killpg(s["http_pid"], signal.SIGKILL)
    except Exception: pass
