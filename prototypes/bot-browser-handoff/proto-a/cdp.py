"""Tiny stdlib CDP client for throwaway prototypes. Usage:
    from cdp import launch, WS, http, pages, kill, kill_ours
    sess = launch(profile_dir, headless=True)      -> {"port", "pid", "proc"}
    ws = WS(target["webSocketDebuggerUrl"]); ws.call("Runtime.evaluate", expression="1+1")
Profiles MUST live under the scratchpad (asserted) so kill_ours() never touches the user's Chrome.
"""
import base64, json, os, socket, struct, subprocess, time, urllib.request, secrets, threading, signal

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SCRATCH_TAG = "acb7c266-9bd8-4dc6-a57d-56112293155a"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def http(port, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode() or "null")


def launch(profile_dir, headless=True, extra=(), url="about:blank", window=(1280, 800), antibg=True):
    assert SCRATCH_TAG in profile_dir, "prototypes must use scratchpad profiles"
    os.makedirs(profile_dir, exist_ok=True)
    port = free_port()
    mode = ["--headless=new"] if headless else (["--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"] if antibg else [])
    # proto-a: pass the profile RELATIVE to the scratchpad cwd so the cmdline lacks the tag (siblings' kill_ours() SIGKILLed us)
    cwd = profile_dir.split("/profiles/")[0]; rel = os.path.relpath(profile_dir, cwd)
    args = [CHROME, *mode, f"--remote-debugging-port={port}", f"--user-data-dir={rel}",
            "--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-background-networking",
            f"--window-size={window[0]},{window[1]}", *extra, *([url] if url else [])]
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, cwd=cwd)
    for _ in range(150):
        try:
            http(port, "/json/version"); break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("chrome did not come up")
    return {"port": port, "pid": proc.pid, "proc": proc}


def pages(port):
    return [t for t in http(port, "/json/list") if t.get("type") == "page"]


def kill(sess):
    try:
        os.killpg(sess["pid"], signal.SIGTERM)
    except Exception:
        pass
    try:
        sess["proc"].wait(timeout=5)
    except Exception:
        try: os.killpg(sess["pid"], signal.SIGKILL)
        except Exception: pass


def kill_ours():
    """Kill only Chrome processes whose cmdline carries the scratchpad tag."""
    out = subprocess.run(["pgrep", "-f", SCRATCH_TAG], capture_output=True, text=True).stdout.split()
    for pid in out:
        if int(pid) != os.getpid():
            try: os.kill(int(pid), signal.SIGKILL)
            except Exception: pass


class WS:
    def __init__(self, url, timeout=30):
        import urllib.parse
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.id = 0; self.events = []; self.buf = b""; self.lock = threading.Lock()

    def _recv_exact(self, n):
        while len(self.buf) < n:
            c = self.sock.recv(65536)
            if not c: raise ConnectionError("ws closed")
            self.buf += c
        d, self.buf = self.buf[:n], self.buf[n:]; return d

    def send(self, text):
        data = text.encode(); hdr = bytearray([0x81])
        n = len(data)
        if n < 126: hdr.append(0x80 | n)
        elif n < 65536: hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else: hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = secrets.token_bytes(4)
        self.sock.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        while True:
            b0, b1 = self._recv_exact(2); op = b0 & 0xF; n = b1 & 0x7F
            if n == 126: n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127: n = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(n)
            if op == 1: return json.loads(payload.decode())
            if op == 8: raise ConnectionError("ws close frame")

    def notify(self, method, **params):
        self.id += 1; self.send(json.dumps({"id": self.id, "method": method, "params": params})); return self.id

    def call(self, method, **params):
        with self.lock:
            mid = self.notify(method, **params)
            while True:
                m = self.recv()
                if m.get("id") == mid:
                    if "error" in m: raise RuntimeError(f"{method}: {m['error']}")
                    return m.get("result", {})
                if "method" in m: self.events.append(m)

    def wait_event(self, name, timeout=15):
        for i, e in enumerate(self.events):
            if e["method"] == name: return self.events.pop(i)["params"]
        self.sock.settimeout(timeout)
        try:
            while True:
                m = self.recv()
                if m.get("method") == name: return m["params"]
                if "method" in m: self.events.append(m)
        finally:
            self.sock.settimeout(30)

    def evaluate(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def close(self):
        try: self.sock.close()
        except Exception: pass
