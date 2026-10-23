"""Text to speech with Kokoro-82M on sherpa-onnx (`sherpa-tts`).

The cross-platform sibling of `mlx_audio_tts`: same request, same reply, same
WAV, but a CPU engine that runs wherever sherpa-onnx has a wheel. Preset
voices only — Kokoro has no style line and cannot clone or design a voice.

Stdlib and numpy at import; `sherpa_onnx` is imported inside `load` so a
broken wheel is a load error with a message, not a worker that never starts.

**Language.** The export picks the language from the voice (English voices
speak English, `z*` voices Chinese), so `language` is accepted, validated by
`formats.speech_options`, and handed back in the reply — it changes nothing
about the audio.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time  # noqa: E402

import formats  # noqa: E402
import speech_common  # noqa: E402
import worker_base  # noqa: E402

_loaded = {}

#: CPU threads for the engine, the cap `../diarize.py` uses: past four cores a
#: short utterance stops getting faster and the rest of the machine slows down.
NUM_THREADS = min(4, os.cpu_count() or 1)
#: Kokoro's speaking rate; the request has no speed option, so 1.0 always.
SPEED = 1.0


def download(model_id):
    """The whole repo (~0.39 GB): every file in it is read by the engine."""
    return worker_base.download_snapshot(model_id)


def _entries(path):
    names = set(os.listdir(path))
    return names, {name for name in names if os.path.isdir(os.path.join(path, name))}


def load(model_id, path):
    names, dirnames = _entries(path)
    traits = formats.kokoro_traits_from_names(names, dirnames)
    if traits is None:
        raise RuntimeError(
            f"{model_id} is not a Kokoro sherpa-onnx export: it needs "
            f"{', '.join(formats.KOKORO_FILES)}, a lexicon-*.txt and the "
            f"{formats.KOKORO_DATA_DIR}/ and "
            f"{formats.KOKORO_DICT_DIR}/ folders beside them")
    try:
        import sherpa_onnx
    except ImportError as e:
        raise RuntimeError(f"sherpa-onnx could not be imported from {sys.prefix}: {e}") from e
    lexicon = ",".join(os.path.join(path, name) for name in formats.kokoro_lexicons(names))
    if not lexicon:
        raise RuntimeError(f"{model_id} is not a Kokoro sherpa-onnx export: it has no lexicon-*.txt")
    kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
        model=os.path.join(path, "model.onnx"),
        voices=os.path.join(path, "voices.bin"),
        tokens=os.path.join(path, "tokens.txt"),
        data_dir=os.path.join(path, formats.KOKORO_DATA_DIR),
        dict_dir=os.path.join(path, formats.KOKORO_DICT_DIR),
        lexicon=lexicon)
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=kokoro, num_threads=NUM_THREADS, provider="cpu", debug=False),
        max_num_sentences=1)
    tts = sherpa_onnx.OfflineTts(config)
    _loaded.clear()
    _loaded.update(model=tts, model_id=model_id, traits=traits)
    # CPU everywhere, Apple Silicon included — see this folder's pyproject.toml.
    worker_base.set_state(device="cpu")


def memory():
    """RSS: the weights live in ordinary process memory on the CPU provider."""
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:  # noqa: BLE001 - a memory probe must never break /health
        return None


def generate(body):
    tts = _loaded.get("model")
    if tts is None:
        raise RuntimeError("no model is loaded")
    out = str(body.get("out") or "")
    paragraphs = speech_common.speech_chunks(str(body.get("text") or ""))
    if not paragraphs or not out:
        raise ValueError("'text' and 'out' are required")
    model_id = _loaded["model_id"]
    options = formats.speech_options(model_id, _loaded["traits"], body)
    sid = formats.KOKORO_VOICES.index(options["voice"])

    import numpy as np

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    started = time.time()
    job = body.get("job") or None
    total = sum(len(chunks) for chunks in paragraphs)
    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=total, detail="Speaking")
    sample_rate = None
    pause = None
    pieces = []
    spoken = 0
    done = 0
    for index, chunks in enumerate(paragraphs):
        if index and pieces:  # a paragraph break; `pause` exists once a part has run
            pieces.append(pause)
        for chunk in chunks:
            if worker_base.CANCEL.is_set():
                raise worker_base.Cancelled()
            worker_base.report_or_cancel(
                job=job, kind="task", unit="", done=done, total=total,
                detail="Part %d/%d · %ds of audio" % (
                    done + 1, total, spoken // sample_rate if sample_rate else 0))
            audio = tts.generate(chunk, sid=sid, speed=SPEED)
            samples = np.asarray(audio.samples, dtype=np.float32).reshape(-1)
            if sample_rate is None:
                sample_rate = int(audio.sample_rate)
                pause = np.zeros(int(sample_rate * speech_common.PARAGRAPH_PAUSE_S),
                                 dtype=np.float32)
            pieces.append(samples)
            spoken += samples.size
            done += 1
    if not spoken:
        raise RuntimeError(f"{model_id} returned no audio for this text")
    audio = np.concatenate(pieces)
    speech_common.write_wav(out, audio, sample_rate)
    return {"path": out, "seconds": round(time.time() - started, 2),
            "audioSeconds": round(audio.size / sample_rate, 2), "parts": total,
            **options}


def main():
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)


if __name__ == "__main__":
    main()
