"""Text splitting and WAV writing shared by the two text-to-speech workers.

`mlx_audio_tts` (Qwen3-TTS) and `sherpa_tts` (Kokoro) are different engines
behind one request shape (`formats.speech_options`), so the parts of
`generate()` that have nothing to do with a model live here once: blank lines
start a paragraph (a pause follows it), sentences are packed into parts of at
most `CHUNK_CHARS`, and the result is one mono 16-bit WAV.

Stdlib at import; numpy is imported inside `write_wav`, and this module never
imports `worker_base` or `fused_render` (both workers load it through the same
`sys.path` insert that reaches `formats`).
"""

import os
import re
import wave

CHUNK_CHARS = 600
PARAGRAPH_PAUSE_S = 0.5
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])")


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


def write_wav(path, samples, sample_rate):
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
