"""Gate for the model_card template (SPEC CT-12, §38).

`main(path)` answers "is this folder a model?" — and it runs on EVERY directory
the user opens, because model_card is bound to the universal `/` registry key
the way zarr_aoi is. So the same discipline applies: never list or walk the
directory, only targeted constant-time probes, cheapest first.

Two shapes count:

1. **A Hugging Face cache repo folder** — `models--org--name` holding
   `snapshots/`. A name check plus one `isdir`, no reads at all.
2. **A model folder proper** — a snapshot directory, or a checkout someone
   cloned themselves. Recognised by its marker files: `model_index.json` (a
   diffusers pipeline), `config_sentence_transformers.json` or `tokenizer.json`
   decide it outright, and `config.json` decides it only after a BOUNDED read
   confirming it is a model config rather than some other program's settings
   file. That read restores the file's atime, because a gate must not be the
   thing that marks a model as recently used (MV-5) — and it probes with
   `isfile` before it stats, so a folder WITHOUT a `config.json` costs the same
   cheap, listing-answerable probe it always did.

That last read is the whole reason this gate is accurate: `config.json` is one
of the most common filenames there is, and a folder of application settings is
not a model. `architectures`, `model_type` and `_class_name` are the keys that
make it one, and reading 64KB of a file we already know exists costs less than
being wrong on every project folder in someone's home directory.
"""
import json
import os

_KIND_PREFIXES = ("models--", "datasets--", "spaces--")

# Decisive on their own: nothing else writes these filenames. `tokenizer.json`
# earns its place here because the view now tokenizes too (MV-2), so a folder
# holding only a tokenizer — the shape a tokenizer-only repo has — is a folder
# this view has something to say about.
_MARKERS = ("model_index.json", "config_sentence_transformers.json", "tokenizer.json")

# Keys that make a config.json a MODEL config rather than any other JSON.
_MODEL_CONFIG_KEYS = ("architectures", "model_type", "_class_name", "quantization")

_CONFIG_READ_LIMIT = 64 * 1024


def _is_cache_repo(path: str) -> bool:
    name = os.path.basename(path.rstrip("/\\"))
    if not name.startswith(_KIND_PREFIXES):
        return False
    return os.path.isdir(os.path.join(path, "snapshots"))


def _has_model_config(path: str) -> bool:
    config = os.path.join(path, "config.json")
    # `isfile` FIRST, and the stat only after it says yes. The two are not
    # interchangeable over a mount: isfile/isdir/exists can be answered from the
    # listing the endpoint already has, and stat cannot — so probing with stat
    # would make every non-model folder pay a remote round trip for a name the
    # listing had already proven absent. The stat exists only to capture the
    # atime, which is worth paying on the folders that ARE models.
    if not os.path.isfile(config):
        return False
    try:
        before = os.stat(config)
    except OSError:
        return False  # vanished between the probe and the stat
    try:
        with open(config, "rb") as handle:
            raw = handle.read(_CONFIG_READ_LIMIT)
    except OSError:
        return False
    finally:
        # The gate reads a cache file, so it owes the same debt every other read
        # here does (MV-5/HF-15): "last read" is what the AI Models page prunes
        # by, and a folder GATE must not be what marks a model as used.
        try:
            os.utime(config, (before.st_atime, before.st_mtime))
        except OSError:
            pass
    try:
        parsed = json.loads(raw)
    except ValueError:
        # A truncated read of a config bigger than the limit lands here, as does
        # a file that is not JSON at all. Fail closed: not offered beats offered
        # and broken.
        return False
    return isinstance(parsed, dict) and any(key in parsed for key in _MODEL_CONFIG_KEYS)


def main(path):
    if not os.path.isdir(path):
        return False
    if _is_cache_repo(path):
        return True
    if any(os.path.isfile(os.path.join(path, marker)) for marker in _MARKERS):
        return True
    return _has_model_config(path)
