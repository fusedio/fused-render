"""Standalone repro: minimized window + Emulation.setFocusEmulationEnabled. Run: python3 -I minimized_repro.py
Each variant gets a FRESH window (createTarget newWindow) in one headed Chrome. Prints launch argv and per-variant results.
Variants:
  V1 emu BEFORE minimize, same session, page loaded while VISIBLE        (main.py recipe)
  V2 emu AFTER  minimize, same session, page loaded while VISIBLE        (focusemu.py recipe)
  V3 emu on a SEPARATE CDP session, shots on another session             (Playwright new_cdp_session-like)
  V4 like V1, then Page.navigate while minimized                          (expected: HANG)
  V5 page CREATED/loaded after the window is already minimized           (expected: HANG?)
  V0 control: minimized, no emu
"""
import sys, os, time, json, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import launch, kill, pages
from util import PAGE, png_stats, browser_ws, page_ws, win, S

sess = launch(f"{S}/profiles/A-repro", headless=False, url=None, extra=["--no-startup-window"], window=(900, 600))
argv = subprocess.run(["ps", "-o", "args=", "-p", str(sess["pid"])], capture_output=True, text=True).stdout.strip()
print("LAUNCH ARGV (cwd=scratchpad; profile path is relative):\n ", argv)
print("Chrome:", subprocess.run([argv.split(" --")[0], "--version"], capture_output=True, text=True).stdout.strip())


def measure(ws, label):
    vis = ws.evaluate("document.visibilityState")
    ws.evaluate("document.body.style.background='#22d';1")  # DOM change: a fresh shot must be BLUE
    ws.sock.settimeout(6); t0 = time.time()
    try:
        r = ws.call("Page.captureScreenshot", format="png"); st = png_stats(r["data"]); st["ms"] = round((time.time() - t0) * 1000)
        st["fresh"] = st["blue"] > 0.5
    except Exception as e:
        st = {"ERR": repr(e)}
        print(f"  {label}: vis={vis} shot={st}  (skipping screencast; session now unusable)"); return
    n = 0
    ws.call("Page.startScreencast", format="jpeg", quality=50, maxWidth=640, maxHeight=400)
    ws.sock.settimeout(0.3); t0 = time.time()
    while time.time() - t0 < 3:
        try: m = ws.recv()
        except Exception: continue
        if m.get("method") == "Page.screencastFrame":
            n += 1; ws.notify("Page.screencastFrameAck", sessionId=m["params"]["sessionId"])
    ws.sock.settimeout(30); ws.call("Page.stopScreencast")
    print(f"  {label}: vis={vis} shot={st} screencast_frames_3s={n}")


def new_bot(bws, url=PAGE):
    tid = bws.call("Target.createTarget", url=url, newWindow=True, background=True)["targetId"]
    wid = win(bws, tid)["windowId"]
    ws = page_ws(sess["port"], tid); ws.call("Page.enable"); ws.call("Runtime.enable")
    time.sleep(1.0)  # let it paint while visible
    return tid, wid, ws


def minimize(bws, wid):
    bws.call("Browser.setWindowBounds", windowId=wid, bounds={"windowState": "minimized"}); time.sleep(1.2)
    return bws.call("Browser.getWindowBounds", windowId=wid)["bounds"]["windowState"]


try:
    bws = browser_ws(sess["port"])
    print("V0 control: minimized, NO focus emulation")
    tid, wid, ws = new_bot(bws); print("  state:", minimize(bws, wid)); measure(ws, "V0")

    print("V1 emu BEFORE minimize, same session (Page.enable on)")
    tid, wid, ws = new_bot(bws); ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
    print("  state:", minimize(bws, wid)); measure(ws, "V1")

    print("V2 emu AFTER minimize, same session")
    tid, wid, ws = new_bot(bws); print("  state:", minimize(bws, wid))
    ws.call("Emulation.setFocusEmulationEnabled", enabled=True); time.sleep(0.5); measure(ws, "V2")

    print("V3 emu on SEPARATE session A, shot/screencast on session B")
    tid, wid, wsB = new_bot(bws); wsA = page_ws(sess["port"], tid)
    wsA.call("Emulation.setFocusEmulationEnabled", enabled=True)
    print("  state:", minimize(bws, wid)); measure(wsB, "V3 (shots on B, emu on A)")
    measure(wsA, "V3 (shots on A, the emu session)")

    print("V4 = V1 then Page.navigate while minimized")
    tid, wid, ws = new_bot(bws); ws.call("Emulation.setFocusEmulationEnabled", enabled=True); minimize(bws, wid)
    ws.call("Page.navigate", url=PAGE); time.sleep(1.5); measure(ws, "V4")

    print("V5 window created then minimized BEFORE the page loads (about:blank -> navigate)")
    tid = bws.call("Target.createTarget", url="about:blank", newWindow=True, background=True)["targetId"]
    wid = win(bws, tid)["windowId"]; minimize(bws, wid)
    ws = page_ws(sess["port"], tid); ws.call("Page.enable"); ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
    ws.call("Page.navigate", url=PAGE); time.sleep(1.5); measure(ws, "V5")
finally:
    kill(sess)
