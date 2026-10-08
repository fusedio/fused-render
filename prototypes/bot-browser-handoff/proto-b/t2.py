import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from env import *
R = f"{S}/results/B"
e = Env("B2")
try:
    e.goto(TA); e.ws.evaluate("document.querySelector('#t').focus()")
    pid0 = e.bot["pid"]
    e.bot_type("bot:"); print("bot typed:", repr(val(e.ws)))
    e.open_liveview(); print("liveview frames:", e.dws.evaluate("__stats.frames"))
    print("take_over flip s:", e.take_over())
    e.h_click_canvas(0.2, 0.1)   # click the textarea through the forwarded mouse path
    time.sleep(.3)
    s = "human.text's,here"
    e.h_type(s); time.sleep(.3)
    v = val(e.ws); print("after human:", repr(v), "PASS" if v == "bot:" + s else "FAIL")
    # full punctuation set from the human path
    e.ws.evaluate("document.querySelector('#t').value=''")
    full = """a.b,c-d/e;f'g[h]i Hello World! (x) {y} <z> ~!@#$%^&*_+=|:"?"""
    e.h_type(full); e.h_key("Enter"); time.sleep(.3)
    v = val(e.ws); print("full punct:", v == full + "\n", repr(v))
    e.h_shot(f"{R}/t2_liveview.png")
    print("hand back:", e.hand_back()); e.bot_type("-bot"); print(repr(val(e.ws)), e.tid, [(p["id"], p["url"][:25]) for p in pages(e.bot["port"])], pid0 == e.bot["pid"])
    print(e.dws.evaluate("JSON.stringify(__stats.errors)"))
finally: e.close()
