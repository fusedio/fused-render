"""Mini Python kernel for the notebook template — stdlib only, so it runs
under ANY interpreter the user points the env picker at (Python >= 3.9).

Launched by kernel.py's daemon as `[python, kernel_body.py]` with
MPLBACKEND=Agg and cwd = the notebook's directory. Speaks JSON-lines:
requests on stdin ({"op":"execute","id","code"} / {"op":"interrupt"}),
events on stdout (ready/stream/execute_result/display_data/error/done, one
object per line). Nothing else may print to the real stdout — sys.stdout/
stderr/stdin are swapped while user code runs. Cells execute serially on the
main thread in one shared namespace; interrupt rides the stdin reader thread,
drops every queued execute, and raises KeyboardInterrupt in the running cell:
pthread_kill(main_thread, SIGINT) on POSIX (only delivery to the main thread
itself interrupts a C-blocked time.sleep there) and signal.raise_signal(SIGINT)
on Windows, where
the C handler wakes sleeping threads and — unlike console CTRL events — needs
no console, which neither the daemon nor this process has.
"""
import ast
import base64
import io
import json
import os
import queue
import signal
import sys
import threading
import time
import traceback
import warnings

REAL_STDOUT = sys.stdout
REAL_STDERR = sys.stderr
REAL_STDIN = sys.stdin
EMIT_LOCK = threading.Lock()
RUNNING = threading.Event()
STREAM_CAP = 2 * 1024 * 1024
FLUSH_BYTES = 8 * 1024
FLUSH_S = 0.1

NS = {"__name__": "__main__", "__builtins__": __builtins__}


def emit(obj):
    with EMIT_LOCK:
        REAL_STDOUT.write(json.dumps(obj) + "\n")
        REAL_STDOUT.flush()


class StreamWriter(io.TextIOBase):
    """stdout/stderr replacement: coalesces writes into stream events,
    flushing at ~100 ms / 8 KB boundaries, capped at 2 MB per execution
    (the budget dict is shared between the stdout and stderr writers)."""

    def __init__(self, name, exec_id, budget):
        self._name = name
        self._exec_id = exec_id
        self._budget = budget
        self._buf = []
        self._size = 0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def writable(self):
        return True

    def write(self, s):
        s = str(s)
        with self._lock:
            if self._budget["left"] <= 0:
                if not self._budget["truncated"]:
                    self._budget["truncated"] = True
                    emit({"type": "stream", "id": self._exec_id, "name": "stderr",
                          "text": "\n[output truncated — 2 MB limit reached]\n"})
                return len(s)
            take = s[:self._budget["left"]]
            self._budget["left"] -= len(take)
            self._buf.append(take)
            self._size += len(take)
            if self._size >= FLUSH_BYTES or time.monotonic() - self._last >= FLUSH_S:
                self._flush_locked()
        return len(s)

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if self._buf:
            emit({"type": "stream", "id": self._exec_id, "name": self._name,
                  "text": "".join(self._buf)})
            self._buf = []
            self._size = 0
        self._last = time.monotonic()


def display_data(value):
    html = getattr(value, "_repr_html_", None)
    if callable(html):
        out = html()
        if isinstance(out, str):
            return {"text/html": out}
    png = getattr(value, "_repr_png_", None)
    if callable(png):
        out = png()
        if isinstance(out, bytes):
            return {"image/png": base64.b64encode(out).decode("ascii")}
        if isinstance(out, str):
            return {"image/png": out}
    return {"text/plain": repr(value)}


def take_figures():
    """Open matplotlib figures -> base64 PNGs, then close them all."""
    if "matplotlib" not in sys.modules:
        return []
    from matplotlib._pylab_helpers import Gcf
    out = []
    for mgr in Gcf.get_all_fig_managers():
        buf = io.BytesIO()
        mgr.canvas.figure.savefig(buf, format="png", bbox_inches="tight")
        out.append(base64.b64encode(buf.getvalue()).decode("ascii"))
    Gcf.destroy_all()
    return out


def format_tb(exc):
    """Traceback lines with the kernel's own frames trimmed off the top."""
    tb = exc.__traceback__
    while tb and tb.tb_frame.f_code.co_filename != "<cell>":
        tb = tb.tb_next
    if tb is None:
        lines = traceback.format_exception_only(type(exc), exc)
    else:
        lines = traceback.format_exception(type(exc), exc, tb)
    return [seg for chunk in lines for seg in chunk.rstrip("\n").split("\n")]


