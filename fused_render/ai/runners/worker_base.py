"""The worker side of the contract, written once (SPEC AI-3, AI-9).

Every runner is a folder with a `pyproject.toml` and a `worker.py`, started by
`fused_render.ai.supervisor` on the interpreter built from that declaration.
What a worker DOES differs completely — mlx_text loads a language model and
streams tokens, diffusers_image loads a pipeline and writes a PNG — but what it
IS does not: four routes, a token in a header, a port the child publishes, a
state machine the supervisor polls, and progress posted to the download manager.

That invariant half lives here. A concrete worker supplies three functions and
calls `serve()`:

    download(model_id)          fetch what is missing; return where it landed
    load(model_id, fetched)     put it in memory; raise to fail
    generate(body[, write])     one request; NDJSON via `write` or a dict back

and gets `/health`, `/cancel`, `/quit`, `/generate`, `--download-only`, the port
handshake, the auth check, the error framing and the reporting for free.

**Why a shared module rather than two standalone files.** The obvious reading of
"a runner is a folder" is that each folder is self-contained, and the first cut
was exactly that: mlx_text/worker.py carried all of this inline. Copying it for
the image runner would have put the SUPERVISOR'S contract — the auth header's
name, the status file's shape, the state vocabulary it polls for, the way
download bytes are measured — in two places, and every bug in this feature so
far has been two places encoding one rule and drifting apart. The contract
belongs beside the thing that defines it, once.

This module is **stdlib only**, deliberately, for two reasons. It is imported by
every runner's interpreter, so anything imported here becomes a dependency of
every backend forever. And it means the contract is IMPORTABLE BY THE TESTS,
which is the only way any of it gets tested at all: neither concrete worker can
run on CI (one needs Metal, the other several GB of torch), but this can, with
stub callables standing in for the model.
"""

import argparse
import fnmatch
import http.server
import json
import os
import socket
import socketserver
import stat
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

# ------------------------------------------------------------------- the state
#
# The vocabulary the SUPERVISOR polls for. `state` is the load-time machine —
# `ready` is the only value meaning the model can answer, `error` the only
# terminal failure — so these strings are contract, not description.

STATE = {
    "state": "starting",   # starting | downloading | loading | ready | error
    "model": "",
    "detail": "",
    "error": "",
    "resident_bytes": None,
    "loaded_at": None,
}
_state_lock = threading.Lock()

#: Set by `/cancel`. Long-running work checks it; what "stop" means is the
#: runner's to decide (a token loop breaks, a denoiser raises).
CANCEL = threading.Event()

#: One generation at a time. A laptop has one GPU, and neither mlx's model
#: object nor a diffusers pipeline is safe to call from two threads — so a
#: second request waits rather than interleaves.
GENERATE_LOCK = threading.Lock()

TOKEN = os.environ.get("FUSED_AI_WORKER_TOKEN", "")
JOB_ID = ""
JOB_URL = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/") + "/api/jobs"

JOB_TIMEOUT_S = 3.0


def set_state(**fields):
    with _state_lock:
        STATE.update(fields)


def snapshot():
    with _state_lock:
        return dict(STATE)


class Cancelled(Exception):
    """Raised out of a progress callback when the app asked us to stop.

    A worker's heavy phases are opaque C calls with no interruption point, so
    the only place a stop can be honoured is the callback the library hands us —
    which is where this comes from.
    """


# ------------------------------------------------------- reporting to the app


