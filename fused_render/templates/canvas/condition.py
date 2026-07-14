"""Condition gate for the `canvas` template (SPEC CT-12, §26).

Runs in the SERVER process on every `.toml` stat/render resolution, so it must
be cheap and must NEVER raise — a broken gate is meant to *drop* the template,
and returning False is how we do that (`server._run_condition` also catches, but
we fail closed here explicitly, SPEC CT-12/§26).

`method(target_path)` is True only when the file is a genuine Fused canvas
definition: basename `canvas.toml` (the cheap pre-check, done before any I/O)
AND the parsed TOML declares `type = "canvas"` (the content sniff, D105). A
plain `.toml`, a `canvas.toml` that isn't actually a canvas, an oversized file,
or anything that fails to parse → False, so the `code` mode stays the default
for ordinary toml.
"""
import os
import tomllib

# Content sniff only opens files small enough to be a real canvas definition —
# a canvas.toml is node/edge metadata, never megabytes. Anything larger is not
# ours to preview; skip the parse and fail closed (matches the reader's guard).
MAX_BYTES = 2 * 1024 * 1024


def method(target_path) -> bool:
    try:
        # Cheap pre-check first: only a file literally named canvas.toml can be
        # a canvas, so a plain .toml never pays the open/parse cost (SPEC §26).
        if os.path.basename(str(target_path)).lower() != "canvas.toml":
            return False
        # Size guard before opening — a pathological file must not be parsed.
        if os.path.getsize(target_path) > MAX_BYTES:
            return False
        with open(target_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        # Missing file, permission error, malformed TOML, decode error — a gate
        # that can't decide is not silently shown (SPEC CT-12): fail closed.
        return False
    return isinstance(data, dict) and data.get("type") == "canvas"
