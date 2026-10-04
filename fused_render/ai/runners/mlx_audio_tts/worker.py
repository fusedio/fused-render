import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import wave  # noqa: E402

import formats  # noqa: E402
import worker_base  # noqa: E402

_loaded = {}

_STREAMS = {}
_STREAMS_LOCK = threading.Lock()

SPLIT_PATTERN = "\n\n"
TOKENS_PER_SECOND = 12
_SEGMENT = re.compile(r"Segment (\d+)/(\d+)")


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


def load(model_id, path):
    try:
        from mlx_audio.tts.utils import load_model
    except ImportError as e:
        raise RuntimeError(f"mlx-audio could not be imported from {sys.prefix}: {e}") from e
    _pin_stream()
    model = load_model(path)
    config = getattr(model, "config", None)
    mode = formats.SPEECH_VOICE_MODES.get(str(getattr(config, "tts_model_type", "base")))
    if getattr(config, "model_type", None) != formats.QWEN3_TTS_MODEL_TYPE or mode is None:
        raise RuntimeError(f"{model_id} is not a Qwen3-TTS checkpoint")
    speakers = [str(v) for v in getattr(model, "supported_speakers", None) or []]
    _loaded.clear()
    _loaded.update(
        model=model, model_id=model_id, mode=mode, speakers=speakers,
        languages=[str(v) for v in getattr(model, "supported_languages", None) or []])
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


class _TokenBar:
    def __init__(self, job, desc):
        self.job = job
        match = _SEGMENT.search(desc or "")
        self.segment = int(match.group(1)) if match else 1
        self.segments = int(match.group(2)) if match else 1
        self.tokens = 0

    def update(self, n=1):
        self.tokens += n
        if self.tokens % TOKENS_PER_SECOND:
            return
        worker_base.report_or_cancel(
            job=self.job, kind="task", unit="", done=self.segment - 1,
            total=self.segments,
            detail="Segment %d/%d · %ds of audio" % (
                self.segment, self.segments, self.tokens // TOKENS_PER_SECOND))

    def close(self, *_args, **_kwargs):
        pass

    set_description = close


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
    text = str(body.get("text") or "")
    out = str(body.get("out") or "")
    if not text.strip() or not out:
        raise ValueError("'text' and 'out' are required")
    language = str(body.get("language") or "auto").lower()
    opts = {k: body.get(k) or None for k in ("voice", "instruct", "refAudio", "refText")}
    problem = formats.speech_option_error(
        _loaded["model_id"], _loaded["mode"], _loaded["speakers"], _loaded["languages"],
        voice=opts["voice"], instruct=opts["instruct"], ref_audio=opts["refAudio"],
        ref_text=opts["refText"], language=language)
    if problem:
        raise ValueError(problem)
    if _loaded["mode"] == "preset" and not opts["voice"] and _loaded["speakers"]:
        opts["voice"] = _loaded["speakers"][0]

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    started = time.time()
    job = body.get("job") or None
    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=1, detail="Speaking")

    import numpy as np
    from mlx_audio.tts.models.qwen3_tts import qwen3_tts

    if not hasattr(qwen3_tts, "tqdm"):
        raise RuntimeError("mlx-audio's qwen3_tts has no `tqdm` to hook for progress")
    original_tqdm = qwen3_tts.tqdm
    qwen3_tts.tqdm = lambda *_a, desc=None, **_k: _TokenBar(job, desc)
    try:
        results = list(model.generate(
            text=text, lang_code=language, split_pattern=SPLIT_PATTERN, verbose=False,
            voice=opts["voice"], instruct=opts["instruct"],
            ref_audio=opts["refAudio"], ref_text=opts["refText"]))
    finally:
        qwen3_tts.tqdm = original_tqdm
    if not results:
        raise RuntimeError(f"{_loaded['model_id']} returned no audio for this text")

    sample_rate = int(getattr(results[0], "sample_rate", 0) or model.sample_rate)
    audio = np.concatenate([np.asarray(r.audio, dtype=np.float32).reshape(-1) for r in results])
    _write_wav(out, audio, sample_rate)
    return {"path": out, "seconds": round(time.time() - started, 2),
            "audioSeconds": round(len(audio) / sample_rate, 2), "segments": len(results),
            **{k: v for k, v in opts.items() if v}}


def main():
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory, peak_memory=peak_memory,
                      release=release)


if __name__ == "__main__":
    main()
