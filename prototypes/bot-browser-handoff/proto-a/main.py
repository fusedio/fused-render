"""Approach A main run: T1..T6 (+ setup for T8). Hidden state = minimized window + Emulation.setFocusEmulationEnabled."""
import sys, os, time, json, subprocess, statistics, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import launch, kill, pages, http
from util import *

fh = open(f"{OUT}/main.log", "w")
R = {}
SESS = []
CAST = []
STR = "a.b,c-d/e;f'g[h]i Hello World! (x) {y} <z> ~!@#$%^&*_+=|:\"?"
HUMAN = "human.text's,here"


def save():
    json.dump(R, open(f"{OUT}/main.json", "w"), indent=1)


class Bot:
    def __init__(self, sess, bws, name, url=PAGE):
        self.sess, self.bws, self.name = sess, bws, name
        self.tid = bws.call("Target.createTarget", url=url, newWindow=True, background=True)["targetId"]
        self.wid = win(bws, self.tid)["windowId"]
        bws.call("Browser.setWindowBounds", windowId=self.wid, bounds={"left": 120, "top": 120, "width": 900, "height": 600})
        self.attach()
        self.hand_back()

    def attach(self):
        self.ws = page_ws(self.sess["port"], self.tid); self.ws.call("Page.enable"); self.ws.call("Runtime.enable")
        self.ws.call("Emulation.setFocusEmulationEnabled", enabled=True)

    def val(self): return self.ws.evaluate("document.getElementById('t').value")
    def focus_end(self): self.ws.evaluate("var t=document.getElementById('t');t.focus();t.setSelectionRange(t.value.length,t.value.length);1")

    HOME = {"left": 120, "top": 120, "width": 900, "height": 600}

    def take_over(self, activate=True):
        t0 = time.time()
        self.bws.call("Browser.setWindowBounds", windowId=self.wid, bounds=self.HOME)
        self.ws.call("Page.bringToFront")
        t_cdp = time.time()
        if activate: activate_pid(self.sess["pid"])
        return {"cdp_ms": round((t_cdp - t0) * 1000), "with_activate_ms": round((time.time() - t0) * 1000)}

    def hand_back(self, restore_app_pid=None):
        t0 = time.time()
        # hidden state = pushed off the bottom-right edge; macOS clamps so a ~40px corner stays on screen
        self.bws.call("Browser.setWindowBounds", windowId=self.wid, bounds={"left": 99999, "top": 99999})
        t_cdp = time.time()
        self.hidden_bounds = self.bws.call("Browser.getWindowBounds", windowId=self.wid)["bounds"]
        if restore_app_pid: activate_pid(restore_app_pid)
        return {"cdp_ms": round((t_cdp - t0) * 1000), "with_reactivate_ms": round((time.time() - t0) * 1000)}


def osa_type(text):
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    return osa(f'tell application "System Events" to keystroke "{esc}"')


