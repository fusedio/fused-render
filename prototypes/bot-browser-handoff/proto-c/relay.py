"""Stdlib WebSocket SERVER that the extension's service worker dials. One live connection at a time."""
import base64, hashlib, json, socket, struct, threading, time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Relay:
    def __init__(self, port=17777):
        self.port = port
        self.srv = socket.socket(); self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port)); self.srv.listen(4)
        self.conn = None; self.hello = None; self.connected = threading.Event()
        self.nid = 0; self.pending = {}; self.events = []; self.lock = threading.Lock(); self.slock = threading.Lock()
        self.connects = 0
        threading.Thread(target=self._accept, daemon=True).start()

    # --- framing ---
    def _accept(self):
        while True:
            c, _ = self.srv.accept()
            try:
                buf = b""
                while b"\r\n\r\n" not in buf:
                    d = c.recv(4096)
                    if not d: raise ConnectionError
                    buf += d
                hdrs = {}
                for line in buf.split(b"\r\n")[1:]:
                    if b":" in line:
                        k, v = line.split(b":", 1); hdrs[k.strip().lower().decode()] = v.strip().decode()
                acc = base64.b64encode(hashlib.sha1((hdrs["sec-websocket-key"] + GUID).encode()).digest()).decode()
                c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
            except Exception:
                c.close(); continue
            self.conn = c; self.cbuf = b""; self.connects += 1; self.hello = None
            threading.Thread(target=self._reader, args=(c,), daemon=True).start()

    def _exact(self, c, n):
        while len(self.cbuf) < n:
            d = c.recv(1 << 20)
            if not d: raise ConnectionError("closed")
            self.cbuf += d
        r, self.cbuf = self.cbuf[:n], self.cbuf[n:]; return r

    def _frame(self, c):
        msg = b""
        while True:
            b0, b1 = self._exact(c, 2); op = b0 & 0xF; fin = b0 & 0x80; n = b1 & 0x7F
            if n == 126: n = struct.unpack(">H", self._exact(c, 2))[0]
            elif n == 127: n = struct.unpack(">Q", self._exact(c, 8))[0]
            mask = self._exact(c, 4) if b1 & 0x80 else b"\0\0\0\0"
            p = self._exact(c, n)
            p = bytes(b ^ mask[i % 4] for i, b in enumerate(p)) if b1 & 0x80 else p
            if op == 8: raise ConnectionError("close frame")
            if op == 9: self._send_raw(c, 0xA, p); continue
            if op == 0xA: continue
            msg += p
            if fin: return msg

    def _send_raw(self, c, op, data):
        hdr = bytearray([0x80 | op]); n = len(data)
        if n < 126: hdr.append(n)
        elif n < 65536: hdr += bytes([126]) + struct.pack(">H", n)
        else: hdr += bytes([127]) + struct.pack(">Q", n)
        with self.slock: c.sendall(bytes(hdr) + data)

    def _reader(self, c):
        try:
            while True:
                m = json.loads(self._frame(c).decode())
                if "id" in m and m["id"] in self.pending:
                    slot = self.pending[m["id"]]; slot["msg"] = m; slot["ev"].set()
                elif m.get("type") == "hello":
                    self.hello = m; self.connected.set()
                else:
                    m["t"] = time.time(); self.events.append(m)
        except Exception as e:
            if self.conn is c:
                self.conn = None; self.connected.clear()
                self.events.append({"type": "bridge_down", "why": str(e), "t": time.time()})

    # --- API ---
    def wait_connected(self, timeout=40):
        return self.connected.wait(timeout)

    def call(self, cmd, timeout=20, **kw):
        if not self.conn: raise ConnectionError("extension not connected")
        with self.lock:
            self.nid += 1; mid = self.nid
        slot = {"ev": threading.Event()}; self.pending[mid] = slot
        self._send_raw(self.conn, 1, json.dumps({"id": mid, "cmd": cmd, **kw}).encode())
        if not slot["ev"].wait(timeout): raise TimeoutError(cmd)
        m = self.pending.pop(mid)["msg"]
        if "error" in m: raise RuntimeError(f"{cmd}: {m['error']}")
        return m.get("result")

    def cdp(self, tabId, method, **params):
        return self.call("cdp", tabId=tabId, method=method, params=params)

    def evaluate(self, tabId, expr):
        r = self.cdp(tabId, "Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def close(self):
        try: self.conn and self.conn.close()
        except Exception: pass
        self.srv.close()
