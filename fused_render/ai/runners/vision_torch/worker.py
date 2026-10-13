"""Image understanding on torch (SPEC AI-33): caption, query, detect, point, segment.

Started by `fused_render.ai.supervisor` on the interpreter built from this
folder's `pyproject.toml`; the HTTP contract and download reporting are
`worker_base`'s. Which repos load, and which tasks each answers, is
`formats.VISION_TORCH_RECIPES`; a family class below runs each kind.
"""

import os
import sys

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402

import formats  # noqa: E402 - the recipe table, shared with the server
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: Every task the vision verb names; a recipe answers a subset.
TASKS = ("caption", "query", "detect", "point", "segment")
CAPTION_LENGTHS = ("short", "normal", "long")
DEFAULT_MAX_OBJECTS = 50
#: Grounding DINO's own defaults for keeping a box and naming it.
BOX_THRESHOLD = 0.3
TEXT_THRESHOLD = 0.25

_loaded = {}


def recipe(model_id):
    spec = formats.vision_torch_recipe(model_id)
    if spec is None:
        raise ValueError(
            f"{model_id!r} is not a vision model this runner knows; "
            f"known: {', '.join(sorted(formats.VISION_TORCH_RECIPES))}")
    return spec


def _device(half):
    import torch

    if torch.backends.mps.is_available():
        return "mps", torch.bfloat16 if half else torch.float32
    if torch.cuda.is_available():
        return "cuda", torch.bfloat16 if half else torch.float32
    return "cpu", torch.float32


def _open(path):
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as im:
            return ImageOps.exif_transpose(im).convert("RGB")
    except FileNotFoundError:
        raise ValueError(f"image not found: {path}") from None
    except OSError as e:
        raise ValueError(f"image not readable: {path} ({e})") from None


def _clamp(v):
    return min(max(float(v), 0.0), 1.0)


# ------------------------------------------------------------------ families


class _Moondream:
    """Moondream's own torch code from the pinned snapshot; no transformers wrapper."""

    def __init__(self, model_id, spec, path):
        import torch
        from safetensors.torch import load_file

        if spec["revision"] and os.path.basename(os.path.normpath(path)) != spec["revision"]:
            raise RuntimeError(
                f"{model_id} resolved to {path}, not the pinned commit {spec['revision']}")
        self.device, dtype = _device(half=True)
        md, cfg_mod = self._import_snapshot(path, f"_fused_vision_{spec['family']}")
        md.Tokenizer = self._local_tokenizer(spec["tokenizer"])
        with open(os.path.join(path, "config.json")) as f:
            cfg = cfg_mod.MoondreamConfig.from_dict(json.load(f).get("config") or {})
        model = md.MoondreamModel(cfg, dtype=dtype, setup_caches=False)
        weights = {k.removeprefix("model."): v
                   for k, v in load_file(os.path.join(path, "model.safetensors")).items()}
        missing, unexpected = model.load_state_dict(weights, strict=False)
        if missing or unexpected:
            raise RuntimeError(
                f"{model_id} weights do not match its code: "
                f"{len(missing)} missing, {len(unexpected)} unexpected tensors")
        model.to(self.device)
        model._setup_caches()
        model.eval()
        torch.set_grad_enabled(False)
        self.model = model
        self._key, self._encoded = None, None

    @staticmethod
    def _import_snapshot(path, name):
        pkg = types.ModuleType(name)
        pkg.__path__ = [path]
        sys.modules[name] = pkg
        return (importlib.import_module(f"{name}.moondream"),
                importlib.import_module(f"{name}.config"))

    @staticmethod
    def _local_tokenizer(repo_id):
        """Tokenizer.from_pretrained, served from the snapshot `download` fetched."""
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        path = os.path.join(snapshot_download(repo_id, local_files_only=True), "tokenizer.json")

        class _Local:
            @staticmethod
            def from_pretrained(*_a, **_k):
                return Tokenizer.from_file(path)

        return _Local

    def _encode(self, path):
        # Encoding is most of a call; reuse it while requests keep naming one file.
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        if self._key != key:
            image = _open(path)
            self._key, self._encoded = key, (self.model.encode_image(image), image.size)
        return self._encoded

    def run(self, task, path, prompt, opts):
        encoded, (width, height) = self._encode(path)
        out = {"width": width, "height": height}
        if task == "caption":
            out["text"] = self.model.caption(encoded, length=opts["length"])["caption"]
        elif task == "query":
            reply = self.model.query(encoded, prompt, reasoning=opts["reasoning"])
            out["text"] = reply["answer"]
            reasoning = reply.get("reasoning")
            if reasoning:
                out["reasoning"] = reasoning.get("text") if isinstance(reasoning, dict) else str(reasoning)
        elif task == "detect":
            # encode_image reads settings["variant"] unguarded, so it must be present.
            settings = {"variant": None, "max_objects": opts["maxObjects"]}
            found = self.model.detect(encoded, prompt, settings=settings)["objects"]
            out["objects"] = [{"xMin": _clamp(o["x_min"]), "yMin": _clamp(o["y_min"]),
                               "xMax": _clamp(o["x_max"]), "yMax": _clamp(o["y_max"]),
                               "label": prompt, "score": None} for o in found]
        elif task == "point":
            found = self.model.point(encoded, prompt)["points"][: opts["maxObjects"]]
            out["points"] = [{"x": _clamp(p["x"]), "y": _clamp(p["y"]), "label": prompt}
                             for p in found]
        return out