def run():
    prev = frontmost(); R["frontmost_before"] = prev
    prev_pid = int(prev.split(",")[-1]) if prev and "," in prev else None
    sess = launch(f"{S}/profiles/A-main", headless=False, url=None, extra=["--no-startup-window"], window=(900, 600))
    SESS.append(sess); R["pid"] = sess["pid"]; R["port"] = sess["port"]
    log(fh, "launched", sess["pid"], sess["port"], "pages at start:", len(pages(sess["port"])))
    bws = browser_ws(sess["port"])
    bws.call("Target.setDiscoverTargets", discover=True)
    A = Bot(sess, bws, "A")
    time.sleep(1.0)
    R["frontmost_after_create+minimize"] = frontmost(); log(fh, "frontmost after create+minimize:", R["frontmost_after_create+minimize"], "(before:", prev, ")")

    # ---- T1 (bot types while window MINIMIZED)
    try:
        A.focus_end(); type_text(A.ws, STR); press(A.ws, "Enter")
        v1 = A.val(); exp1 = STR + "\n"
        for _ in range(3): press(A.ws, "ArrowLeft")
        press(A.ws, "Backspace"); v2 = A.val()
        pos = len(exp1) - 3; exp2 = exp1[:pos - 1] + exp1[pos:]
        select_all(A.ws); type_text(A.ws, "q"); v3 = A.val()
        R["T1"] = {"state": bws.call("Browser.getWindowBounds", windowId=A.wid)["bounds"]["windowState"], "typed": v1, "typed_ok": v1 == exp1,
                   "after_left3_bs": v2, "left_bs_ok": v2 == exp2, "after_selectall_q": v3, "selall_ok": v3 == "q",
                   "PASS": v1 == exp1 and v2 == exp2 and v3 == "q"}
    except Exception as e:
        R["T1"] = {"error": traceback.format_exc()}
    log(fh, "T1", json.dumps(R["T1"])); save()

    # ---- infobar check + T2 take-over: human types via osascript
    A.ws.evaluate("document.getElementById('t').value='';1"); A.focus_end()
    lat = A.take_over(); time.sleep(0.8)
    R["T2_takeover_latency"] = lat
    R["T2_frontmost_after_takeover"] = frontmost()
    rc = osa_type(HUMAN); time.sleep(0.5)
    v_osa = A.val()
    human = None
    if v_osa != HUMAN:  # TCC blocked keystrokes -> CDP-as-human on a SEPARATE CDP session to the same target
        human = page_ws(sess["port"], A.tid)
        type_text(human, HUMAN); time.sleep(0.2)
    v = A.val()
    subprocess.run(["screencapture", "-x", f"{OUT}/t2_screen.png"])
    A.ws.call("Emulation.setFocusEmulationEnabled", enabled=False)  # does real focus hold without emulation?
    R["T2"] = {"osascript": rc, "value_via_osascript": v_osa, "fallback_second_cdp_session": human is not None, "value": v, "PASS": v == HUMAN, "frontmost": R["T2_frontmost_after_takeover"], "latency": lat,
               "hasFocus_real": A.ws.evaluate("document.hasFocus()")}
    A.ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
    log(fh, "T2", json.dumps(R["T2"])); save()

    # ---- T3 hand-back
    tid_before, pid_before = A.tid, sess["pid"]
    hb = A.hand_back(restore_app_pid=prev_pid); time.sleep(0.5)
    subprocess.run(["screencapture", "-x", f"{OUT}/t3_hidden_screen.png"])
    A.focus_end(); type_text(A.ws, "-bot"); v = A.val()
    alive = [p["id"] for p in pages(sess["port"])]
    R["T3"] = {"hand_back_latency": hb, "hidden_bounds": A.hidden_bounds, "value": v, "same_tid": A.tid in alive and A.tid == tid_before, "pid_same": sess["proc"].poll() is None and sess["pid"] == pid_before,
               "frontmost_after": frontmost(), "PASS": v == HUMAN + "-bot" and A.tid in alive and sess["proc"].poll() is None}
    log(fh, "T3", json.dumps(R["T3"])); save()

    # ---- T4 churn 20 cycles (CDP flip + osascript activate on take-over, re-activate previous app on hand-back)
    to, hb_l, fails = [], [], []
    # screencast that stays open across flips: count frames while flipping
    A.ws.call("Page.startScreencast", format="jpeg", quality=40, maxWidth=480, maxHeight=300)
    for i in range(20):
        try:
            a = A.take_over(); to.append(a)
            vis = A.ws.evaluate("document.visibilityState")
            A.ws.evaluate(f"document.getElementById('t').value='c{i}';1")
            b = A.hand_back(restore_app_pid=prev_pid); hb_l.append(b)
            A.focus_end(); type_text(A.ws, "."); ok = A.val() == f"c{i}." and vis == "visible"
            # drain + ack screencast frames that piled up in ws.events during calls
            fr = [e for e in A.ws.events if e["method"] == "Page.screencastFrame"]
            A.ws.events = [e for e in A.ws.events if e["method"] != "Page.screencastFrame"]
            for e in fr: A.ws.notify("Page.screencastFrameAck", sessionId=e["params"]["sessionId"])
            CAST.append(len(fr))
            if not ok: fails.append((i, A.val(), vis))
        except Exception as e:
            fails.append((i, repr(e), "chrome returncode", sess["proc"].poll()))
            if sess["proc"].poll() is not None: break
    def pct(xs, k):
        xs = sorted(xs); return xs[min(len(xs) - 1, int(round(k * (len(xs) - 1))))] if xs else None
    A.ws.call("Page.stopScreencast")
    R["T4"] = {"cycles": 20, "failures": fails, "screencast_frames_per_cycle_across_flips": CAST,
               "takeover_cdp_ms_p50/p95": [pct([x["cdp_ms"] for x in to], .5), pct([x["cdp_ms"] for x in to], .95)],
               "takeover_with_activate_ms_p50/p95": [pct([x["with_activate_ms"] for x in to], .5), pct([x["with_activate_ms"] for x in to], .95)],

               "handback_cdp_ms_p50/p95": [pct([x["cdp_ms"] for x in hb_l], .5), pct([x["cdp_ms"] for x in hb_l], .95)],
               "handback_with_reactivate_ms_p50/p95": [pct([x["with_reactivate_ms"] for x in hb_l], .5), pct([x["with_reactivate_ms"] for x in hb_l], .95)],
               "pid_same": sess["proc"].poll() is None, "PASS": not fails}
    log(fh, "T4", json.dumps(R["T4"])); save()

    # ---- T5 shared profile: B works while A is taken over
    B = Bot(sess, bws, "B")
    A.take_over(); time.sleep(0.3)
    b_res = []
    for i in range(5):
        col = ["%2322d", "%232a2", "%23d2d", "%23222", "%23d82"][i]
        B.ws.call("Page.navigate", url=PAGE.replace("%23d22", col)); time.sleep(0.6)
        B.ws.call("Emulation.setFocusEmulationEnabled", enabled=True)
        st = shot(B.ws, save=f"t5_B_{i}" if i == 0 else None)
        B.focus_end(); type_text(B.ws, f"b{i}.'x"); v = B.val()
        b_res.append({"shot_nonwhite": st["nonwhite"], "value": v, "ok": st["nonwhite"] > 0.5 and v == f"b{i}.'x"})
    R["T5"] = {"B": b_res, "B_window_state": bws.call("Browser.getWindowBounds", windowId=B.wid)["bounds"]["windowState"],
               "A_window_state": bws.call("Browser.getWindowBounds", windowId=A.wid)["bounds"]["windowState"],
               "frontmost": frontmost(), "B_cast": screencast(B.ws, 2)}
    subprocess.run(["screencapture", "-x", f"{OUT}/t5_screen.png"])
    A.hand_back(restore_app_pid=prev_pid)
    A.focus_end(); type_text(A.ws, "Z")
    R["T5"]["A_after_handback"] = A.val()
    R["T5"]["PASS"] = all(x["ok"] for x in b_res)
    log(fh, "T5", json.dumps(R["T5"])); save()

    # ---- T6 user closes the window during take-over
    A.take_over(); time.sleep(0.3)
    bws.events.clear()
    bws.call("Target.closeTarget", targetId=A.tid)
    t0 = time.time(); destroyed = None
    try:
        destroyed = bws.wait_event("Target.targetDestroyed", timeout=3)
    except Exception as e:
        destroyed = repr(e)
    det_ms = round((time.time() - t0) * 1000)
    try:
        A.ws.evaluate("1"); page_ws_state = "still alive?!"
    except Exception as e:
        page_ws_state = f"page ws error: {e!r}"
    alive_before = sess["proc"].poll() is None
    A2 = Bot(sess, bws, "A2"); A2.focus_end(); type_text(A2.ws, "again"); v = A2.val()
    # real cmd-W path: take over A2 and send cmd-w via System Events
    A2.take_over(); time.sleep(0.5); bws.events.clear()
    rc = osa('tell application "System Events" to keystroke "w" using command down')
    try:
        d2 = bws.wait_event("Target.targetDestroyed", timeout=3)
    except Exception as e:
        d2 = repr(e)
    # close every window: does Chrome stay alive (macOS app semantics)?
    for t in pages(sess["port"]):
        bws.call("Target.closeTarget", targetId=t["id"])
    time.sleep(1.0)
    alive_zero = sess["proc"].poll() is None
    try:
        http(sess["port"], "/json/version"); cdp_up = True
    except Exception:
        cdp_up = False
    A3 = Bot(sess, bws, "A3"); A3.focus_end(); type_text(A3.ws, "z.w"); v3 = A3.val()
    R["T6"] = {"targetDestroyed": destroyed, "detect_ms": det_ms, "old_page_ws": page_ws_state, "chrome_alive": alive_before,
               "new_tab_value": v, "cmdW_osascript": rc, "cmdW_destroyed": d2,
               "after_closing_ALL_windows_chrome_alive": alive_zero, "cdp_up": cdp_up, "new_bot_after_zero_windows": v3,
               "pid_same": sess["proc"].poll() is None, "PASS": v == "again" and v3 == "z.w" and sess["proc"].poll() is None}
    log(fh, "T6", json.dumps(R["T6"])); save()

    # ---- T7 recap on the live bot (minimized + focus emulation)
    A3.ws.evaluate("document.body.style.background='#22d';1")
    R["T7"] = {"state": bws.call("Browser.getWindowBounds", windowId=A3.wid)["bounds"]["windowState"], "vis": A3.ws.evaluate("document.visibilityState"),
               "shot": shot(A3.ws, save="t7_main_minimized_focusemu"), "cast": screencast(A3.ws, 3)}
    R["T7"]["PASS"] = R["T7"]["shot"]["blue"] > 0.5 and R["T7"]["cast"]["frames"] > 0
    log(fh, "T7", json.dumps(R["T7"])); save()

    # ---- leave session file for T8
    json.dump({"port": sess["port"], "pid": sess["pid"], "bot_tid": A3.tid, "wid": A3.wid}, open(f"{OUT}/session.json", "w"))
    log(fh, "session file written; chrome LEFT RUNNING for T8")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        log(fh, "FATAL", traceback.format_exc()); save()
        log(fh, "chrome returncode at fatal:", SESS[0]["proc"].poll() if SESS else None)
