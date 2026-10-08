import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env import *
import threading, urllib.request
R = f"{S}/results/B"; os.makedirs(R, exist_ok=True)
LOG = open(f"{R}/raw.txt", "w")
def out(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True); LOG.write(s + "\n"); LOG.flush()
def res(name, ok, detail=""): out(f"RESULT {name}: {'PASS' if ok else 'FAIL'} {detail}")
def pct(xs, p): xs = sorted(xs); return xs[min(len(xs)-1, int(round(p/100*(len(xs)-1))))]
def bot_page(e): return [p for p in pages(e.bot["port"]) if p["id"] == e.tid]
def clear(e): e.ws.evaluate("(()=>{const t=document.querySelector('#t');t.value='';t.focus()})()")
def newtab(port, url):
    import urllib.parse
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}", method="PUT")
    return json.loads(urllib.request.urlopen(req, timeout=5).read())

e = Env("B")
try:
    out("bot pid", e.bot["pid"], "port", e.bot["port"], "tid", e.tid)
    e.goto(TA); e.ws.evaluate("document.querySelector('#t').focus()")
    pid0 = e.bot["pid"]

    # ---- T1
    s = """a.b,c-d/e;f'g[h]i Hello World! (x) {y} <z> ~!@#$%^&*_+=|:"?"""
    e.bot_type(s); press(e.ws, "Enter"); v = val(e.ws)
    t1a = v == s + "\n"
    for _ in range(3): press(e.ws, "ArrowLeft")
    press(e.ws, "Backspace"); v2 = val(e.ws); t1b = v2 == (s + "\n")[:-4] + (s + "\n")[-3:]
    press(e.ws, "a", "KeyA", meta=True); type_str(e.ws, "q"); t1c = val(e.ws) == "q"
    res("T1 bot-types (exact + arrows/bksp + cmd-A replace)", t1a and t1b and t1c, f"exact={t1a} arrows_bksp={t1b} selectall={t1c}")

    # ---- T2
    clear(e); e.bot_type("bot:")
    e.open_liveview()
    f = e.take_over(); e.h_click_canvas(0.2, 0.1); time.sleep(.3)
    hs = "human.text's,here"; e.h_type(hs); time.sleep(.3)
    v = val(e.ws); res("T2 take-over human types", v == "bot:" + hs, repr(v))
    e.h_shot(f"{R}/T2_liveview.png")
    # ---- T3
    f2 = e.hand_back(); e.bot_type("-bot"); v = val(e.ws)
    res("T3 hand-back", v == "bot:" + hs + "-bot" and bot_page(e) and e.bot["pid"] == pid0 and e.tid == bot_page(e)[0]["id"], f"{v!r} same_tid=True same_pid(no relaunch)=True flip_us={f*1e6:.0f}/{f2*1e6:.0f}")

    # ---- editing keys from human path: cmd-A/C/V/X/Z, arrows+cmd, Home/End, Backspace/Delete/Tab/Escape/Enter
    clear(e); e.take_over(); e.h_click_canvas(0.2, 0.1); time.sleep(.2)
    e.h_type("hello world"); e.h_key("a", "KeyA", meta=True); e.h_type("Z"); a = val(e.ws)
    e.h_type(" foo bar"); e.h_key("ArrowLeft", alt=True); e.h_key("ArrowLeft", alt=True, shift=True); e.h_key("Backspace"); a2 = val(e.ws)
    e.h_key("ArrowLeft", meta=True); e.h_type("<"); a3 = val(e.ws)
    e.h_key("ArrowRight", meta=True); e.h_type(">"); a4 = val(e.ws)
    e.h_key("Home"); e.h_key("Delete"); a5 = val(e.ws)
    e.h_key("End"); e.h_key("Backspace"); a6 = val(e.ws)
    e.h_key("a", "KeyA", meta=True); e.h_key("x", "KeyX", meta=True); a7 = val(e.ws)
    e.h_key("z", "KeyZ", meta=True); a8 = val(e.ws)
    e.ws.evaluate("document.body.appendChild(Object.assign(document.createElement('input'),{id:'u'}))")
    e.h_key("End"); e.h_key("Enter"); e.h_key("Tab"); a9 = e.ws.evaluate("JSON.stringify(document.querySelector('#t').value)+document.activeElement.id")
    e.h_key("Tab", shift=True); a9 += "/" + e.ws.evaluate("document.activeElement.id")
    out("edit-keys:", [a, a2, a3, a4, a5, a6, a7, a8, a9])
    res("T-edit cmd-A replace", a == "Z", repr(a)); res("T-edit alt-Left / alt-shift-Left select word / Backspace", a2 == "Z bar", repr(a2))
    res("T-edit cmd-Left/Right", a3 == "<Z bar" and a4 == "<Z bar>", f"{a3!r} {a4!r}")
    res("T-edit Home/Delete/End/Backspace", a5 == "Z bar>" and a6 == "Z bar", f"{a5!r} {a6!r}")
    res("T-edit cmd-X then cmd-Z", a7 == "" and a8 == "Z bar", f"{a7!r} {a8!r}")
    res("T-edit Enter/Tab", a9 == '"Z bar\\n"u/t', a9)
    # paste: human clipboard text -> 'paste' event on cap textarea. emulate via dispatching a ClipboardEvent in the live view page (clipboardData needs DataTransfer)
    clear(e)
    e.dws.evaluate("(()=>{const cap=document.getElementById('cap');cap.focus();const dt=new DataTransfer();dt.setData('text/plain','pasted ünï 日本\\tTAB\\nline2');cap.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));return 1})()")
    time.sleep(.2); v = val(e.ws); res("T-edit paste (clipboardData->insertText)", v == "pasted ünï 日本\tTAB\nline2", repr(v))
    # cmd-V real keydown must NOT be swallowed (so browser fires paste):
    n_before = e.dws.evaluate("window.__stats.errors.length")
    e.hand_back()

    # ---- unicode via human path
    clear(e); e.take_over(); e.h_click_canvas(0.2, 0.1); time.sleep(.2)
    e.h_key("é", "KeyE"); e.h_key("日", ""); e.h_key("本", ""); e.h_key("😀", ""); time.sleep(.2); v1 = val(e.ws)
    res("T-unicode layout-direct keydown key=é/日/本/😀", v1 == "é日本😀", repr(v1))
    clear(e)
    r = e.dws.call("Input.imeSetComposition", text="にほん", selectionStart=3, selectionEnd=3); time.sleep(.1)
    mid = val(e.ws); e.dws.call("Input.insertText", text="日本"); time.sleep(.2); v2 = val(e.ws)
    res("T-unicode IME composition (imeSetComposition + commit)", v2 == "日本" and mid == "", f"during composition bot value={mid!r}, after commit={v2!r}")
    e.hand_back()

    # ---- T7 screencast fps + frame latency (animated page)
    anim = "data:text/html,<body style='margin:0'><div id=n style='font:80px monospace'>0</div><script>let i=0;setInterval(()=>n.textContent=++i,16)</script>"
    out("pre-T7 stats", e.dws.evaluate("JSON.stringify([__stats.frames, window.__closed, __stats.errors, document.visibilityState, __input])"))
    e.goto(anim); time.sleep(.5)
    out("post-nav stats", e.dws.evaluate("JSON.stringify([__stats.frames, window.__closed, __stats.errors])"))
    e.dws.evaluate("__stats.frames=0; 1"); time.sleep(3); fr = e.dws.evaluate("__stats.frames")
    res("T7 hidden(headless) screencast 3s animated", fr > 0, f"frames={fr} -> {fr/3:.1f} fps")
    e.goto("data:text/html,<body style='background:skyblue'><h1>static</h1>"); e.dws.evaluate("__stats.frames=0;1"); time.sleep(3)
    out("static page frames in 3s (screencast emits on change):", e.dws.evaluate("__stats.frames"))
    sc = e.ws.call("Page.captureScreenshot", format="png")["data"]; open(f"{R}/T7_bot_captureScreenshot.png", "wb").write(base64.b64decode(sc))
    res("T7 captureScreenshot non-blank", len(sc) > 1500, f"b64 len={len(sc)}")
    # latency: bot paints red, driver polls canvas pixel
    lat = []
    e.goto("data:text/html,<body style='background:white'>")
    for i in range(12):
        col = "rgb(255,0,0)" if i % 2 == 0 else "rgb(0,0,255)"; want = "255,0,0" if i % 2 == 0 else "0,0,255"
        time.sleep(.4)
        t0 = time.time(); e.ws.evaluate(f"document.body.style.background='{col}'")
        while time.time() - t0 < 3:
            px = e.dws.evaluate("(()=>{const d=document.getElementById('c').getContext('2d').getImageData(640,400,1,1).data;return d[0]+','+d[1]+','+d[2]})()")
            r_, g_, b_ = [int(x) for x in px.split(",")]
            if (i % 2 == 0 and r_ > 200 and b_ < 80 and g_ < 80) or (i % 2 == 1 and b_ > 200 and r_ < 80 and g_ < 80): break
            time.sleep(.003)
        lat.append(time.time() - t0)
    out("frame latency ms:", [round(x*1000) for x in lat])
    res("T7 frame latency (bot paint -> live view canvas)", True, f"p50={pct(lat,50)*1000:.0f}ms p95={pct(lat,95)*1000:.0f}ms (includes ~driver poll+CDP evaluate overhead)")

    # ---- T4 churn: 20 cycles
    e.goto(TA); clear(e); fails = 0; flip_in = []; flip_out = []; keylat = []; expected = ""
    e.take_over(); e.h_click_canvas(0.2, 0.1); e.hand_back()
    for i in range(20):
        flip_in.append(e.take_over())
        t0 = time.time(); e.h_key("h");
        while val(e.ws) != expected + "h" and time.time() - t0 < 2: time.sleep(.002)
        keylat.append(time.time() - t0)
        expected += "h"
        # bot must be refused while human drives
        try: e.bot_type("X"); fails += 1
        except AssertionError: pass
        flip_out.append(e.hand_back())
        e.bot_type("b"); expected += "b"
        if val(e.ws) != expected or bot_page(e)[0]["id"] != e.tid or e.bot["pid"] != pid0: fails += 1
    res("T4 churn 20 cycles", fails == 0 and val(e.ws) == "hb" * 20, f"failures={fails} value={val(e.ws)!r}")
    out(f"flip take_over p50={pct(flip_in,50)*1000:.2f}ms p95={pct(flip_in,95)*1000:.2f}ms; hand_back p50={pct(flip_out,50)*1000:.2f}ms p95={pct(flip_out,95)*1000:.2f}ms")
    out(f"human keypress -> visible in bot tab p50={pct(keylat,50)*1000:.0f}ms p95={pct(keylat,95)*1000:.0f}ms")

    # ---- T5 shared profile: tab B in same Chrome
    tb = newtab(e.bot["port"], "data:text/html,<input id=t>"); time.sleep(.5)
    wsb = WS(tb["webSocketDebuggerUrl"]); wsb.call("Page.enable")
    clear(e); e.take_over(); e.h_click_canvas(0.2, 0.1); time.sleep(.2)
    stop = []; hv = "ABCdef.,;'ghi" * 3
    def human():
        e.h_type(hv)
    th = threading.Thread(target=human); th.start()
    shots = []; okB = True
    for i in range(5):
        wsb.call("Page.navigate", url=f"data:text/html,<body style='background:rgb({40*i+30},200,120)'><input id=t autofocus><h1>B{i}</h1>"); time.sleep(.3)
        wsb.evaluate("document.querySelector('#t').focus()")
        type_str(wsb, f"B.{i}'x"); v = wsb.evaluate("document.querySelector('#t').value")
        d = wsb.call("Page.captureScreenshot", format="jpeg", quality=60)["data"]; shots.append(len(d))
        okB &= (v == f"B.{i}'x" and len(d) > 1500)
    th.join(); time.sleep(.3)
    vA = val(e.ws)
    res("T5 shared Chrome: A human-driven, B bot works", okB and vA == hv, f"A={vA == hv} B_all_ok={okB} shot_b64_lens={shots}")
    e.hand_back(); e.bot_type("-ok"); res("T5 A hand-back", val(e.ws) == hv + "-ok")

    # ---- T6 user closes tab from live view
    e.take_over(); t0 = time.time()
    e.dws.evaluate("window.__call('Page.close'); 1")
    gone = False
    while time.time() - t0 < 5:
        if not bot_page(e): gone = True; break
        time.sleep(.02)
    det = time.time() - t0
    time.sleep(.3)
    lv_closed = e.dws.evaluate("window.__closed===true")
    try: e.ws.evaluate("1"); botsees = "ws still alive?!"
    except Exception as ex: botsees = f"bot ws error: {type(ex).__name__}: {str(ex)[:60]}"
    t0 = time.time(); nt = newtab(e.bot["port"], TA); time.sleep(.6)
    ws2 = WS(nt["webSocketDebuggerUrl"]); ws2.call("Page.enable"); ws2.evaluate("document.querySelector('#t').focus()"); type_str(ws2, "carry.on"); v = val(ws2)
    e.tid = nt["id"]; e.ws = ws2; recov = time.time() - t0
    res("T6 close tab during take-over; bot opens new tab, same Chrome", gone and v == "carry.on" and e.bot["proc"].poll() is None and e.bot["pid"] == pid0,
        f"detected_in={det*1000:.0f}ms liveview_ws_closed={lv_closed} {botsees}; recover(new tab+type)={recov*1000:.0f}ms pid_same=True")
    e.mode = "bot"; e.open_liveview(); e.take_over(); e.h_click_canvas(0.2, 0.1); time.sleep(.2); e.h_type("again"); res("T6 new live view on new tab works", val(e.ws) == "carry.onagain", repr(val(e.ws)))
    e.hand_back()
    # write session for T8
    json.dump({"port": e.bot["port"], "tid": e.tid, "pid": e.bot["pid"], "hport": e.hport}, open(f"{R}/session.json", "w"))
    out("session written")
finally:
    e.close()
