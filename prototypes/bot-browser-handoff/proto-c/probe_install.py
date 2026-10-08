"""Probe every extension install path. Signal of success = extension SW dials our relay (hello)."""
import json, os, shutil, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); S = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(S, "lib")]
import cdp
from relay import Relay

EXT = os.path.join(HERE, "ext")
BRANDED = cdp.CHROME
CFT = "/Users/vasu/.cache/puppeteer/chrome/mac_arm-152.0.7977.54/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
COMMON = ["--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-background-networking", "--window-size=900,700"]
only = sys.argv[1:]
relay = Relay()
results = {}


def profile(n):
    p = os.path.join(S, "profiles", f"C-probe-{n}"); shutil.rmtree(p, ignore_errors=True); os.makedirs(p); return p


def spawn(binary, prof, extra, port=True, pipe=False):
    args = [binary, f"--user-data-dir={prof}", *COMMON, *extra]
    pt = None
    if port:
        pt = cdp.free_port(); args.insert(1, f"--remote-debugging-port={pt}")
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open(prof + ".stderr", "w"), start_new_session=True)
    pipes = None
    if pipe:
        r_in, w_in = os.pipe()   # we write -> chrome fd3
        r_out, w_out = os.pipe() # chrome fd4 -> we read
        def pre():
            os.dup2(r_in, 3); os.dup2(w_out, 4)
        kw.update(close_fds=False, preexec_fn=pre)
        args.insert(1, "--remote-debugging-pipe")
        pipes = (w_in, r_out, r_in, w_out)
    proc = subprocess.Popen(args, **kw)
    if pipe:
        os.close(pipes[2]); os.close(pipes[3])
    if pt:
        for _ in range(100):
            try: cdp.http(pt, "/json/version"); break
            except Exception: time.sleep(0.1)
    return {"pid": proc.pid, "proc": proc, "port": pt, "pipes": pipes}


class Pipe:
    def __init__(self, w, r): self.w, self.r, self.id, self.buf = w, r, 0, b""
    def call(self, method, **params):
        self.id += 1; os.write(self.w, json.dumps({"id": self.id, "method": method, "params": params}).encode() + b"\0")
        t0 = time.time()
        while time.time() - t0 < 15:
            while b"\0" in self.buf:
                msg, self.buf = self.buf.split(b"\0", 1); m = json.loads(msg)
                if m.get("id") == self.id: return m
            self.buf += os.read(self.r, 1 << 20)
        return {"error": "timeout"}


def ext_targets(port):
    if not port: return None
    try: return [t["url"] for t in cdp.http(port, "/json/list") if t["url"].startswith("chrome-extension://")]
    except Exception as e: return str(e)


def check(name, sess, wait=12, extra_info=None):
    relay.connected.clear(); relay.hello = None
    ok = relay.wait_connected(wait)
    res = {"hello": bool(ok), "ext_id": relay.hello and relay.hello.get("ext"), "ext_targets": ext_targets(sess["port"])}
    if extra_info: res.update(extra_info)
    try: res["version"] = cdp.http(sess["port"], "/json/version")["Browser"] if sess["port"] else None
    except Exception: pass
    results[name] = res; print(name, json.dumps(res), flush=True)
    cdp.kill(sess); time.sleep(1)


def run(name):
    return not only or name in only

if run("a_branded_load_extension"):
    s = spawn(BRANDED, profile("a"), [f"--load-extension={EXT}"]); check("a_branded_load_extension", s)

if run("d_branded_disable_except"):
    s = spawn(BRANDED, profile("d"), [f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"]); check("d_branded_disable_except", s)

if run("b_branded_pipe_loadUnpacked"):
    s = spawn(BRANDED, profile("b"), ["--enable-unsafe-extension-debugging"], port=True, pipe=True)
    p = Pipe(s["pipes"][0], s["pipes"][1]); time.sleep(1.5)
    r = p.call("Extensions.loadUnpacked", path=EXT)
    check("b_branded_pipe_loadUnpacked", s, extra_info={"loadUnpacked": r})

if run("b2_branded_port_loadUnpacked"):
    s = spawn(BRANDED, profile("b2"), ["--enable-unsafe-extension-debugging"])
    ws = cdp.WS(cdp.http(s["port"], "/json/version")["webSocketDebuggerUrl"])
    try: r = ws.call("Extensions.loadUnpacked", path=EXT)
    except Exception as e: r = str(e)
    check("b2_branded_port_loadUnpacked", s, extra_info={"loadUnpacked": r})

if run("b3_branded_pipe_noflag"):
    s = spawn(BRANDED, profile("b3"), [], port=True, pipe=True)
    p = Pipe(s["pipes"][0], s["pipes"][1]); time.sleep(1.5)
    r = p.call("Extensions.loadUnpacked", path=EXT)
    check("b3_branded_pipe_noflag", s, wait=6, extra_info={"loadUnpacked": r})

if run("c_cft_load_extension"):
    s = spawn(CFT, profile("c"), [f"--load-extension={EXT}"]); check("c_cft_load_extension", s)

if run("b4_branded_port_noflag"):
    s = spawn(BRANDED, profile("b4"), [])
    ws = cdp.WS(cdp.http(s["port"], "/json/version")["webSocketDebuggerUrl"])
    try: r = ws.call("Extensions.loadUnpacked", path=EXT)
    except Exception as e: r = str(e)
    check("b4_branded_port_noflag", s, wait=6, extra_info={"loadUnpacked": r})

if run("b5_persist_after_relaunch"):
    prof = profile("b5")
    s = spawn(BRANDED, prof, ["--enable-unsafe-extension-debugging"])
    ws = cdp.WS(cdp.http(s["port"], "/json/version")["webSocketDebuggerUrl"])
    r = ws.call("Extensions.loadUnpacked", path=EXT); ws.close()
    check("b5_first_launch", s, extra_info={"loadUnpacked": r})
    s = spawn(BRANDED, prof, [])  # plain relaunch: no flag, still a port for observing
    check("b5_relaunch_plain_with_port", s)
    s = spawn(BRANDED, prof, [], port=False)  # truly plain: what a user double-clicking would get
    check("b5_relaunch_no_port", s)

os.makedirs(os.path.join(S, "results", "C"), exist_ok=True)
prev = os.path.join(S, "results", "C", "probe_install.json")
if os.path.exists(prev): results = {**json.load(open(prev)), **results}
json.dump(results, open(os.path.join(S, "results", "C", "probe_install.json"), "w"), indent=1)
relay.close()
