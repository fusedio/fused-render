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


def _mlx_audio_load():
    try:
        from mlx_audio.tts.utils import load_model
    except ImportError as e:
        raise RuntimeError(
            f"mlx-audio could not be imported from the runner environment at "
            f"{sys.prefix} ({e.__class__.__name__}: {e}). That is an "
            "environment failure rather than a problem with this model."
        ) from e
    return load_model


def load(model_id, path):
    load_model = _mlx_audio_load()
    _pin_stream()
    model = load_model(path)
    config = getattr(model, "config", None)
    if getattr(config, "model_type", "qwen3_tts") != "qwen3_tts":
        raise RuntimeError(f"{model_id} is not a Qwen3-TTS checkpoint")
    mode = formats.SPEECH_VOICE_MODES.get(str(getattr(config, "tts_model_type", "base")))
    if mode is None:
        raise RuntimeError(f"{model_id} has an unknown Qwen3-TTS variant")
    _loaded.clear()
    speakers = [str(v) for v in (getattr(model, "supported_speakers", None) or [])]
    _loaded.update(
        model=model,
        model_id=model_id,
        mode=mode,
        speakers=speakers,
        voices=[v.lower() for v in speakers],
        languages=[str(v).lower() for v in (getattr(model, "supported_languages", None) or [])],
    )
    worker_base.set_state(device="mps")


def memory():
    import mlx.core as mx

    for probe in (getattr(mx, "get_active_memory", None),
                  getattr(getattr(mx, "metal", None), "get_active_memory", None)):
        if probe is None:
            continue
        value = probe()
        if isinstance(value, int) and value > 0:
            return value
    return None


def peak_memory():
    import mlx.core as mx

    for probe in (getattr(mx, "get_peak_memory", None),
                  getattr(getattr(mx, "metal", None), "get_peak_memory", None)):
        if probe is None:
            continue
        value = probe()
        if isinstance(value, int) and value > 0:
            return value
    return None


def release():
    import mlx.core as mx

    clear = getattr(mx, "clear_cache", None)
    if clear is not None:
        clear()


def _assert_tqdm_hook_exists(module):
    if not hasattr(module, "tqdm"):
        raise RuntimeError(
            "mlx_audio.tts.models.qwen3_tts.qwen3_tts has no module-level "
            "`tqdm` to hook for progress; the pinned mlx-audio changed")


class _TokenTicker:
    def __init__(self, job):
        self.job = job

    def __call__(self, *_args, desc=None, **_kwargs):
        return _TokenBar(self.job, desc)


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

    def set_description(self, *_args, **_kwargs):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


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
    voice = body.get("voice") or None
    instruct = body.get("instruct") or None
    ref_audio = body.get("refAudio") or None
    ref_text = body.get("refText") or None
    language = str(body.get("language") or "auto")
    out = str(body.get("out") or "")
    job = body.get("job") or None
    if not text.strip():
        raise ValueError("'text' must not be empty")
    if not out:
        raise ValueError("'out' must be the path to write the audio to")
    problem = formats.speech_option_error(
        _loaded["model_id"], _loaded["mode"], _loaded["voices"], _loaded["languages"],
        voice=voice, instruct=instruct, ref_audio=ref_audio, ref_text=ref_text,
        language=language)
    if problem:
        raise ValueError(problem)

    if _loaded["mode"] == "preset" and not voice and _loaded["speakers"]:
        voice = _loaded["speakers"][0]

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    started = time.time()
    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=1, detail="Speaking")

    import numpy as np
    from mlx_audio.tts.models.qwen3_tts import qwen3_tts

    _assert_tqdm_hook_exists(qwen3_tts)
    original_tqdm = qwen3_tts.tqdm
    qwen3_tts.tqdm = _TokenTicker(job)
    try:
        kwargs = {"text": text, "lang_code": language.lower(),
                  "split_pattern": SPLIT_PATTERN, "verbose": False}
        if voice:
            kwargs["voice"] = voice
        if instruct:
            kwargs["instruct"] = instruct
        if ref_audio:
            kwargs["ref_audio"] = ref_audio
            kwargs["ref_text"] = ref_text
        results = list(model.generate(**kwargs))
    finally:
        qwen3_tts.tqdm = original_tqdm
    if not results:
        raise RuntimeError(f"{_loaded['model_id']} returned no audio for this text")

    sample_rate = int(getattr(results[0], "sample_rate", 0) or getattr(model, "sample_rate", 24000))
    audio = np.concatenate([np.asarray(r.audio, dtype=np.float32).reshape(-1) for r in results])
    _write_wav(out, audio, sample_rate)

    reply = {
        "path": out,
        "seconds": round(time.time() - started, 2),
        "sampleRate": sample_rate,
        "audioSeconds": round(len(audio) / sample_rate, 2),
        "segments": len(results),
        "text": text,
        "language": language,
    }
    for key, value in (("voice", voice), ("instruct", instruct),
                       ("refAudio", ref_audio), ("refText", ref_text)):
        if value is not None:
            reply[key] = value
    return reply


def main():
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory, peak_memory=peak_memory,
                      release=release)


if __name__ == "__main__":
    main()
