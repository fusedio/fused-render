"""_serve_pipe resilience: one hostile or broken client must not kill the
single-instance IPC thread for the rest of the supervisor's life."""
import queue
import struct
import threading
import uuid

import pytest

pytest.importorskip("win32pipe")

import pywintypes
import win32pipe

from fused_render.win_supervisor import instance, protocol

_MAGIC = 0x3153_5246


def _frame(opcode: int, payload: bytes) -> bytes:
    return struct.pack("<IHHI", _MAGIC, 1, opcode, len(payload) // 2) + payload


def _call(pipe: str, frame: bytes, timeout_ms: int = 5000) -> int:
    (status,) = struct.unpack("<I", win32pipe.CallNamedPipe(pipe, frame, 4, timeout_ms))
    return status


@pytest.fixture
def served():
    names = instance.InstanceNames.with_suffix(f"test-{uuid.uuid4().hex[:8]}")
    requests: "queue.Queue[instance.Request]" = queue.Queue()
    stop = threading.Event()
    logs = []
    thread = threading.Thread(
        target=instance._serve_pipe, args=(names, requests, stop, logs.append), daemon=True
    )
    thread.start()
    yield names, requests, thread, logs
    stop.set()
    try:
        win32pipe.CallNamedPipe(names.pipe, b"\x00", 4, 250)
    except pywintypes.error:
        pass
    thread.join(timeout=5)


def _serve_next_ok(requests):
    def answer():
        requests.get(timeout=5).response.put(0)

    threading.Thread(target=answer, daemon=True).start()


def test_decode_rejects_unpaired_surrogate_as_protocol_error():
    # UnicodeDecodeError from utf-16-le must surface as ProtocolError so
    # _serve_pipe's existing narrow handler already covers it.
    with pytest.raises(protocol.ProtocolError):
        protocol.decode(_frame(1, b"\x00\xd8"))  # lone high surrogate


def test_hostile_utf16_frame_does_not_kill_the_pipe_thread(served):
    names, requests, thread, _logs = served
    assert _call(names.pipe, _frame(1, b"\x00\xd8")) == 1  # rejected, not fatal
    _serve_next_ok(requests)
    assert _call(names.pipe, protocol.encode(protocol.OpenHome())) == 0
    assert thread.is_alive()


def test_unexpected_decode_exception_does_not_kill_the_pipe_thread(served, monkeypatch):
    # The broad per-connection catch: even a non-ProtocolError blowup while
    # handling one client only drops that client.
    names, requests, thread, logs = served
    real_decode = protocol.decode

    def flaky(frame):
        if frame == b"BOOM":
            raise RuntimeError("boom")
        return real_decode(frame)

    monkeypatch.setattr(protocol, "decode", flaky)
    with pytest.raises(pywintypes.error):
        _call(names.pipe, b"BOOM", 2000)  # transaction aborted mid-flight
    _serve_next_ok(requests)
    assert _call(names.pipe, protocol.encode(protocol.OpenHome())) == 0
    assert thread.is_alive()
    assert any("pipe client handling failed" in line for line in logs)
