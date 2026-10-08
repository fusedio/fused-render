"""T4 rerun in the production layout: no minimize; hand-back = attach + re-enable focus emulation + eval."""
import json, os, statistics, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
from relay import Relay
kill_c(); time.sleep(1)
relay = Relay(); BASE = serve_page()
sess, ext_id, ok, dt = launch_bridge("C-t4b", extra=["--silent-debugger-extension-api"], relay=relay)
w = relay.call("createWindow", url="about:blank", focused=False, width=800, height=600, left=60, top=60); A = w["tabId"]
relay.call("attach", tabId=A); relay.cdp(A, "Page.navigate", url=BASE); time.sleep(0.6)
take, back, att, fails = [], [], [], []
for i in range(20):
    try:
        t0 = time.time(); relay.call("detach", tabId=A); relay.call("activate", tabId=A); take.append(time.time() - t0)
        t0 = time.time(); relay.call("attach", tabId=A); att.append(time.time() - t0)
        relay.cdp(A, "Emulation.setFocusEmulationEnabled", enabled=True); assert relay.evaluate(A, "1+1") == 2; back.append(time.time() - t0)
    except Exception as e: fails.append((i, repr(e)))
q = lambda xs: {"p50_ms": round(statistics.median(xs) * 1000, 1), "p95_ms": round(sorted(xs)[int(len(xs) * .95) - 1] * 1000, 1)}
R = {"fails": fails, "take_over": q(take), "attach_only": q(att), "hand_back_total": q(back), "pid_alive": sess["proc"].poll() is None}
json.dump(R, open(os.path.join(OUT, "t4b.json"), "w"), indent=1); print(json.dumps(R))
cdp.kill(sess); relay.close()