class _GroundingDino:
    """Open-vocabulary detection through transformers' Grounding DINO."""

    def __init__(self, model_id, spec, path):
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        self.device, _ = _device(half=False)
        self.processor = AutoProcessor.from_pretrained(path)
        self.model = AutoModelForZeroShotObjectDetection.from_pretrained(path).to(self.device).eval()
        torch.set_grad_enabled(False)

    def run(self, task, path, prompt, opts):
        image = _open(path)
        width, height = image.size
        # One phrase per label, the "a. b." form the model was trained on.
        labels = [p.strip().lower() for p in re.split(r"[,.;\n]", prompt) if p.strip()]
        inputs = self.processor(images=image, text=". ".join(labels) + ".",
                                return_tensors="pt").to(self.device)
        result = self.processor.post_process_grounded_object_detection(
            self.model(**inputs), inputs.input_ids, threshold=BOX_THRESHOLD,
            text_threshold=TEXT_THRESHOLD, target_sizes=[(height, width)])[0]
        names = result.get("text_labels") or result.get("labels") or []
        found = sorted(zip(result["boxes"].tolist(), result["scores"].tolist(), names),
                       key=lambda t: -t[1])[: opts["maxObjects"]]
        objects = [{"xMin": _clamp(b[0] / width), "yMin": _clamp(b[1] / height),
                    "xMax": _clamp(b[2] / width), "yMax": _clamp(b[3] / height),
                    "label": str(name), "score": round(float(score), 4)}
                   for b, score, name in found]
        return {"width": width, "height": height, "objects": objects}


_FAMILIES = {"moondream": _Moondream, "grounding-dino": _GroundingDino}


# --------------------------------------------------------------- model loading


def download(model_id):
    spec = recipe(model_id)
    if spec.get("tokenizer"):
        worker_base.download_snapshot(spec["tokenizer"], allow_patterns=["*.json"])
    if spec["revision"]:
        return worker_base.download_snapshot(
            model_id, allow_patterns=spec["allow"], revision=spec["revision"])
    return worker_base.download_snapshot(model_id, allow_patterns=spec["allow"])


def load(model_id, path):
    spec = recipe(model_id)
    family = _FAMILIES[spec["family"]](model_id, spec, path)
    worker_base.set_state(device=family.device)
    _loaded.clear()
    _loaded.update(family=family, model_id=model_id, tasks=tuple(spec["tasks"]))


def memory():
    import torch

    total = 0
    if torch.backends.mps.is_available():
        try:
            total += int(torch.mps.current_allocated_memory())
        except (RuntimeError, OSError):
            pass
    if torch.cuda.is_available():
        try:
            total += int(torch.cuda.memory_allocated())
        except (RuntimeError, OSError):
            pass
    return total or None


def release():
    import torch

    if torch.backends.mps.is_available():
        try:
            torch.mps.empty_cache()
        except (RuntimeError, OSError):
            pass
    if torch.cuda.is_available():
        try:
            torch.cuda.empty_cache()
        except (RuntimeError, OSError):
            pass


# ------------------------------------------------------------------ answering


def validate_request(body):
    """`(task, image, prompt, options)` or a ValueError naming the field at fault."""
    if not isinstance(body, dict):
        raise ValueError("the request body must be an object")
    task = body.get("task")
    if task not in TASKS:
        raise ValueError(f"'task' must be one of {', '.join(TASKS)}")
    image = body.get("image")
    if not isinstance(image, str) or not os.path.isabs(image):
        raise ValueError("'image' must be an absolute path")
    prompt = body.get("prompt")
    if task == "caption":
        if prompt not in (None, ""):
            raise ValueError("a caption takes no 'prompt'")
    elif not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"a {task} needs a 'prompt'")
    length = body.get("length") or "normal"
    if length not in CAPTION_LENGTHS:
        raise ValueError(f"'length' must be one of {', '.join(CAPTION_LENGTHS)}")
    max_objects = body.get("maxObjects") or DEFAULT_MAX_OBJECTS
    if not isinstance(max_objects, int) or isinstance(max_objects, bool) or max_objects < 1:
        raise ValueError("'maxObjects' must be a positive integer")
    return task, image, (prompt or "").strip(), {
        "length": length, "reasoning": bool(body.get("reasoning")), "maxObjects": max_objects}


def generate(body):
    family = _loaded.get("family")
    if family is None:
        raise RuntimeError("no model is loaded")
    task, path, prompt, opts = validate_request(body)
    if task not in _loaded["tasks"]:
        raise ValueError(
            f"{_loaded['model_id']} cannot {task}; it answers {', '.join(_loaded['tasks'])}")
    started = time.monotonic()
    out = family.run(task, path, prompt, opts)
    out["task"] = task
    out["seconds"] = round(time.monotonic() - started, 4)
    return out


def main():
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory, release=release)


if __name__ == "__main__":
    main()
