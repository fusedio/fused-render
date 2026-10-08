"""Moshi (Kyutai) full-duplex voice chat on Apple Silicon, through `moshi_mlx`.

Not one request, one reply: a SESSION. Moshi listens and speaks at the same
time, 80 ms at a time, so this worker's `generate` does not return until the
conversation ends. It runs under the ordinary streaming `/generate` (held
open for the session's whole life, which is what keeps `GENERATE_LOCK`,
the heartbeat, `/cancel` and the idle-release timer all working unchanged)
and announces, in its first NDJSON chunk, a loopback TCP port the audio
rides on. The server proxies the browser's WebSocket onto that port; the
page never learns it.

Wire frames on the socket, both directions: `kind:u8 · len:u32 · payload`.
  0  mic PCM in   — Int16 little-endian, 24 kHz mono, 1920 samples (80 ms)
  1  Moshi PCM out — same format
  2  text piece out — UTF-8, Moshi's inner monologue as it speaks
  3  end — either side; from the worker the payload is the result JSON

The folder is `moshi_voice`, not `moshi_mlx`: `runners/` sits at
`sys.path[0]` and a sibling named after the pip package would shadow it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json  # noqa: E402
import queue  # noqa: E402
import secrets  # noqa: E402
import socket  # noqa: E402
import struct  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import formats  # noqa: E402
import worker_base  # noqa: E402

SAMPLE_RATE = 24000
FRAME = 1920                 # 80 ms at 24 kHz: one Mimi frame, one LM step
STEPS_PER_SECOND = 12.5
DEFAULT_SECONDS = 300
MAX_SECONDS = 600
#: Mic frames queued but not yet stepped. Past this the oldest are dropped:
#: Moshi runs at real time or not at all, and a backlog only grows.
MAX_LAG_FRAMES = 25
ACCEPT_TIMEOUT_S = 30.0
KEEPALIVE_S = 10.0

KIND_MIC, KIND_OUT, KIND_TEXT, KIND_END = 0, 1, 2, 3
_HEADER = struct.Struct(">BI")
#: SentencePiece ids Moshi emits that are not text: 0 = pad, 3 = end of
#: padding. Upstream's CLI skips exactly these two.
_SILENT_TOKENS = (0, 3)

_loaded = {}
_STREAMS = {}
_STREAMS_LOCK = threading.Lock()


def _pin_stream():
    """The MLX runners' shared stream pin (see `mflux_image`). mlx 0.26 has no
    `new_thread_unsafe_stream`, so this is a no-op there; every MLX call in
    this worker already runs on the one generate thread."""
    import mlx.core as mx

    make = getattr(mx, "new_thread_unsafe_stream", None)
    pin = getattr(mx, "set_default_stream", None)
    if make is None or pin is None:
        return None
    devices = [mx.cpu, mx.default_device()]
    with _STREAMS_LOCK:
        streams = []
        for device in devices:
            key = str(device)
            if key not in _STREAMS:
                _STREAMS[key] = make(device)
            if _STREAMS[key] not in streams:
                streams.append(_STREAMS[key])
    for stream in streams:
        pin(stream)
    return streams


def download(model_id):
    """The whole repo: every Moshi MLX repo holds exactly the three files the
    loader reads (one weight file, the Mimi codec, the text tokenizer)."""
    return worker_base.download_snapshot(model_id)


def load(model_id, path):
    names = os.listdir(path)
    if not formats.is_moshi_snapshot(names):
        raise RuntimeError(f"{model_id} is not a Moshi MLX checkpoint")
    bits = formats.moshi_quant_bits(names)
    try:
        import mlx.core as mx
        import mlx.nn as nn
        import rustymimi
        import sentencepiece
        from moshi_mlx import models, utils
    except ImportError as e:
        raise RuntimeError(f"moshi_mlx could not be imported from {sys.prefix}: {e}") from e
    _pin_stream()
    # No config.json in these repos: upstream's own CLI builds the v0.1
    # config and quantizes BEFORE loading, with the group size the weights
    # were exported at (32 for 4-bit, 64 for 8-bit).
    config_path = os.path.join(path, "config.json")
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as handle:
            lm_config = models.LmConfig.from_config_dict(json.load(handle))
    else:
        lm_config = models.config_v0_1()
    model = models.Lm(lm_config)
    model.set_dtype(mx.bfloat16)
    if bits:
        nn.quantize(model, bits=bits, group_size=32 if bits == 4 else 64)
    model.load_weights(os.path.join(path, formats.moshi_weight_file(names)), strict=True)
    model.warmup()
    mimi = rustymimi.StreamTokenizer(os.path.join(path, formats.MOSHI_MIMI_FILE))
    tokenizer = sentencepiece.SentencePieceProcessor(
        model_file=os.path.join(path, formats.MOSHI_TOKENIZER_FILE))
    _loaded.clear()
    _loaded.update(model=model, mimi=mimi, tokenizer=tokenizer, model_id=model_id,
                   bits=bits, utils=utils, models=models)
    _warm_up()
    worker_base.set_state(device="mps")


def _new_gen(max_steps):
    utils, models = _loaded["utils"], _loaded["models"]
    return models.LmGen(model=_loaded["model"], max_steps=max_steps,
                        text_sampler=utils.Sampler(), audio_sampler=utils.Sampler(),
                        check=False)


def _poll(getter, timeout_s):
    """Mimi encodes and decodes on its own Rust thread; the result is polled.
    None after `timeout_s` — the caller decides whether that is a problem."""
    deadline = time.monotonic() + timeout_s
    while True:
        value = getter()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.001)


def _step(gen, mimi, pcm):
    """One 80 ms step: mic PCM in, (text piece or None, Moshi PCM or None) out."""
    import mlx.core as mx
    import numpy as np

    mimi.encode(pcm)
    codes = _poll(mimi.get_encoded, 2.0)
    if codes is None:
        raise RuntimeError("the Mimi codec stopped answering")
    data = mx.array(codes).transpose(1, 0)[:, :8]
    text_token = gen.step(data)[0].item()
    piece = None
    if text_token not in _SILENT_TOKENS:
        piece = _loaded["tokenizer"].id_to_piece(text_token).replace("▁", " ")
    audio = gen.last_audio_tokens()
    out = None
    if audio is not None:
        mimi.decode(np.array(audio).astype(np.uint32))
        out = _poll(mimi.get_decoded, 2.0)
    return piece, out


def _warm_up():
    """Upstream's `full_warmup`: a few zero frames through codec and model so
    the first real step of a session is not also the first Metal compile."""
    import numpy as np

    gen = _new_gen(16)
    zeros = np.zeros(FRAME, dtype=np.float32)
    for _ in range(4):
        _step(gen, _loaded["mimi"], zeros)


def _mx_memory(name):
    import mlx.core as mx

    for probe in (getattr(mx, name, None), getattr(getattr(mx, "metal", None), name, None)):
        value = probe() if probe else None
        if isinstance(value, int) and value > 0:
            return value
    return None


def memory():
    return _mx_memory("get_active_memory")


def peak_memory():
    return _mx_memory("get_peak_memory")


def release():
    import mlx.core as mx

    clear = getattr(mx, "clear_cache", None)
    if clear is not None:
        clear()


# ------------------------------------------------------------------ session


class _Link:
    """The one audio connection of a session: a loopback listener, a reader
    thread filling `inbox`, a writer thread draining `outbox`."""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.token = secrets.token_hex(16)
        self.conn = None
        self.inbox = queue.Queue()
        self.outbox = queue.Queue()
        self.ended = threading.Event()
        self.flushed = threading.Event()
        self.dropped = 0

    def accept(self, timeout_s):
        self.server.settimeout(timeout_s)
        try:
            conn, _ = self.server.accept()
        except socket.timeout:
            return False
        finally:
            self.server.close()
        conn.settimeout(5.0)
        line = b""
        try:
            while not line.endswith(b"\n") and len(line) < 128:
                chunk = conn.recv(1)
                if not chunk:
                    break
                line += chunk
        except OSError:
            line = b""
        if line.strip().decode(errors="replace") != self.token:
            conn.close()
            return False
        conn.settimeout(None)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.conn = conn
        threading.Thread(target=self._read, name="moshi-read", daemon=True).start()
        threading.Thread(target=self._write, name="moshi-write", daemon=True).start()
        return True

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _read(self):
        try:
            while not self.ended.is_set():
                header = self._recv_exact(_HEADER.size)
                if header is None:
                    break
                kind, length = _HEADER.unpack(header)
                payload = self._recv_exact(length) if length else b""
                if payload is None:
                    break
                if kind == KIND_END:
                    break
                if kind == KIND_MIC:
                    while self.inbox.qsize() >= MAX_LAG_FRAMES:
                        try:
                            self.inbox.get_nowait()
                            self.dropped += 1
                        except queue.Empty:
                            break
                    self.inbox.put(payload)
        except OSError:
            pass
        self.ended.set()

    def _write(self):
        try:
            while True:
                item = self.outbox.get()
                if item is None:
                    break
                kind, payload = item
                self.conn.sendall(_HEADER.pack(kind, len(payload)) + payload)
        except OSError:
            self.ended.set()
        self.flushed.set()

    def send(self, kind, payload):
        self.outbox.put((kind, payload))

    def close(self):
        self.ended.set()
        self.outbox.put(None)
        if self.conn is not None:
            # Give the writer a moment to put the end frame on the wire.
            self.flushed.wait(1.0)
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.conn.close()


def _max_seconds(body):
    try:
        seconds = float(body.get("maxSeconds") or DEFAULT_SECONDS)
    except (TypeError, ValueError):
        seconds = DEFAULT_SECONDS
    return max(10.0, min(float(MAX_SECONDS), seconds))


def generate(body, write):
    """One voice session. Returns when the page hangs up, `/cancel` is
    called, the step cap is reached, or the link breaks. Streaming shape:
    the first chunk names the socket, keepalives follow every 10 s so the
    supervisor's read timeout never fires mid-conversation, `done` closes."""
    import numpy as np

    _pin_stream()
    mimi = _loaded.get("mimi")
    if mimi is None:
        write({"type": "done", "ok": False, "error": "no model is loaded"})
        return
    seconds = _max_seconds(body)
    max_steps = int(seconds * STEPS_PER_SECOND)
    link = _Link()
    write({"type": "chunk", "port": link.port, "token": link.token,
           "sampleRate": SAMPLE_RATE, "frame": FRAME, "maxSeconds": seconds})
    pieces = []
    steps = 0
    started = None
    why = "ended"
    try:
        if not link.accept(ACCEPT_TIMEOUT_S):
            write({"type": "done", "ok": False,
                   "error": "the page never connected to the voice session"})
            return
        gen = _new_gen(max_steps + 5)
        started = time.monotonic()
        last_keepalive = started
        silence = np.zeros(FRAME, dtype=np.float32)
        while True:
            if worker_base.CANCEL.is_set():
                why = "cancelled"
                break
            if link.ended.is_set():
                why = "ended"
                break
            if steps >= max_steps:
                why = "length"
                break
            try:
                raw = link.inbox.get(timeout=0.5)
                pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                if pcm.size != FRAME:
                    pcm = np.resize(pcm, FRAME)
            except queue.Empty:
                # No mic frame in half a second: feed silence so Moshi can
                # still open the conversation, at well under real time.
                pcm = silence
            piece, out = _step(gen, mimi, pcm)
            steps += 1
            if piece:
                pieces.append(piece)
                link.send(KIND_TEXT, piece.encode("utf-8"))
            if out is not None:
                samples = np.clip(np.asarray(out, dtype=np.float32), -1.0, 1.0)
                link.send(KIND_OUT, (samples * 32767.0).astype("<i2").tobytes())
            now = time.monotonic()
            if now - last_keepalive >= KEEPALIVE_S:
                last_keepalive = now
                write({"type": "keepalive", "steps": steps})
        result = {"text": "".join(pieces).strip(), "steps": steps,
                  "seconds": round(time.monotonic() - started, 2) if started else 0.0,
                  "finishReason": "stop" if why == "ended" else why,
                  "droppedFrames": link.dropped}
        link.send(KIND_END, json.dumps(result).encode("utf-8"))
        write({"type": "done", "ok": True, "cancelled": why == "cancelled", "result": result})
    finally:
        link.close()


def main():
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=True, memory=memory, peak_memory=peak_memory,
                      release=release)


if __name__ == "__main__":
    main()
