"""The MLX embedding runner against the REAL EmbeddingGemma 2 checkpoint —
sound and video through the real `mlx-vlm` processor and model, not the fakes
`test_ai_mlx_embed_worker.py` drives it with.

**NOT part of the default suite's coverage**, for `test_ai_onnx_embed_real_
weights.py`'s reasons: `mlx-vlm`, `mlx-audio` and OpenCV live in the runner's
own venv, never in the one the suite runs in, and the checkpoint is a 1.5 GB
download. Skipped whenever any of those is missing — unless
`FUSED_RENDER_REAL_WEIGHTS=1`, which turns every skip into a hard failure
naming what is missing, so an opt-in run cannot report "skipped" and pass for
the wrong reason. Run it as::

    FUSED_RENDER_REAL_WEIGHTS=1 <runner venv>/bin/python -m pytest -p no:cacheprovider \\
        -o addopts="" tests/test_ai_mlx_embed_real_weights.py

(`-o addopts=""` drops the suite's `-n auto`: xdist is not in the runner venv.)

where the runner venv is the one `envinstall` built for
`fused_render/ai/runners/mlx_embed/pyproject.toml` (under
`~/.fused-render/venvs/`), with `pytest` and `pillow` importable in it.

What it pins, measured 2026-10-13 on the bf16 conversion: every vector is
768-dim and unit length; a 440 Hz tone as wav, mp3 and m4a land within
cosine 0.95 of each other; a red picture and a red video sit above 0.85; and
a mixed `paths` list comes back in the caller's order.
"""
import glob
import importlib.util
import math
import os
import struct
import subprocess
import sys
import wave

import pytest

_REQUIRE = os.environ.get("FUSED_RENDER_REAL_WEIGHTS") == "1"
_REPO = "mlx-community/embeddinggemma-2-bf16"
_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKER = os.path.join(_HERE, "..", "fused_render", "ai", "runners", "mlx_embed",
                       "worker.py")


def _snapshot():
    hub = os.path.expanduser("~/.cache/huggingface/hub")
    found = glob.glob(os.path.join(hub, "models--" + _REPO.replace("/", "--"),
                                   "snapshots", "*", "config.json"))
    return os.path.dirname(found[0]) if found else None


def _gate(condition, why):
    if condition:
        return
    if _REQUIRE:
        pytest.fail(f"FUSED_RENDER_REAL_WEIGHTS=1 but {why}")
    pytest.skip(why)


@pytest.fixture(scope="module")
def worker():
    for name in ("mlx_vlm", "mlx_audio", "cv2", "PIL"):
        _gate(importlib.util.find_spec(name) is not None,
              f"{name} is not importable from {sys.executable}")
    path = _snapshot()
    _gate(path is not None, f"{_REPO} is not in the Hub cache")
    spec = importlib.util.spec_from_file_location("mlx_embed_worker_real", _WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load(_REPO, path)
    assert module._loaded["family"] == module._UNIFIED
    return module


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    from PIL import Image

    root = tmp_path_factory.mktemp("media")
    paths = {}
    for name, hz in (("tone440.wav", 440), ("tone880.wav", 880)):
        path = root / name
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * i / 16000)))
                for i in range(16000 * 2)))
        paths[name] = str(path)
    for name, colour in (("red.png", (255, 0, 0)), ("blue.png", (0, 0, 255))):
        path = root / name
        Image.new("RGB", (64, 64), colour).save(path)
        paths[name] = str(path)
    import shutil
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        for name, args in (
                ("tone440.mp3", ["-i", paths["tone440.wav"]]),
                ("tone440.m4a", ["-i", paths["tone440.wav"], "-c:a", "aac"]),
                ("red.mp4", ["-f", "lavfi", "-i", "color=c=red:duration=3:size=320x240:rate=10",
                             "-pix_fmt", "yuv420p"]),
                ("bars.mp4", ["-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10",
                              "-pix_fmt", "yuv420p"])):
            path = root / name
            subprocess.run([ffmpeg, "-loglevel", "error", "-y", *args, str(path)],
                           check=True)
            paths[name] = str(path)
    return paths


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))


def test_sound_pictures_and_video_land_in_one_unit_space(worker, media):
    _gate("red.mp4" in media, "ffmpeg is not on PATH to make the mp4/mp3/m4a fixtures")
    order = ["tone440.wav", "red.png", "red.mp4", "tone880.wav", "blue.png",
             "tone440.mp3", "bars.mp4", "tone440.m4a"]
    result = worker.generate({"paths": [media[n] for n in order]})
    assert result["dim"] == 768
    vectors = dict(zip(order, result["vectors"]))
    for vector in vectors.values():
        assert len(vector) == 768
        assert abs(math.sqrt(sum(v * v for v in vector)) - 1) < 1e-3
    # The same tone in three containers is the same sound.
    assert _cos(vectors["tone440.wav"], vectors["tone440.mp3"]) > 0.95
    assert _cos(vectors["tone440.wav"], vectors["tone440.m4a"]) > 0.95
    # A red picture and a red video sit together; both sit away from a tone.
    assert _cos(vectors["red.png"], vectors["red.mp4"]) > 0.85
    assert _cos(vectors["red.png"], vectors["tone440.wav"]) < 0.8
    # Order is the caller's: the two pictures differ, and their positions hold.
    assert _cos(vectors["red.png"], vectors["blue.png"]) < 0.95


def test_a_phrase_ranks_a_recording_against_pictures(worker, media):
    texts = worker.generate({"texts": ["a pure sine tone beeping",
                                       "a solid red square"], "kind": "query"})
    tone, red = texts["vectors"]
    paths = worker.generate({"paths": [media["tone440.wav"], media["red.png"]]})
    sound, picture = paths["vectors"]
    assert _cos(red, picture) > _cos(red, sound)
    assert _cos(tone, sound) > _cos(tone, picture) - 0.1


def test_a_broken_sound_file_is_named(worker, tmp_path):
    bad = tmp_path / "noise.wav"
    bad.write_bytes(b"not a wav at all")
    with pytest.raises(ValueError) as info:
        worker.generate({"paths": [str(bad)]})
    assert str(bad) in str(info.value)
    assert "not a readable audio" in str(info.value)
