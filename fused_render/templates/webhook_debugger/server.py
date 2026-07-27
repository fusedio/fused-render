# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Webhook capture daemon for the webhook_debugger template.

One long-lived localhost process runs TWO independent HTTP surfaces:

  Capture surface  — 127.0.0.1:<capture_port> (default 17780, no token).
                     Accepts ANY method + ANY path and records the request
                     into an in-RAM ring (mirrored to capture.jsonl). This is
                     what the user points their webhook / curl at. Write-only.
  Control surface  — 127.0.0.1:<ephemeral> (per-daemon token as ?t=).
                     The template reads captures, histograms, replays, and
                     drives the optional cloudflared tunnel through here.

main(action="ensure", file=<descriptor>) parses the .webhook descriptor,
reuses a live daemon that matches the code version + capture port + config, or
respawns one, and returns the ports/token for the client handshake. Same
architecture as netcdf/grid_tile_server.py, but the reaper idles at 12h (not
30min): a webhook debugger that exits drops incoming webhooks.
"""

import hashlib
import json
import os
import sys
import threading
import time

DEFAULT_CAPTURE_PORT = 17780
IDLE_EXIT_S = 12 * 60 * 60
BODY_STORE_CAP = 1 * 1024 * 1024
BODY_READ_CAP = 25 * 1024 * 1024
REPLAY_BODY_CAP = 256 * 1024
JSONL_REWRITE_BYTES = 64 * 1024 * 1024
RING_MAX = 1000

_STD_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailers", "transfer-encoding",
               "upgrade", "content-length", "host"}


def _home():
    base = os.environ.get("FUSED_WEBHOOK_HOME")
    if not base:
        base = os.path.expanduser("~/.cache/fused-render-webhook")
    return base


def _state_path():
    return os.path.join(_home(), "daemon.json")


def _jsonl_path():
    return os.path.join(_home(), "capture.jsonl")


def _me():
    if "__file__" in globals():
        return os.path.abspath(__file__)
    return os.path.join(os.path.abspath(sys.path[0]), "server.py")


def _version():
    try:
        h = hashlib.sha256(open(_me(), "rb").read()).hexdigest()[:12]
    except OSError:
        h = "0"
    # Normalize pythonw.exe -> python.exe: the daemon runs under pythonw (no
    # console) while the ensuring parent runs under python, and the version must
    # match across both so the liveness check recognizes the daemon.
    exe = sys.executable
    if os.name == "nt" and os.path.basename(exe).lower() == "pythonw.exe":
        exe = os.path.join(os.path.dirname(exe), "python.exe")
    return h + "|" + exe


def _config_sig(cfg):
    return hashlib.sha256(
        json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _parse_descriptor(file):
    raw = {}
    if file:
        try:
            with open(file, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            return None, f"cannot read descriptor: {e}"
        if not isinstance(raw, dict):
            return None, "descriptor must be a JSON object"
    cfg = {
        "capture_port": int(raw.get("capture_port") or DEFAULT_CAPTURE_PORT),
        "response": raw.get("response") if isinstance(raw.get("response"), dict) else None,
        "keep_alive": bool(raw.get("keep_alive")),
        "public_url": (raw.get("public_url") or "").strip() or None,
        "name": (raw.get("name") or "").strip() or None,
        "path": (raw.get("path") or "").strip() or None,
    }
    return cfg, None


def _alive(port, version):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            d = json.load(r)
        return d.get("ok") and d.get("version") == version
    except Exception:
        return False


def main(action: str = "ensure", file: str = ""):
    import subprocess
    cfg, err = _parse_descriptor(file)
    if err:
        return {"error": err}
    version = _version()
    sig = _config_sig(cfg)
    capture_port = cfg["capture_port"]
    state = _state_path()

    def _result(st, reused):
        cap = st.get("capture_port", capture_port)
        return {"capture_port": cap, "control_port": st["port"],
                "token": st.get("token"), "capture_url": f"http://127.0.0.1:{cap}",
                "public_url": st.get("public_url") or cfg["public_url"],
                "name": cfg["name"], "path": cfg["path"], "reused": reused}

    try:
        with open(state) as f:
            st = json.load(f)
        if (st.get("capture_port") == capture_port and st.get("config_sig") == sig
                and _alive(st.get("port"), version)):
            return _result(st, True)
        try:
            import urllib.request
            urllib.request.urlopen(
                f"http://127.0.0.1:{st.get('port')}/quit?t={st.get('token', '')}",
                timeout=1).read()
        except Exception:
            pass
    except (OSError, ValueError):
        pass

    os.makedirs(_home(), exist_ok=True)
    log = os.path.join(_home(), "daemon.log")
    env = dict(os.environ)
    env["FUSED_WEBHOOK_CONFIG"] = json.dumps(cfg, default=str)
    env["FUSED_WEBHOOK_CONFIG_SIG"] = sig
    # Launch the daemon windowless: a console-subsystem python.exe spawned
    # DETACHED_PROCESS gets a fresh console window on Windows (the popup), so run
    # the pythonw.exe sibling (GUI subsystem, no console) with the winopen.py combo.
    exe = sys.executable
    if os.name == "nt":
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pyw):
            exe = pyw
    detach = ({"creationflags": subprocess.DETACHED_PROCESS
               | subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
              if os.name == "nt" else {"start_new_session": True})
    with open(log, "ab") as lf:
        subprocess.Popen([exe, _me(), "--serve"], stdout=lf, stderr=lf,
                         stdin=subprocess.DEVNULL, cwd=os.path.dirname(_me()),
                         env=env, **detach)
    for _ in range(200):
        time.sleep(0.05)
        try:
            with open(state) as f:
                st = json.load(f)
            if (st.get("version") == version and st.get("config_sig") == sig
                    and _alive(st.get("port"), version)):
                return _result(st, False)
        except (OSError, ValueError):
            continue
    return {"error": f"webhook daemon did not start — see {log}"}


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass


# ================================================================ daemon
def _serve():
    import base64
    import re
    import secrets
    import shutil
    import subprocess
    import urllib.error
    import urllib.request
    from collections import deque
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs, urlsplit

    cfg = json.loads(os.environ.get("FUSED_WEBHOOK_CONFIG") or "{}")
    sig = os.environ.get("FUSED_WEBHOOK_CONFIG_SIG") or _config_sig(cfg)
    capture_port = int(cfg.get("capture_port") or DEFAULT_CAPTURE_PORT)
    response_cfg = cfg.get("response") if isinstance(cfg.get("response"), dict) else None
    keep_alive = bool(cfg.get("keep_alive"))
    public_url = [cfg.get("public_url")]

    VERSION = _version()
    TOKEN = secrets.token_urlsafe(32)
    last_hit = [time.time()]

    ring = deque(maxlen=RING_MAX)
    seq_counter = [0]
    lock = threading.Lock()
    tunnel = {"proc": None, "url": None, "state": "off"}

    jsonl = _jsonl_path()

    def _seed():
        if not os.path.exists(jsonl):
            return
        try:
            with open(jsonl, "rb") as f:
                lines = f.read().splitlines()
        except OSError:
            return
        for line in lines[-RING_MAX:]:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rec["body"] = base64.b64decode(rec.get("body_b64") or b"")
            rec.pop("body_b64", None)
            rec.setdefault("replays", [])
            ring.append(rec)
            seq_counter[0] = max(seq_counter[0], rec.get("seq", 0))

    def _persist(rec):
        out = dict(rec)
        out["body_b64"] = base64.b64encode(rec["body"]).decode("ascii")
        out.pop("body", None)
        with open(jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(out, default=str) + "\n")
        if os.path.getsize(jsonl) > JSONL_REWRITE_BYTES:
            _rewrite()

    def _rewrite():
        tmp = jsonl + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in list(ring):
                out = dict(rec)
                out["body_b64"] = base64.b64encode(rec["body"]).decode("ascii")
                out.pop("body", None)
                f.write(json.dumps(out, default=str) + "\n")
        os.replace(tmp, jsonl)

    _seed()

    def _read_body(handler):
        headers = handler.headers
        te = (headers.get("Transfer-Encoding") or "").lower()
        body = bytearray()
        total = 0
        truncated = False
        if "chunked" in te:
            while total < BODY_READ_CAP:
                size_line = handler.rfile.readline(64)
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    break
                if size == 0:
                    handler.rfile.readline()
                    break
                remaining = size
                while remaining > 0:
                    chunk = handler.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    total += len(chunk)
                    if len(body) < BODY_STORE_CAP:
                        body.extend(chunk[:BODY_STORE_CAP - len(body)])
                    if total >= BODY_READ_CAP:
                        break
                handler.rfile.readline()
            if total > len(body):
                truncated = True
            return bytes(body), total, truncated

        length = int(headers.get("Content-Length") or 0)
        length = min(length, BODY_READ_CAP)
        while total < length:
            chunk = handler.rfile.read(min(length - total, 65536))
            if not chunk:
                break
            total += len(chunk)
            if len(body) < BODY_STORE_CAP:
                body.extend(chunk[:BODY_STORE_CAP - len(body)])
        if total > len(body):
            truncated = True
        return bytes(body), total, truncated

    def _capture(handler):
        last_hit[0] = time.time()
        body, body_len, truncated = _read_body(handler)
        u = urlparse(handler.path)
        with lock:
            seq_counter[0] += 1
            rec = {
                "seq": seq_counter[0],
                "id": secrets.token_hex(6),
                "ts": time.time(),
                "method": handler.command,
                "path": u.path,
                "query": u.query,
                "http_version": handler.request_version,
                "headers": [[k, v] for k, v in handler.headers.items()],
                "remote_addr": handler.client_address[0],
                "body": body,
                "body_truncated": truncated,
                "body_len": body_len,
                "replays": [],
            }
            ring.append(rec)
            _persist(rec)
        status, payload, ctype, extra = 200, json.dumps(
            {"ok": True, "id": rec["id"]}).encode(), "application/json", {}
        if response_cfg:
            status = int(response_cfg.get("status") or 200)
            ctype = response_cfg.get("content_type") or ctype
            rbody = response_cfg.get("body")
            if rbody is not None:
                payload = (rbody if isinstance(rbody, str)
                           else json.dumps(rbody)).encode()
            extra = response_cfg.get("headers") if isinstance(
                response_cfg.get("headers"), dict) else {}
        handler.send_response(status)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(payload)))
        for k, v in extra.items():
            handler.send_header(str(k), str(v))
        handler.end_headers()
        if handler.command != "HEAD":
            try:
                handler.wfile.write(payload)
            except BrokenPipeError:
                pass

    class Capture(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def __getattr__(self, name):
            if name.startswith("do_"):
                return lambda: _capture(self)
            raise AttributeError(name)

    # ---------------- control surface ----------------
    def _matcher(q):
        q = (q or "").strip()
        if q.startswith("re:"):
            p = re.compile(q[3:])
            return lambda t: p.search(t) is not None
        if len(q) > 2 and q.startswith("/") and q.endswith("/"):
            p = re.compile(q[1:-1])
            return lambda t: p.search(t) is not None
        needle = q.casefold()
        return lambda t: not needle or needle in t.casefold()

    def _haystack(rec):
        parts = [rec["method"], rec["path"], rec["query"]]
        parts += [f"{k}: {v}" for k, v in rec["headers"]]
        try:
            parts.append(rec["body"].decode("utf-8", "replace"))
        except Exception:
            pass
        return "\n".join(parts)

    def _method_bucket(method):
        return method if method in _STD_METHODS else "OTHER"

    def _filters(q, methods, path_prefix, from_ts, to_ts):
        match_q = _matcher(q)
        wanted = {m.strip().upper() for m in (methods or "").split(",") if m.strip()}

        def ok(rec):
            if wanted and _method_bucket(rec["method"]) not in wanted:
                return False
            if path_prefix and not rec["path"].startswith(path_prefix):
                return False
            if from_ts and rec["ts"] < from_ts:
                return False
            if to_ts and rec["ts"] > to_ts:
                return False
            return match_q(_haystack(rec))
        return ok

    def _summary(rec):
        ctype = next((v for k, v in rec["headers"] if k.lower() == "content-type"), "")
        return {"seq": rec["seq"], "id": rec["id"], "ts": rec["ts"],
                "method": rec["method"], "path": rec["path"], "query": rec["query"],
                "body_len": rec["body_len"], "body_truncated": rec["body_truncated"],
                "content_type": ctype, "header_count": len(rec["headers"]),
                "replay_count": len(rec["replays"])}

    def _requests(q):
        since = int(q.get("since", ["0"])[0] or 0)
        limit = max(1, min(int(q.get("limit", ["200"])[0] or 200), 1000))
        tail = q.get("tail", ["1"])[0] not in ("", "0")
        ok = _filters(q.get("q", [""])[0], q.get("methods", [""])[0],
                      q.get("path_prefix", [""])[0],
                      float(q.get("from", ["0"])[0] or 0),
                      float(q.get("to", ["0"])[0] or 0))
        with lock:
            snap = list(ring)
            next_seq = seq_counter[0]
        matched = [r for r in snap if r["seq"] > since and ok(r)]
        matching_count = len(matched)
        rows = matched[-limit:] if tail else matched[:limit]
        return {"rows": [_summary(r) for r in rows], "next_seq": next_seq,
                "matching_count": matching_count, "count": len(snap)}

    def _full(rec):
        body = rec["body"]
        try:
            text = body.decode("utf-8")
            binary = False
            b64 = None
        except UnicodeDecodeError:
            text = None
            binary = True
            b64 = base64.b64encode(body).decode("ascii")
        return {"seq": rec["seq"], "id": rec["id"], "ts": rec["ts"],
                "method": rec["method"], "path": rec["path"], "query": rec["query"],
                "http_version": rec["http_version"], "remote_addr": rec["remote_addr"],
                "headers": rec["headers"], "body_len": rec["body_len"],
                "body_truncated": rec["body_truncated"], "body_text": text,
                "binary": binary, "body_base64": b64, "replays": rec["replays"]}

    def _request(q):
        rid = q.get("id", [""])[0]
        with lock:
            rec = next((r for r in ring if r["id"] == rid), None)
        if not rec:
            return None
        return _full(rec)

    def _histogram(q):
        import math
        bins = max(10, min(int(q.get("bins", ["60"])[0] or 60), 200))
        ok = _filters(q.get("q", [""])[0], q.get("methods", [""])[0],
                      q.get("path_prefix", [""])[0],
                      float(q.get("from", ["0"])[0] or 0),
                      float(q.get("to", ["0"])[0] or 0))
        with lock:
            snap = [r for r in ring if ok(r)]
        buckets = {}
        width = 1.0

        def counts():
            return {m: 0 for m in (_STD_METHODS + ("OTHER",))}

        def merge(b):
            out = {}
            for i, bk in b.items():
                t = out.setdefault(i // 2, {"count": 0, "methods": counts()})
                t["count"] += bk["count"]
                for m, c in bk["methods"].items():
                    t["methods"][m] += c
            return out

        for rec in snap:
            index = math.floor(rec["ts"] / width)
            bucket = buckets.setdefault(index, {"count": 0, "methods": counts()})
            bucket["count"] += 1
            bucket["methods"][_method_bucket(rec["method"])] += 1
            while buckets and max(buckets) - min(buckets) + 1 > bins:
                buckets = merge(buckets)
                width *= 2.0
        out = []
        if buckets:
            for i in range(min(buckets), max(buckets) + 1):
                b = buckets.get(i, {"count": 0, "methods": counts()})
                out.append({"start": i * width, "end": (i + 1) * width,
                            "count": b["count"], "methods": b["methods"]})
        return {"bins": out, "bucket_seconds": width, "matching_count": len(snap)}

    def _replay(payload):
        rid = payload.get("id")
        with lock:
            rec = next((r for r in ring if r["id"] == rid), None)
        if not rec:
            return None, "unknown request id"
        url = (payload.get("url") or "").strip()
        if not url:
            return None, "a target url is required"
        method = (payload.get("method") or rec["method"]).upper()
        host = urlsplit(url).netloc
        if payload.get("headers") is not None:
            src_headers = payload["headers"].items() if isinstance(
                payload["headers"], dict) else payload["headers"]
        else:
            src_headers = rec["headers"]
        headers = {}
        for k, v in src_headers:
            if k.lower() in _HOP_BY_HOP:
                continue
            headers[k] = v
        if host:
            headers["Host"] = host
        body = payload.get("body")
        if body is None:
            data = rec["body"]
        else:
            data = body.encode("utf-8") if isinstance(body, str) else body
        if method in ("GET", "HEAD") and not data:
            data = None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                out_body = resp.read(REPLAY_BODY_CAP)
                status = resp.status
                out_headers = [[k, v] for k, v in resp.headers.items()]
        except urllib.error.HTTPError as e:
            out_body = e.read(REPLAY_BODY_CAP)
            status = e.code
            out_headers = [[k, v] for k, v in e.headers.items()]
        except Exception as e:
            return None, str(e)
        duration_ms = round((time.time() - start) * 1000, 1)
        try:
            btext = out_body.decode("utf-8")
        except UnicodeDecodeError:
            btext = base64.b64encode(out_body).decode("ascii")
        result = {"status": status, "headers": out_headers, "body": btext,
                  "duration_ms": duration_ms, "url": url, "method": method,
                  "ts": start}
        with lock:
            rec["replays"].append({"ts": start, "url": url, "method": method,
                                   "status": status, "duration_ms": duration_ms})
        return result, None

    def _tunnel_start():
        if tunnel["proc"] and tunnel["proc"].poll() is None:
            return {"available": True, "public_url": tunnel["url"],
                    "state": tunnel["state"]}
        binary = shutil.which("cloudflared")
        if not binary:
            return {"available": False, "hint":
                    "Install cloudflared: https://developers.cloudflare.com/"
                    "cloudflare-one/connections/connect-networks/downloads/ "
                    "— or paste your own tunnel URL"}
        no_window = ({"creationflags": subprocess.CREATE_NO_WINDOW}
                     if os.name == "nt" else {})
        proc = subprocess.Popen(
            [binary, "tunnel", "--url", f"http://127.0.0.1:{capture_port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, **no_window)
        tunnel["proc"] = proc
        tunnel["state"] = "starting"
        tunnel["url"] = None

        def watch():
            pat = re.compile(rb"https://[-a-z0-9]+\.trycloudflare\.com")
            for line in proc.stdout:
                m = pat.search(line)
                if m:
                    tunnel["url"] = m.group(0).decode()
                    tunnel["state"] = "up"
                    break
            proc.stdout.close()
        threading.Thread(target=watch, daemon=True).start()
        for _ in range(120):
            if tunnel["url"]:
                break
            time.sleep(0.25)
        return {"available": True, "public_url": tunnel["url"],
                "state": tunnel["state"] if tunnel["url"] else "starting"}

    def _tunnel_stop():
        proc = tunnel["proc"]
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        tunnel["proc"] = None
        tunnel["url"] = None
        tunnel["state"] = "off"
        return {"state": "off"}

    def _ping():
        with lock:
            count, last_seq = len(ring), seq_counter[0]
        return {"ok": True, "version": VERSION, "capture_port": capture_port,
                "count": count, "last_seq": last_seq,
                "public_url": tunnel["url"] or public_url[0],
                "tunnel_state": tunnel["state"]}

    class Control(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, default=str).encode() if not isinstance(obj, bytes) else obj
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def _authed(self, q):
            return q.get("t", [""])[0] == TOKEN

        def do_GET(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/ping":
                self._send(200, _ping())
                return
            if not self._authed(q):
                self._send(403, {"error": "forbidden"})
                return
            if u.path == "/quit":
                self._send(200, {"ok": True})
                _tunnel_stop()
                threading.Thread(target=control.shutdown, daemon=True).start()
                threading.Thread(target=capture.shutdown, daemon=True).start()
                return
            try:
                if u.path == "/api/requests":
                    self._send(200, _requests(q))
                elif u.path == "/api/request":
                    rec = _request(q)
                    self._send(200 if rec else 404, rec or {"error": "not found"})
                elif u.path == "/api/histogram":
                    self._send(200, _histogram(q))
                else:
                    self._send(404, {"error": "not found"})
            except re.error as e:
                self._send(400, {"error": f"bad regex: {e}"})
            except Exception as e:
                self._send(500, {"error": str(e)})

        def do_POST(self):
            last_hit[0] = time.time()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if not self._authed(q):
                self._send(403, {"error": "forbidden"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {}
            if u.path == "/api/replay":
                result, err = _replay(payload)
                self._send(200 if result else 400, result or {"error": err})
            elif u.path == "/api/clear":
                with lock:
                    ring.clear()
                    open(jsonl, "w").close()
                self._send(200, {"ok": True})
            elif u.path == "/api/tunnel/start":
                self._send(200, _tunnel_start())
            elif u.path == "/api/tunnel/stop":
                self._send(200, _tunnel_stop())
            else:
                self._send(404, {"error": "not found"})

    capture = ThreadingHTTPServer(("127.0.0.1", capture_port), Capture)
    control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
    control_port = control.server_address[1]

    os.makedirs(_home(), exist_ok=True)
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump({"port": control_port, "token": TOKEN, "version": VERSION,
                   "capture_port": capture_port, "config_sig": sig,
                   "public_url": public_url[0], "pid": os.getpid()}, f)

    if not keep_alive:
        def reaper():
            while True:
                time.sleep(300)
                if time.time() - last_hit[0] > IDLE_EXIT_S:
                    _tunnel_stop()
                    capture.shutdown()
                    control.shutdown()
                    return
        threading.Thread(target=reaper, daemon=True).start()

    threading.Thread(target=capture.serve_forever, daemon=True).start()
    print(f"webhook daemon: capture 127.0.0.1:{capture_port} "
          f"control 127.0.0.1:{control_port} (v{VERSION})", flush=True)
    control.serve_forever()


if __name__ == "__main__" and "--serve" in sys.argv:
    _serve()
