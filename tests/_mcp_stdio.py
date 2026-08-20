"""A live stdio MCP server subprocess, spoken to the way the Claude Code CLI does.

Shared by the two suites that drive the chat template's `permission_server.py`
over its own JSON-RPC (test_claude_permission_bridge.py for the approval wire
shape and the decision latch, test_claude_app_state.py for approvals +
`app_state`). Both point at `claude` now — they were split across the two
chat templates until the plain one was deleted (D235). Kept in a non-test module
so neither suite reaches
into the other's namespace — same reason as _claude_history.py — and so the
demultiplexer below exists exactly once: it encodes two real harness defects
whose symptoms were indistinguishable from a server bug (see _Server below).
"""
import json
import os
import subprocess
import threading


class Pending:
    """One in-flight JSON-RPC request: the slot its response lands in, or the reason
    none ever will.

    `result()` is the only way to read it, so a response that is lost, garbled or
    never sent fails as an explicit assertion naming the request — instead of an
    `IndexError` on an empty list three lines later, which is what the previous
    sink-plus-`join()` shape produced and what made a frequent CI failure a
    twenty-minute log dig."""

    def __init__(self, req_id, method):
        self.id = req_id
        self.method = method
        self.done = threading.Event()
        self.message = None
        self.failure = None  # why no response will arrive (EOF, undecodable line)

    def result(self, timeout=10.0):
        if not self.done.wait(timeout):
            raise AssertionError(
                f"no response to {self.method} (id {self.id}) within {timeout}s")
        if self.failure is not None:
            raise AssertionError(
                f"no response to {self.method} (id {self.id}): {self.failure}")
        return self.message


class MCPServer:
    """A server subprocess (`argv`), with one thread owning its stdout.

    ONE thread owns `proc.stdout` for the server's lifetime and routes every response
    to its request's slot by JSON-RPC id. Both halves fixed a real defect in this
    harness, and the concurrency test in test_claude_permission_bridge.py was failing
    in CI because of them:

    * **N threads reading one `TextIOWrapper` is not thread-safe.** Two readers can
      both find the decoded buffer empty and both go to the raw fd; one read returns
      BOTH lines, that reader hands back the first, and the second line sits in a
      buffer the other reader has already committed to bypassing — so that reader
      blocks forever and its response is unrecoverable. Measured ~1% of rounds under
      load; a bigger `join()` timeout cannot help a wedged thread. The visible symptom
      was `IndexError: list index out of range` on an empty sink.
    * **Nothing demultiplexed by id**, so a response could be handed to the wrong
      waiter: measured ~90% of concurrent rounds were already swapped. The old
      assertions could not tell, because both waiters expected the same verdict —
      which means the test would also have passed against a server that answered
      strictly serially, i.e. against the very bug it exists to catch.

    The server is RIGHT to answer out of order: permission_server spawns a thread per
    request precisely so a parked approval does not block later ones. Ordering is
    therefore the harness's problem to handle, not the server's to avoid.
    """

    def __init__(self, argv, env=None):
        self.proc = subprocess.Popen(
            [str(a) for a in argv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # encoding, not bare text=True: the real client is Node, which writes
            # `JSON.stringify(msg) + "\n"` as UTF-8 with non-ASCII unescaped. A
            # locale-encoded pipe here would make a non-ASCII payload depend on
            # the runner's LANG instead of pinning what the server actually
            # receives (test_a_windows_locale_stdio_still_parks_a_non_ascii_write).
            text=True, encoding="utf-8", env={**os.environ, **(env or {})})
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending = {}      # id -> Pending, awaiting a response
        self._unrouted = []     # responses nobody was waiting for (a harness bug)
        self._failure = None    # set once stdout ends: no further response can come
        self._pump = threading.Thread(target=self._read_stdout, daemon=True,
                                      name="mcp-stdout")
        self._pump.start()

    # ---- the single reader ---------------------------------------------------

    def _read_stdout(self):
        """Own `proc.stdout` and route what comes off it. The ONLY place that
        touches that stream."""
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError as e:
                    # Surfaced to every waiter rather than raised in here: an
                    # exception in a daemon thread reaches the test only as a warning
                    # and an empty sink, which is how this failure mode stayed
                    # undiagnosed.
                    self._fail_everything(
                        f"undecodable line from the server: {line!r} ({e})")
                    return
                self._route(message)
        except Exception as e:  # noqa: BLE001 — must reach the waiters, not the log
            self._fail_everything(f"the stdout reader died: {e!r}")
            return
        self._fail_everything("the server closed stdout without replying")

    def _route(self, message):
        with self._lock:
            pending = self._pending.pop(message.get("id"), None)
            if pending is None:
                self._unrouted.append(message)
                return
        pending.message = message
        pending.done.set()

    def _fail_everything(self, reason):
        with self._lock:
            waiting, self._pending = self._pending, {}
            self._failure = reason
        for pending in waiting.values():
            pending.failure = reason
            pending.done.set()

    # ---- sending ------------------------------------------------------------

    def send_async(self, method, params=None):
        """Fire a request; returns the handle its response will land in."""
        with self._lock:
            self._next_id += 1
            pending = Pending(self._next_id, method)
            # Registered BEFORE the write, because the reply can be routed before
            # write() even returns.
            self._pending[pending.id] = pending
            failure = self._failure
        if failure is not None:
            pending.failure = failure
            pending.done.set()
            return pending
        # ensure_ascii=False, matching Node's JSON.stringify: the real client
        # puts non-ASCII on the wire as UTF-8 BYTES, not as \uXXXX escapes, and
        # an escaping serializer here cannot reach the decoder at all — every
        # payload would be pure ASCII, which any codec accepts (Bugbot, PR #677:
        # the Windows-locale test passed with the fix reverted).
        self.proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": pending.id, "method": method,
            "params": params or {}}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return pending

    def call(self, method, params=None, timeout=10.0):
        """One request/response round trip, through the SAME demultiplexer as
        send_async — a synchronous call overlapping a pending async request used to
        race it for the pipe, so fixing only send_async would have left the bug
        reachable from every other test using this harness."""
        return self.send_async(method, params).result(timeout)

    def initialize(self):
        return self.call("initialize", {"protocolVersion": "2025-06-18",
                                        "capabilities": {},
                                        "clientInfo": {"name": "test"}})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
