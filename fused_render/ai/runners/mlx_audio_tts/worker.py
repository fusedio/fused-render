import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contextlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import wave  # noqa: E402

import formats  # noqa: E402
import worker_base  # noqa: E402

_loaded = {}

_STREAMS = {}
_STREAMS_LOCK = threading.Lock()

TOKENS_PER_SECOND = 12
CHUNK_CHARS = 600
PARAGRAPH_PAUSE_S = 0.5
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])")


def _pin_stream():
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
    return worker_base.download_snapshot(model_id)


def _read_config(path):
    try:
        with open(os.path.join(path, "config.json"), encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def load(model_id, path):
    traits = formats.speech_traits(_read_config(path))
    if traits is None:
        raise RuntimeError(f"{model_id} is not a Qwen3-TTS checkpoint")
    try:
        from mlx_audio.tts.utils import load_model
    except ImportError as e:
        raise RuntimeError(f"mlx-audio could not be imported from {sys.prefix}: {e}") from e
    _pin_stream()
    model = load_model(path)
    _loaded.clear()
    _loaded.update(model=model, model_id=model_id, traits=traits)
    worker_base.set_state(device="mps")


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


def _sentences(paragraph, limit):
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit) + 1 or limit
            yield sentence[:cut]
            sentence = sentence[cut:]
        yield sentence


def speech_chunks(text, limit=CHUNK_CHARS):
    paragraphs = []
    for block in _PARAGRAPH_BREAK.split(text):
        chunks, current = [], ""
        for sentence in _sentences(" ".join(block.split()), limit):
            if current.strip() and len(current) + len(sentence) > limit:
                chunks.append(current.strip())
                current = ""
            current += sentence
        if current.strip():
            chunks.append(current.strip())
        if chunks:
            paragraphs.append(chunks)
    return paragraphs


class _Progress:
    def __init__(self, job, total):
        self.job = job
        self.total = total
        self.done = 0
        self.tokens = 0

    def __call__(self, *_args, **_kwargs):
        return self

    def update(self, n=1):
        seconds = self.tokens // TOKENS_PER_SECOND
        self.tokens += n
        if self.tokens // TOKENS_PER_SECOND == seconds:
            return
        worker_base.report_or_cancel(
            job=self.job, kind="task", unit="", done=self.done, total=self.total,
            detail="Part %d/%d · %ds of audio" % (
                self.done + 1, self.total, self.tokens // TOKENS_PER_SECOND))

    def close(self, *_args, **_kwargs):
        pass


@contextlib.contextmanager
def _progress_hook(progress):
    from mlx_audio.tts.models.qwen3_tts import qwen3_tts

    if not hasattr(qwen3_tts, "tqdm"):
        raise RuntimeError("mlx-audio's qwen3_tts has no `tqdm` to hook for progress")
    original = qwen3_tts.tqdm
    qwen3_tts.tqdm = progress
    try:
        yield
    finally:
        qwen3_tts.tqdm = original


def _write_wav(path, samples, sample_rate):
    import numpy as np

    pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    tmp = path + ".part"
    with wave.open(tmp, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(pcm.tobytes())
    os.replace(tmp, path)


def generate(body):
    _pin_stream()
    model = _loaded.get("model")
    if model is None:
        raise RuntimeError("no model is loaded")
    out = str(body.get("out") or "")
    paragraphs = speech_chunks(str(body.get("text") or ""))
    if not paragraphs or not out:
        raise ValueError("'text' and 'out' are required")
    options = formats.speech_options(_loaded["model_id"], _loaded["traits"], body)

    import numpy as np

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    started = time.time()
    job = body.get("job") or None
    progress = _Progress(job, sum(len(chunks) for chunks in paragraphs))
    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=progress.total, detail="Speaking")
    sample_rate = model.sample_rate
    pause = np.zeros(int(sample_rate * PARAGRAPH_PAUSE_S), dtype=np.float32)
    pieces = []
    with _progress_hook(progress):
        for index, chunks in enumerate(paragraphs):
            if index and pieces:
                pieces.append(pause)
            for chunk in chunks:
                for result in model.generate(
                        text=chunk, lang_code=options["language"], split_pattern="",
                        voice=options.get("voice"), instruct=options.get("instruct"),
                        ref_audio=options.get("refAudio"), ref_text=options.get("refText"),
                        verbose=False):
                    pieces.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
                progress.done += 1
    if not any(piece is not pause and piece.size for piece in pieces):
        raise RuntimeError(f"{_loaded['model_id']} returned no audio for this text")
    audio = np.concatenate(pieces)
    _write_wav(out, audio, sample_rate)
    return {"path": out, "seconds": round(time.time() - started, 2),
            "audioSeconds": round(audio.size / sample_rate, 2), "parts": progress.total,
            **options}


def main():
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory, peak_memory=peak_memory,
                      release=release)


if __name__ == "__main__":
    main()
