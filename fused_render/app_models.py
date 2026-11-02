"""AI models an app declares for itself (`[tool.fused-render.app.models]`).

A publisher lists the models the app uses in its `pyproject.toml`::

    [tool.fused-render.app.models]
    image = { model = "black-forest-labs/FLUX.2-klein-4B" }
    text  = { model = "Qwen/Qwen3-4B-Instruct-GGUF", use = "Captions" }

The key is the `fused.ai` verb, `model` the catalog id, `use` is a short
human label. The user may substitute any declared model with an
already-downloaded model of the same capability. The first time an app is
opened the user sees an overview of these models and accepts it; the choice lives in
`<app>/.fused/models.json` (moves with the folder, never exported), keyed by a
hash of the declaration so a changed declaration asks again.

This table is a sibling of the background-app keys owned by `background_apps`
and deliberately does NOT make an app a background app; that module never reads
it. Everything here is stat/parse only: no network, no model loads, and no
unix-only imports.
"""
import hashlib
import json
import logging
import os

from fused_render import app_fused_dir
from fused_render.ai import registry

logger = logging.getLogger(__name__)

#: `fused.ai.<verb>` -> registry capability.
VERB_CAPABILITY = {
    "text": registry.TEXT_GENERATION,
    "image": registry.IMAGE_GENERATION,
    "video": registry.VIDEO_GENERATION,
    "speech": registry.TEXT_TO_SPEECH,
    "transcribe": registry.SPEECH_TO_TEXT,
    "embed": registry.EMBEDDINGS,
    "decide": registry.DECISIONS,
    "vision": registry.VISION,
}

#: Shown under a model's name when the publisher gave no `use`.
CAPABILITY_LABEL = {
    registry.TEXT_GENERATION: "Text generation",
    registry.IMAGE_GENERATION: "Image generation",
    registry.VIDEO_GENERATION: "Video generation",
    registry.TEXT_TO_SPEECH: "Speech",
    registry.SPEECH_TO_TEXT: "Transcription",
    registry.EMBEDDINGS: "Embeddings",
    registry.DECISIONS: "Classification",
    registry.VISION: "Vision",
}

STATE_FILE = "models.json"
_MAX_WALK = 8

# (pyproject path, mtime_ns) -> declaration. The per-AI-call lookup is a stat.
_decl_cache: dict = {}


def _toml():
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            logger.warning("no TOML parser available; app models are ignored")
            return None
    return tomllib


def _parse(pyproject: str) -> dict:
    tomllib = _toml()
    if tomllib is None:
        return {}
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return {}
    try:
        table = data["tool"]["fused-render"]["app"]["models"]
    except (KeyError, TypeError):
        return {}
    if not isinstance(table, dict):
        return {}
    out: dict = {}
    for verb, entry in table.items():
        cap = VERB_CAPABILITY.get(verb)
        if cap is None or not isinstance(entry, dict):
            continue
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip():
            continue
        use = entry.get("use")
        out[cap] = {
            "verb": verb,
            "model": model.strip(),
            "use": use.strip() if isinstance(use, str) and use.strip() else None,
        }
    return out


def load_declaration(app_dir: str) -> dict:
    """`{capability: {verb, model, use}}`; `{}` when the folder
    declares nothing (missing file, parse error, no table). Never raises."""
    pyproject = os.path.join(app_dir, "pyproject.toml")
    try:
        key = (pyproject, os.stat(pyproject).st_mtime_ns)
    except OSError:
        return {}
    hit = _decl_cache.get(key)
    if hit is None:
        if len(_decl_cache) > 256:
            _decl_cache.clear()
        hit = _decl_cache[key] = _parse(pyproject)
    return {cap: dict(row) for cap, row in hit.items()}


def declaration_hash(decl: dict) -> str:
    """sha256 over (capability, model) only: a `use` relabel must
    not re-show the overview."""
    rows = sorted([cap, r["model"]] for cap, r in decl.items())
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def find_app_dir(path: str) -> str | None:
    """The nearest folder at or above `path` that declares models (at most
    8 levels, stopping at the filesystem root or `$HOME`)."""
    if not path:
        return None
    cur = os.path.abspath(path)
    if not os.path.isdir(cur):
        cur = os.path.dirname(cur)
    home = os.path.abspath(os.path.expanduser("~"))
    for _ in range(_MAX_WALK):
        if load_declaration(cur):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur or cur == home:
            return None
        cur = parent
    return None


def _state_path(app_dir: str) -> str:
    return os.path.join(app_fused_dir.dot_fused(app_dir), STATE_FILE)


def read_state(app_dir: str) -> dict:
    try:
        with open(_state_path(app_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(app_dir: str, accepted: bool, hash_: str, substitutions: dict) -> None:
    """Atomic write. Calls `ensure` itself: an accept can precede any open."""
    app_fused_dir.ensure(app_dir)
    os.makedirs(app_fused_dir.dot_fused(app_dir), exist_ok=True)
    dest = _state_path(app_dir)
    tmp = f"{dest}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"accepted": bool(accepted), "hash": hash_,
                   "substitutions": dict(substitutions)}, f, indent=2)
        f.write("\n")
    os.replace(tmp, dest)


def is_accepted(app_dir: str, decl: dict) -> bool:
    state = read_state(app_dir)
    return bool(state.get("accepted")) and state.get("hash") == declaration_hash(decl)


def substitution_for(page_path: str | None, capability: str,
                     model: str | None) -> str | None:
    """The user's substitute for this call, or None. Only a call that names the
    declared id (or names none) is rewritten; undeclared ids are left alone."""
    if not page_path:
        return None
    app_dir = find_app_dir(page_path)
    if app_dir is None:
        return None
    decl = load_declaration(app_dir)
    row = decl.get(capability)
    if row is None or not is_accepted(app_dir, decl):
        return None
    sub = (read_state(app_dir).get("substitutions") or {}).get(capability)
    if not isinstance(sub, str) or not sub:
        return None
    if model and model != row["model"]:
        return None
    return sub


def resolve(page_path: str | None, capability: str, model: str | None) -> str | None:
    return substitution_for(page_path, capability, model) or model
