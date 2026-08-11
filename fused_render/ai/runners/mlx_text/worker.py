"""Text generation on MLX: one resident model, four routes (SPEC §40).

Started by `fused_render.ai.supervisor` on the interpreter built from this
folder's `pyproject.toml`. Nothing in fused-render imports this file — it runs in
a different interpreter, and the only contract between them is HTTP:

    GET  /health    {state, model, detail, error, resident_bytes, loaded_at}
    POST /generate  {messages|prompt, max_tokens, temperature, top_p} -> NDJSON
    POST /cancel    stop the generation in flight
    POST /quit      release the weights and exit

Every request carries `X-Fused-Worker: <token>`, which the supervisor generated
and passed in the environment. The port is ephemeral and published back through
the status file, so a foreign page cannot reach this by guessing a well-known
number, and a local process that guessed the port still has no token.

**The download happens here**, because this is the process with `huggingface_hub`
in it — and it reports its own byte counts straight to the app's download manager
(`/api/jobs`) under the id the supervisor passed. That is what makes an 8GB pull
visible in the same list as every other long job, cancellable from the same ✕.

**One generation at a time.** MLX's model object is not safe to call from two
threads, and a laptop has one GPU: a second request waits rather than interleaves.

Deliberately stdlib + mlx-lm only. No FastAPI, no requests — this process must
start fast and its dependency list is a thing users download.
"""

import argparse
import http.server
import json
import os
import socket
import socketserver
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

STATE = {
    "state": "starting",   # starting | downloading | loading | ready | error
    "model": "",
    "detail": "",
    "error": "",
    "resident_bytes": None,
    "loaded_at": None,
}
_state_lock = threading.Lock()

#: The loaded (model, tokenizer). One per process — see the module docstring.
_loaded = {}
_generate_lock = threading.Lock()
_cancel = threading.Event()

TOKEN = os.environ.get("FUSED_AI_WORKER_TOKEN", "")
JOB_ID = ""
JOB_URL = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/") + "/api/jobs"


def set_state(**fields):
    with _state_lock:
        STATE.update(fields)


def snapshot():
    with _state_lock:
        return dict(STATE)


# ------------------------------------------------------- reporting to the app


def report(**fields):
    """One progress tick to the download manager. Never raises, never blocks long.

    Reporting is decoration: if it fails the model still loads. The socket
    timeout is short so a wedged server cannot stall the download it describes.
    """
    if not JOB_ID or not JOB_URL.startswith("http"):
        return
    body = json.dumps({"id": JOB_ID, **fields}).encode()
    request = urllib.request.Request(
        JOB_URL, data=body,
        headers={"Content-Type": "application/json", "X-Fused": "1",
                 "X-Fused-Worker": TOKEN},
        method="POST")
    try:
        urllib.request.urlopen(request, timeout=3.0).close()
    except (urllib.error.URLError, OSError, ValueError):
        pass


def resident_bytes():
    """This process's resident memory, or None.

    On Apple Silicon the GPU pool IS system memory, so RSS is the honest single
    number for "what is this model costing you" — no separate VRAM figure exists
    to reconcile it with. psutil comes with the runner's environment; if it is
    somehow absent the answer is None rather than a guess.
    """
    try:
        import psutil
    except ImportError:
        return None
    try:
        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:  # noqa: BLE001 - psutil raises its own family; none is fatal here
        return None


# --------------------------------------------------------------- model loading


def _download(model_id):
    """Fetch what is missing. Returns the snapshot path.

    Split from loading because the AI Models page's "Download" wants exactly
    this half: fill the cache, hold nothing.
    """
    set_state(state="downloading", detail="Fetching weights…")
    report(state="running", kind="download", unit="bytes",
           detail="Fetching weights…", done=None, total=None)

    from huggingface_hub import snapshot_download

    # ONE progress figure for the whole snapshot: hf reports per file, and a bar
    # that restarts at zero for each of four shards is worse than one that
    # counts the set. Every live tqdm registers itself here and the totals are
    # summed across them.
    seen = {}

    class _Tqdm:
        def __init__(self, *args, **kwargs):
            self.total = kwargs.get("total") or 0
            self.n = 0
            self.desc = kwargs.get("desc") or ""
            seen[id(self)] = self

        def update(self, n=1):
            self.n += n
            done = sum(t.n for t in seen.values())
            total = sum(t.total for t in seen.values()) or None
            report(done=done, total=total, detail=self.desc or "Fetching weights…")

        def close(self):
            seen.pop(id(self), None)

        # tqdm's context-manager and iterator surface, minimally — hf uses it
        # both ways depending on the code path.
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

        def __iter__(self):
            return iter(())

        def set_description(self, desc=None, refresh=True):
            self.desc = desc or ""

        def refresh(self):
            pass

    return snapshot_download(model_id, tqdm_class=_Tqdm)


