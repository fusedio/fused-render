import sys, os, time, subprocess, json, statistics
SCR = "/private/tmp/claude-501/-Users-vasu-Documents-fused-dev-fused-render/acb7c266-9bd8-4dc6-a57d-56112293155a/scratchpad"
sys.path.insert(0, SCR + "/lib")
import cdp
URL = "data:text/html,<textarea id=t autofocus style='width:500px;height:200px'></textarea>"
S = """a.b,c-d/e;f'g[h]i Hello World! (x) {y} <z> ~!@#$%^&*_+=|:"?"""
OUT = SCR + "/results/D"


def osa_type(text):
    """real human typing via System Events into frontmost app"""
    esc = text.replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate', "-e", "delay 0.4",
                        "-e", f'tell application "System Events" to keystroke "{esc}"'],
                       capture_output=True, text=True)
    return r.returncode, r.stderr.strip()


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def val(page):
    return page.eval_on_selector("#t", "e=>e.value")
