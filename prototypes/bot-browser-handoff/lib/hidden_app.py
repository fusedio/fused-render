"""App-hidden (NSRunningApplication.hide via System Events) headed Chrome + focus emulation:
animated page, 3 navigations, screenshot freshness, screencast fps, rAF rate, hide/unhide latency,
and a second bot window in the same process."""
import sys, os, time, hashlib, subprocess
sys.path.insert(0, os.path.dirname(__file__))
from cdp import launch, pages, WS, kill
S = os.path.dirname(os.path.dirname(__file__))
FLAGS = ["--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", "--disable-background-timer-throttling", "--no-startup-window"]
def anim(label, c):
    return (f"data:text/html,<body style='margin:0'><script>let i=0;function f(){{i++;document.body.style.background='rgb('+((i*7)&255)+',{c},'+((i*3)&255)+')';"
            f"document.title=i;requestAnimationFrame(f)}}f()</script>{label}")

def osa(script):
    t = time.time(); r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True); return (time.time() - t) * 1000, r.stderr.strip()
PID = [0]
def hide_app(on): return osa(f'tell application "System Events" to set visible of (first process whose unix id is {PID[0]}) to {"false" if on else "true"}')
def app_hidden(): return subprocess.run(["osascript", "-e", f'tell application "System Events" to get visible of (first process whose unix id is {PID[0]})'], capture_output=True, text=True).stdout.strip()

def shot(ws):
    ws.sock.settimeout(4); t = time.time()
    try:
        r = ws.call("Page.captureScreenshot", format="jpeg", quality=40)
        return f"ok/{(time.time()-t)*1000:.0f}ms", hashlib.md5(r["data"].encode()).hexdigest()[:6]
    except Exception as e:
        return f"HANG/{type(e).__name__}", "-"
    finally:
        ws.sock.settimeout(30)

def frames(ws, secs=2):
    ws.call("Page.startScreencast", format="jpeg", quality=30, maxWidth=400, maxHeight=300)
    n = 0; end = time.time() + secs
    while time.time() < end:
        try:
            ev = ws.wait_event("Page.screencastFrame", timeout=max(0.05, end - time.time())); n += 1
            ws.notify("Page.screencastFrameAck", sessionId=ev["sessionId"])
        except Exception: break
    try: ws.call("Page.stopScreencast")
    except Exception: pass
    return n

def raf(ws):
    a = ws.evaluate("+document.title"); time.sleep(1.0); return ws.evaluate("+document.title") - a

def new_bot(port, url):
    b = WS([t for t in __import__("cdp").http(port, "/json/list") if t["type"] == "browser"][0]["webSocketDebuggerUrl"]) if False else None
    import cdp
    info = cdp.http(port, "/json/version"); bws = WS(info["webSocketDebuggerUrl"])
    tid = bws.call("Target.createTarget", url=url, newWindow=True, background=True)["targetId"]; bws.close()
    time.sleep(0.8)
    t = [p for p in pages(port) if p["id"] == tid][0]
    ws = WS(t["webSocketDebuggerUrl"]); ws.call("Page.enable"); ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
    return ws, tid

sess = launch(f"{S}/profiles/ha-1", headless=False, extra=FLAGS, url="about:blank", window=(900, 600))
PID[0] = sess["pid"]
try:
    time.sleep(0.8)
    a, atid = new_bot(sess["port"], anim("A1", 100))
    b, btid = new_bot(sess["port"], anim("B1", 200))
    print("app visible before hide:", app_hidden())
    ms, err = hide_app(True); print(f"hide app: {ms:.0f} ms {err} -> visible={app_hidden()}")
    time.sleep(0.5)
    for i in range(3):
        a.call("Page.navigate", url=anim(f"A{i+2}", 100 + i)); time.sleep(1.0)
        s, h = shot(a); n = frames(a); r = raf(a)
        print(f"A nav{i+1}: vis={a.evaluate('document.visibilityState')} shot={s} frames/2s={n} rAF/s={r}")
    sb, hb = shot(b); nb = frames(b); print(f"B (untouched, hidden): shot={sb} frames/2s={nb} rAF/s={raf(b)}")
    # take-over A: unhide app + activate A's target
    t0 = time.time(); ms, err = hide_app(False)
    a.call("Page.bringToFront"); print(f"take-over: unhide {ms:.0f} ms {err}; total {(time.time()-t0)*1000:.0f} ms; visible={app_hidden()}")
    time.sleep(0.5)
    print(f"during take-over, B: shot={shot(b)[0]} frames/2s={frames(b)}")
    # hand back
    t0 = time.time(); ms, err = hide_app(True); print(f"hand-back: hide {ms:.0f} ms {err}; visible={app_hidden()}")
    time.sleep(0.5)
    a.call("Page.navigate", url=anim("A9", 50)); time.sleep(1.0)
    print(f"A after hand-back + nav: shot={shot(a)[0]} frames/2s={frames(a)} rAF/s={raf(a)}")
    # 10 quick cycles
    fails = 0; lat = []
    for i in range(10):
        t0 = time.time(); hide_app(False); a.call("Page.bringToFront"); hide_app(True); lat.append((time.time() - t0) * 1000)
        a.call("Page.navigate", url=anim(f"C{i}", 60)); time.sleep(0.6)
        if not shot(a)[0].startswith("ok"): fails += 1
    lat.sort(); print(f"10 cycles: fails={fails} flip-pair p50={lat[5]:.0f} ms max={lat[-1]:.0f} ms")
finally:
    hide_app(False)
    kill(sess)