def execute(exec_id, code):
    t0 = time.monotonic()
    emit({"type": "started", "id": exec_id})
    budget = {"left": STREAM_CAP, "truncated": False}
    out = StreamWriter("stdout", exec_id, budget)
    err = StreamWriter("stderr", exec_id, budget)
    saved = sys.stdout, sys.stderr, sys.stdin
    try:
        # the swap lives inside the try: an interrupt landing mid-assignment
        # is still caught here and the finally restores the saved trio
        sys.stdout, sys.stderr, sys.stdin = out, err, io.StringIO()
        tree = ast.parse(code, "<cell>", "exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body[-1].value)
            tree.body = tree.body[:-1]
        exec(compile(tree, "<cell>", "exec"), NS)
        value = eval(compile(last, "<cell>", "eval"), NS) if last else None
        if value is not None:
            emit({"type": "execute_result", "id": exec_id,
                  "data": display_data(value)})
        for png in take_figures():
            emit({"type": "display_data", "id": exec_id,
                  "data": {"image/png": png}})
    except BaseException as exc:  # noqa: BLE001 — includes KeyboardInterrupt
        emit({"type": "error", "id": exec_id, "ename": type(exc).__name__,
              "evalue": str(exc), "traceback": format_tb(exc)})
        if "matplotlib" in sys.modules:
            from matplotlib._pylab_helpers import Gcf
            Gcf.destroy_all()
    finally:
        out.flush()
        err.flush()
        sys.stdout, sys.stderr, sys.stdin = saved
    emit({"type": "done", "id": exec_id,
          "duration_ms": int((time.monotonic() - t0) * 1000)})


def _stdin_lines():
    """Request lines from the real stdin. On Windows this must never leave a
    ReadFile pending while user code runs: a blocked same-process stdin read
    deadlocks LoadLibrary of C extensions (numpy's CRT init blocks on the
    handle) — so poll with PeekNamedPipe and only read bytes already there."""
    if os.name != "nt":
        for line in sys.stdin:
            yield line
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    handle = msvcrt.get_osfhandle(0)
    avail = wintypes.DWORD()
    buf = b""
    while True:
        if not ctypes.windll.kernel32.PeekNamedPipe(
                handle, None, 0, None, ctypes.byref(avail), None):
            break  # pipe closed
        if not avail.value:
            time.sleep(0.03)
            continue
        chunk = os.read(0, avail.value)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8")


def read_requests(q):
    for line in _stdin_lines():
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        if req.get("op") == "interrupt":
            # Jupyter-style: interrupt aborts the queue too. Drain queued
            # executes before signalling so none can start afterwards, and
            # skip the signal entirely when nothing runs — a pending SIGINT
            # with the main thread parked in q.get() would otherwise fire on
            # the NEXT dequeue and swallow that execute (Windows lock waits
            # are not signal-interruptible).
            while True:
                try:
                    nxt = q.get_nowait()
                except queue.Empty:
                    break
                emit({"type": "error", "id": nxt["id"],
                      "ename": "KeyboardInterrupt", "evalue": "",
                      "traceback": ["KeyboardInterrupt"]})
                emit({"type": "done", "id": nxt["id"], "duration_ms": 0})
            if not RUNNING.is_set():
                continue
            if os.name == "nt":
                signal.raise_signal(signal.SIGINT)
            else:
                # raise_signal from this thread sets the flag but cannot wake
                # a main thread C-blocked in e.g. time.sleep on POSIX — the
                # signal must be delivered to the main thread itself
                signal.pthread_kill(
                    threading.main_thread().ident, signal.SIGINT)
        else:
            q.put(req)
    q.put(None)  # stdin closed — the daemon is gone


def main():
    # plt.show() is meaningless under Agg — figures are harvested after each
    # cell — so silence matplotlib's "cannot be shown" warning
    warnings.filterwarnings(
        "ignore", message=r"FigureCanvas\w+ is non-interactive")
    q = queue.Queue()
    threading.Thread(target=read_requests, args=(q,), daemon=True).start()
    emit({"type": "ready", "python": sys.executable, "version": sys.version})
    while True:
        try:
            req = q.get()
            if req is None:
                return
            if req.get("op") == "execute":
                RUNNING.set()
                try:
                    execute(req["id"], req.get("code") or "")
                except KeyboardInterrupt:
                    # the interrupt landed in execute's own bookkeeping
                    # (parse/emit/stdio swap), outside the user-code try —
                    # the cell must still resolve, and stdio must be sane.
                    # At-least-once: a second interrupt can raise inside this
                    # handler too; duplicates are daemon-safe, a missing done
                    # is not
                    RUNNING.clear()  # stop further signalling first
                    while True:
                        try:
                            sys.stdout, sys.stderr, sys.stdin = (
                                REAL_STDOUT, REAL_STDERR, REAL_STDIN)
                            emit({"type": "error", "id": req["id"],
                                  "ename": "KeyboardInterrupt", "evalue": "",
                                  "traceback": ["KeyboardInterrupt"]})
                            emit({"type": "done", "id": req["id"],
                                  "duration_ms": 0})
                            break
                        except KeyboardInterrupt:
                            pass
                finally:
                    RUNNING.clear()
        except KeyboardInterrupt:
            pass  # interrupt landed between cells — nothing to cancel


if __name__ == "__main__":
    main()