def _download_and_load(model_id):
    """Fetch what is missing, then load it into memory."""
    try:
        path = _download(model_id)

        set_state(state="loading", detail="Loading weights into memory…")
        # No total: this is one long opaque step, and an invented percentage is
        # what makes live work read as frozen.
        report(kind="task", unit="", done=None, total=None,
               detail="Loading weights into memory…")

        from mlx_lm import load

        model, tokenizer = load(path)
        _loaded["model"] = model
        _loaded["tokenizer"] = tokenizer
        set_state(state="ready", detail="", resident_bytes=resident_bytes(),
                  loaded_at=time.time())
    except BaseException as e:  # noqa: BLE001 - the reply IS the error report
        # Deliberately broad and deliberately last: this thread is the only
        # thing that can explain why a load failed, and an unhandled exception
        # here would leave /health saying "loading" forever.
        set_state(state="error", error=f"{e.__class__.__name__}: {e}")
        report(state="error", message=str(e) or e.__class__.__name__)
        traceback.print_exc(file=sys.stderr)


# ------------------------------------------------------------------ generation


def _messages_to_prompt(tokenizer, messages, prompt):
    """The model's own chat template, never a hand-rolled one.

    Every instruct model has its own turn markers, and getting them wrong
    produces output that looks almost right — which is worse than an error.
    `apply_chat_template` is the tokenizer's own answer; a model without one
    falls back to the raw prompt.
    """
    if prompt:
        return prompt
    template = getattr(tokenizer, "apply_chat_template", None)
    if template and getattr(tokenizer, "chat_template", None):
        return template(messages, tokenize=False, add_generation_prompt=True)
    return "\n\n".join(m.get("content", "") for m in messages if isinstance(m, dict))


def _generate(body, write):
    """Stream one completion as NDJSON: {token} lines, then {done}."""
    from mlx_lm import stream_generate
    from mlx_lm.sample_utils import make_sampler

    model = _loaded.get("model")
    tokenizer = _loaded.get("tokenizer")
    if model is None or tokenizer is None:
        write({"type": "done", "ok": False, "error": "no model is loaded"})
        return

    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    text = _messages_to_prompt(tokenizer, messages, body.get("prompt") or "")
    max_tokens = int(body.get("max_tokens") or 1024)
    sampler = make_sampler(
        temp=float(body.get("temperature", 0.7)),
        top_p=float(body.get("top_p", 0.95)),
    )

    _cancel.clear()
    count = 0
    started = time.time()
    for response in stream_generate(model, tokenizer, text, max_tokens=max_tokens,
                                    sampler=sampler):
        if _cancel.is_set():
            write({"type": "done", "ok": True, "cancelled": True, "tokens": count})
            return
        count += 1
        write({"type": "chunk", "text": response.text})
    write({
        "type": "done", "ok": True, "tokens": count,
        "seconds": round(time.time() - started, 2),
    })


# ----------------------------------------------------------------- HTTP server


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # the supervisor captures stderr; per-request noise is not useful

    def _authorized(self):
        if TOKEN and self.headers.get("X-Fused-Worker") == TOKEN:
            return True
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorized():
            return
        if self.path.startswith("/health"):
            self._json(snapshot())
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self):
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._json({"error": "body must be JSON"}, status=400)
            return

        if self.path.startswith("/quit"):
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.1), os._exit(0)),
                             daemon=True).start()
            return

        if self.path.startswith("/cancel"):
            _cancel.set()
            self._json({"ok": True})
            return

        if self.path.startswith("/generate"):
            if snapshot()["state"] != "ready":
                self._json({"error": "the model is not loaded"}, status=409)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def write(payload):
                line = (json.dumps(payload) + "\n").encode()
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()

            # Serialized: one GPU, one model object, and mlx is not thread-safe.
            with _generate_lock:
                try:
                    _generate(body, write)
                except BaseException as e:  # noqa: BLE001 - must reach the client
                    write({"type": "done", "ok": False,
                           "error": f"{e.__class__.__name__}: {e}"})
                    traceback.print_exc(file=sys.stderr)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        self._json({"error": "not found"}, status=404)


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET


def main():
    global JOB_ID

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    # Not required: a download-only run serves nothing, so it has no port to
    # publish and no status file to publish it in.
    parser.add_argument("--status", default="")
    parser.add_argument("--job", default="")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    JOB_ID = args.job
    set_state(model=args.model)

    if args.download_only:
        # Fill the cache and stop. The exit CODE is the answer here — the
        # supervisor is waiting on the process, not on a health route — so a
        # failure must not be swallowed into a status nobody reads.
        try:
            _download(args.model)
        except BaseException as e:  # noqa: BLE001 - stderr is the supervisor's report
            traceback.print_exc(file=sys.stderr)
            sys.stderr.write(f"\n{e.__class__.__name__}: {e}\n")
            sys.exit(1)
        sys.exit(0)

    if not args.status:
        sys.stderr.write("--status is required unless --download-only\n")
        sys.exit(2)

    # Bind :0 and publish what we got. Anything the parent reserved could be
    # taken between its bind and our exec, so the child is the one that picks.
    server = Server(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    tmp = args.status + ".tmp"
    with open(tmp, "w") as handle:
        json.dump({"port": port, "pid": os.getpid(), "model": args.model}, handle)
    os.replace(tmp, args.status)

    threading.Thread(target=_download_and_load, args=(args.model,),
                     name="load", daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
