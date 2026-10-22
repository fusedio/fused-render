"""The shared vision runner body, over which `vision_torch/` and `vision_torch_cuda/` are shells (SPEC §40, AI-33).

Image understanding on torch: one family class per kind of model in
`formats.VISION_TORCH_RECIPES`, both through transformers. Served by `worker_base`.
"""

import os
import sys

# This module IS in `runners/`, beside `worker_base`/`formats`/`embed_common`
# (the folder shells insert the same directory; see torch_image.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import re  # noqa: E402
import time  # noqa: E402

import embed_common  # noqa: E402 - the shared image opener (HEIC included)
import formats  # noqa: E402 - the recipe table, shared with the server
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: Every task the vision verb names; a recipe answers a subset.
TASKS = ("caption", "query", "detect", "point")
#: Moondream 2's tokenizer, a separate repo its code loads by name (`formats.COMPONENT_REPOS`).
MOONDREAM_TOKENIZER = "moondream/starmie-v1"
CAPTION_LENGTHS = ("short", "normal", "long")
DEFAULT_MAX_OBJECTS = 50
#: Grounding DINO's own defaults for keeping a box and naming it.
BOX_THRESHOLD = 0.3
TEXT_THRESHOLD = 0.25

_loaded = {}

#: The device this runner's FOLDER promises. `vision_torch_cuda/worker.py` sets
#: it to "cuda" before `main()`; the CPU folder leaves it None. Without it a
#: torch that cannot see the GPU would quietly serve on CPU/float32, which
#: contradicts `registry._cuda` (a bad driver fails loudly at load).
EXPECTED_DEVICE = None


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
    return embed_common.open_image(path, upright=True)


def _clamp(v):
    return min(max(float(v), 0.0), 1.0)


# ------------------------------------------------------------------ families


class _Moondream:
    """Moondream 2 as its model card loads it: transformers running the repo's own code."""

    def __init__(self, model_id, spec, path):
        # The repo's Python runs here, so only the reviewed commit may.
        if os.path.basename(os.path.normpath(path)) != spec["revision"]:
            raise RuntimeError(f"{model_id} resolved to {path}, not the pinned commit {spec['revision']}")
        from transformers import AutoModelForCausalLM

        self.device, dtype = _device(half=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            path, trust_remote_code=True, dtype=dtype).to(self.device).eval()
        self._key, self._encoded = None, None

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
            settings = {"max_objects": opts["maxObjects"]}
            found = self.model.detect(encoded, prompt, settings=settings)["objects"]
            out["objects"] = [{"xMin": _clamp(o["x_min"]), "yMin": _clamp(o["y_min"]),
                               "xMax": _clamp(o["x_max"]), "yMax": _clamp(o["y_max"]),
                               "label": prompt, "score": None} for o in found]
        elif task == "point":
            settings = {"max_objects": opts["maxObjects"]}
            found = self.model.point(encoded, prompt, settings=settings)["points"]
            out["points"] = [{"x": _clamp(p["x"]), "y": _clamp(p["y"]), "label": prompt}
                             for p in found[: opts["maxObjects"]]]
        return out


class _GroundingDino:
    """Open-vocabulary detection through transformers' Grounding DINO."""

    def __init__(self, model_id, spec, path):
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        self.device, _ = _device(half=False)
        self.processor = AutoProcessor.from_pretrained(path)
        self.model = AutoModelForZeroShotObjectDetection.from_pretrained(path).to(self.device).eval()

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
    if spec["family"] == "moondream":
        worker_base.download_file(
            MOONDREAM_TOKENIZER, formats.COMPONENT_REPOS[MOONDREAM_TOKENIZER]["file"],
            detail="Fetching Moondream's tokenizer…")
    return worker_base.download_snapshot(model_id, allow_patterns=spec["allow"],
                                         revision=spec.get("revision"))


def load(model_id, path):
    spec = recipe(model_id)
    if EXPECTED_DEVICE == "cuda" and _device(half=False)[0] != "cuda":
        raise RuntimeError(
            "vision-torch-cuda loaded torch without CUDA; check the NVIDIA "
            "driver supports this torch build")
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
    elif not isinstance(prompt, str) or not any(c.isalnum() for c in prompt):
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
    import torch

    family = _loaded.get("family")
    if family is None:
        raise RuntimeError("no model is loaded")
    task, path, prompt, opts = validate_request(body)
    if task not in _loaded["tasks"]:
        raise ValueError(
            f"{_loaded['model_id']} cannot {task}; it answers {', '.join(_loaded['tasks'])}")
    started = time.monotonic()
    # Grad mode is per thread, and requests do not run on the thread `load` did.
    with torch.inference_mode():
        out = family.run(task, path, prompt, opts)
    out["task"] = task
    out["seconds"] = round(time.monotonic() - started, 4)
    return out


def main():
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory, release=release)


if __name__ == "__main__":
    main()