def report(job=None, **fields):
    """One progress tick to the download manager. Never raises, never blocks long.

    Returns the stored record, or None. **The return value is load-bearing**: the
    manager's ✕ sets `cancel_requested` on the row, and the reply to the tick we
    were sending anyway is how that reaches a process sitting inside a
    multi-minute call. Reporting is otherwise decoration — if it fails the model
    still loads — so the socket timeout is short and every error is swallowed.

    `job` overrides the id: a load reports to the row the supervisor opened for
    it, while one image generation reports to its own per-request row.
    """
    job = job or JOB_ID
    if not job or not JOB_URL.startswith("http"):
        return None
    body = json.dumps({"id": job, **fields}).encode()
    request = urllib.request.Request(
        JOB_URL, data=body,
        headers={"Content-Type": "application/json", "X-Fused": "1",
                 "X-Fused-Worker": TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=JOB_TIMEOUT_S) as response:
            record = json.loads(response.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def report_or_cancel(job=None, **fields):
    """`report`, raising `Cancelled` if the reply says the ✕ was pressed."""
    record = report(job=job, **fields)
    if record and record.get("cancel_requested"):
        raise Cancelled()
    return record


def resident_bytes():
    """This process's resident memory, or None.

    On Apple Silicon the GPU pool IS system memory, so RSS is the honest single
    number for "what is this model costing you" — no separate VRAM figure exists
    to reconcile it with. psutil comes with every runner's environment; if it is
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


# --------------------------------------------------------- downloading weights
#
# Progress is measured from the DISK (SPEC AI-5b). `snapshot_download` exposes
# only its outer "Fetching N files" counter through `tqdm_class`; the per-file
# byte bars are internal. Reporting that counter as bytes is how a 4.6GB pull
# came to read "10 / 11 B", and during a single large shard it does not move at
# all — so the row also went stale mid-download and the manager declared nobody
# was reporting. Walking the repo folder answers both: real bytes, and a tick
# every second whatever huggingface_hub is doing inside.


def repo_folder(model_id, repo_type="model"):
    """This repo's folder in the hub cache, or None.

    `repo_folder_name` is hf's OWN encoder for `org/name` -> `models--org--name`,
    used here rather than a `.replace("/", "--")` for the usual reason: the
    layout is theirs to change, and a second copy of it here would keep
    reporting numbers for a directory that no longer exists. If hf ever moves
    the helper, progress degrades to a pulse — never to a wrong figure.
    """
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        from huggingface_hub.file_download import repo_folder_name
    except ImportError:
        return None
    return os.path.join(HF_HUB_CACHE, repo_folder_name(repo_id=model_id, repo_type=repo_type))


def bytes_on_disk(folder):
    """How much of `folder` is on disk right now, in bytes — None if unknown.

    Counts the `.incomplete` files hf writes while a download is in flight,
    which is the whole point: they ARE the progress. Symlinks are skipped from
    the `lstat` result itself, so the snapshot entries are not counted a second
    time on top of the blobs they point at.
    """
    if not folder:
        return None
    total = 0
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISLNK(info.st_mode):
                total += info.st_size
    return total


def repo_total_bytes(model_id, include=None, ignore=None):
    """The size of what will ACTUALLY be fetched, from the Hub, or None.

    One metadata call, no weights. Without it the bar has no total and shows as
    indeterminate — which is honest, and much better than a wrong total.

    **Scoped, because a repo is rarely fetched whole.** `include` is a single
    filename (one GGUF out of a repo that publishes a dozen quantizations of the
    same model); `ignore` is the same fnmatch patterns `snapshot_download` takes,
    so a download that skips a subfolder does not measure itself against it.
    Summing the whole repo either way is how a 2.6GB pull came to read as a
    fraction of 30GB and then jump to "complete" against a figure it never
    downloaded.
    """
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(model_id, files_metadata=True)
    except Exception:  # noqa: BLE001 - a missing total is a cosmetic loss, never fatal
        return None
    total = 0
    for sibling in getattr(info, "siblings", None) or []:
        name = getattr(sibling, "rfilename", None) or ""
        size = getattr(sibling, "size", None)
        if not isinstance(size, int) or size <= 0:
            continue
        if include is not None and name != include:
            continue
        if ignore and any(fnmatch.fnmatch(name, pattern) for pattern in ignore):
            continue
        total += size
    return total or None


def _capped(done, total):
    """Never report more done than there is to do.

    `bytes_on_disk` measures the whole repo folder, and a SCOPED total covers
    only part of it — so a machine that already holds another quantization of
    the same model would otherwise report 8GB of a 2.6GB download.
    """
    if done is None or total is None:
        return done
    return min(done, total)


def fetch_with_progress(model_id, call, total=None, detail="Fetching weights…", job=None):
    """Run `call()` on a thread, reporting bytes-on-disk once a second.

    `call` is whatever huggingface_hub function actually fetches — a whole
    snapshot for one runner, a single GGUF file for another — and this is the
    part neither of them should write twice: the poll is the progress AND the
    heartbeat, without which a long single-file download reports nothing for
    minutes and the manager calls the row abandoned.
    """
    folder = repo_folder(model_id)
    if total is None:
        total = repo_total_bytes(model_id)
    report(job=job, state="running", kind="download", unit="bytes",
           detail=detail, done=_capped(bytes_on_disk(folder), total), total=total)

    result = {}

    def run():
        try:
            result["value"] = call()
        except BaseException as e:  # noqa: BLE001 - carried out and re-raised on the caller's thread
            result["error"] = e

    thread = threading.Thread(target=run, name="fetch", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(timeout=1.0)
        report(job=job, done=_capped(bytes_on_disk(folder), total), total=total,
               detail=detail)
    if "error" in result:
        raise result["error"]
    # Land on the total rather than on the last walk: the snapshot symlinks are
    # not counted, so a finished repo measures slightly under its own size and a
    # bar that stopped at 98% reads as a download that gave up.
    report(job=job, done=total or bytes_on_disk(folder), total=total)
    return result["value"]


def download_snapshot(model_id, ignore_patterns=None, **kwargs):
    """The repo, with progress. What most runners mean by "download".

    The total is measured against the SAME `ignore_patterns` the download uses,
    or a pull that deliberately skips a subfolder measures itself against
    weights it was never going to fetch — a bar that stalls partway and then
    jumps.
    """
    from huggingface_hub import snapshot_download

    return fetch_with_progress(
        model_id,
        lambda: snapshot_download(model_id, ignore_patterns=ignore_patterns, **kwargs),
        total=repo_total_bytes(model_id, ignore=ignore_patterns),
    )


def download_file(repo_id, filename, detail=None):
    """One file out of a repo — a GGUF checkpoint, say — with progress.

    The total is THAT FILE's size, not the repo's. A repo that publishes a dozen
    quantizations of the same model sums to tens of gigabytes, and measuring a
    2.6GB pull against that is how a download reads as barely started for its
    whole life and then jumps to complete.
    """
    from huggingface_hub import hf_hub_download

    return fetch_with_progress(
        repo_id,
        lambda: hf_hub_download(repo_id=repo_id, filename=filename),
        total=repo_total_bytes(repo_id, include=filename),
        detail=detail or f"Fetching {filename}…",
    )


# -------------------------------------------------------------------- bring-up


def _bring_up(model_id, download, load):
    """download -> load -> ready, on its own thread, reporting every step.

    `load` is handed what `download` returned — a snapshot path, a dict of
    paths — rather than resolving the files a second time. The first cut had
    `load` call the downloader again for its path, which re-ran the Hub metadata
    call and re-reported a finished download on every load of a cached model.
    """
    try:
        set_state(state="downloading", detail="Fetching weights…")
        fetched = download(model_id)

        set_state(state="loading", detail="Loading weights into memory…")
        # No total: this is one long opaque step, and an invented percentage is
        # what makes live work read as frozen.
        report(kind="task", unit="", done=None, total=None,
               detail="Loading weights into memory…")
        load(model_id, fetched)

        set_state(state="ready", detail="", error="",
                  resident_bytes=resident_bytes(), loaded_at=time.time())
        report(state="done", detail="Model loaded")
    except BaseException as e:  # noqa: BLE001 - this thread's only job is to explain a failure
        # Deliberately broad and deliberately last: this thread is the only
        # thing that can say why a load failed, and an unhandled exception here
        # would leave /health saying "loading" forever.
        set_state(state="error", error=f"{e.__class__.__name__}: {e}")
        report(state="error", message=str(e) or e.__class__.__name__)
        traceback.print_exc(file=sys.stderr)


# ----------------------------------------------------------------- HTTP server


def _handler(generate, streaming):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # the supervisor captures stderr; per-request noise is not useful

        def _authorized(self):
            # The token is a header the supervisor generated and passed in this
            # process's environment. A foreign page that guessed the ephemeral
            # port still cannot drive the model, and the value never lands in a
            # log line or a Referer.
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
                CANCEL.set()
                self._json({"ok": True})
                return

            if self.path.startswith("/generate"):
                if snapshot()["state"] != "ready":
                    self._json({"error": "the model is not loaded"}, status=409)
                    return
                CANCEL.clear()
                if streaming:
                    self._stream(body)
                else:
                    self._single(body)
                return

            self._json({"error": "not found"}, status=404)

        def _single(self, body):
            """One JSON reply, for work that produces an ARTEFACT rather than a
            stream. An image is not a sequence of tokens, and pretending it is
            would buy nothing — its progress is steps, and those go to the job
            row where the download manager can already draw them."""
            with GENERATE_LOCK:
                try:
                    self._json({"ok": True, "result": generate(body)})
                except Cancelled:
                    self._json({"ok": True, "cancelled": True})
                except BaseException as e:  # noqa: BLE001 - must reach the client
                    traceback.print_exc(file=sys.stderr)
                    self._json({"ok": False, "error": f"{e.__class__.__name__}: {e}"})

        def _stream(self, body):
            """NDJSON, chunked. `{"type":"chunk"}` lines closed by
            `{"type":"done"}` — the shape `fused.ai`'s reader already speaks."""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def write(payload):
                line = (json.dumps(payload) + "\n").encode()
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()

            with GENERATE_LOCK:
                try:
                    generate(body, write)
                except BaseException as e:  # noqa: BLE001 - must reach the client
                    write({"type": "done", "ok": False,
                           "error": f"{e.__class__.__name__}: {e}"})
                    traceback.print_exc(file=sys.stderr)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    return Handler


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET


def build_server(generate, streaming=False, host="127.0.0.1"):
    """The HTTP half on an ephemeral port, unstarted. Split out of `serve` so a
    test can drive the real routes without spawning a process."""
    return _Server((host, 0), _handler(generate, streaming))


def serve(download, load, generate, streaming=False, argv=None):
    """Parse the supervisor's argv and run this worker. Does not return.

    `--download-only` fills the cache and exits; the exit CODE is the answer
    there, because the supervisor waits on the process rather than on a health
    route, so a failure must not be swallowed into a status nobody reads.
    """
    global JOB_ID

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    # Not required: a download-only run serves nothing, so it has no port to
    # publish and no status file to publish it in.
    parser.add_argument("--status", default="")
    parser.add_argument("--job", default="")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args(argv)
    JOB_ID = args.job
    set_state(model=args.model)

    if args.download_only:
        try:
            download(args.model)
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
    server = build_server(generate, streaming)
    port = server.server_address[1]
    tmp = args.status + ".tmp"
    with open(tmp, "w") as handle:
        json.dump({"port": port, "pid": os.getpid(), "model": args.model}, handle)
    os.replace(tmp, args.status)

    threading.Thread(target=_bring_up, args=(args.model, download, load),
                     name="load", daemon=True).start()
    server.serve_forever()
