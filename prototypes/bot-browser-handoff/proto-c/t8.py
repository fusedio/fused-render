"""T8: orchestrator dies (SIGKILL), fresh orchestrator re-binds the relay port; extension must redial.
parent: python3 -I t8.py        child: python3 -I t8.py child <tag> [idle_secs]"""
import json, os, signal, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *

def child(tag, idle=0):
    from relay import Relay
    t0 = time.time(); r = Relay(); ok = r.wait_connected(90); t_hello = time.time() - t0
    out = {"tag": tag, "hello": ok, "hello_s": round(t_hello, 2)}
    if ok:
        ss = json.load(open(SESSION)); tab = ss["bot_tab"]
        out["tab_in_hello"] = any(t["id"] == tab for t in r.hello["tabs"]); out["ext_reports_attached"] = r.hello["attached"]
        try: r.evaluate(tab, "1"); out["still_attached"] = True
        except Exception as e:
            out["still_attached"] = str(e); r.call("attach", tabId=tab)
        out["target_same"] = next((t["id"] for t in r.call("targets") if t.get("tabId") == tab), None) == ss["bot_target"]
        r.evaluate(tab, "(()=>{const t=document.getElementById('t');t.focus();t.selectionStart=t.selectionEnd=t.value.length})()")
        type_text(r, tab, f"+{tag}"); out["val"] = r.evaluate(tab, "document.getElementById('t').value")
        out["chrome_pid_same"] = subprocess.run(["kill", "-0", str(ss["pid"])]).returncode == 0
        if idle:
            time.sleep(idle)
            out["idle_bridge_down"] = [e for e in r.events if e.get("type") == "bridge_down"]
            try: out["after_idle_ping"] = r.call("ping", timeout=5)
            except Exception as e: out["after_idle_ping"] = repr(e)
            out["reconnects_during_idle"] = r.connects - 1
    print("RESULT " + json.dumps(out), flush=True)
    time.sleep(3600)  # stay alive until parent SIGKILLs us

def run_child(tag, idle=0, wait_s=200):
    p = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "child", tag, str(idle)], stdout=subprocess.PIPE, text=True, start_new_session=True)
    t0 = time.time()
    for line in p.stdout:
        if line.startswith("RESULT "):
            res = json.loads(line[7:]); break
    p.send_signal(signal.SIGKILL); p.wait()   # orchestrator dies hard
    return res

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0); sys.exit()
    R = {}
    R["first"] = run_child("o1")              # first orchestrator after run_t56 exited normally
    time.sleep(2)
    R["after_kill_2s"] = run_child("o2")       # SIGKILLed o1, new one 2 s later
    time.sleep(70)
    R["after_kill_70s"] = run_child("o3", idle=70)  # SW likely idle-terminated; then 70 s idle with no traffic
    R["pass"] = all(R[k]["hello"] and R[k]["val"].endswith("+" + R[k]["tag"]) and R[k]["chrome_pid_same"] for k in ("first", "after_kill_2s", "after_kill_70s"))
    json.dump(R, open(os.path.join(OUT, "t8.json"), "w"), indent=1)
    print(json.dumps(R, indent=1))
