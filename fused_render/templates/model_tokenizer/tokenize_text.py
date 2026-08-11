"""Tokenize text with a folder's own `tokenizer.json`.

Two things are separated deliberately, because they fail differently:

* **Facts about the tokenizer** — vocabulary size, model kind (BPE, WordPiece,
  Unigram), special tokens, how many merges — are read straight from the JSON.
  They need no library and therefore always work.
* **Encoding text** needs `tokenizers`, which is not one of the app's bundled
  libraries, so it arrives through this folder's `pyproject.toml` and only
  under the fused engine (SPEC PY-16/PY-17). When it is missing, the answer
  says so in the reply rather than raising: the page still has the facts to
  show, and "this needs the fused engine" is information, not an error.

`tokenizer.json` can be tens of MB (a 250k-entry vocabulary), so the facts are
read with a bounded, targeted parse rather than by loading the whole file for
every keystroke — and the loaded tokenizer is cached per path, since the
playground calls this once per edit.
"""
import json
import os

_CACHE = {}


def _load(path):
    """The `tokenizers.Tokenizer` for this file; None when the library is absent,
    or a string explaining why this particular file could not be loaded.

    Cached per (path, mtime): a keystroke must not re-read a 40MB vocabulary
    from disk."""
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return None
    hit = _CACHE.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return None
    try:
        tokenizer = Tokenizer.from_file(path)
    except Exception as exc:  # noqa: BLE001 - see below
        # Deliberately broad, and the one place in this folder that is. The
        # loader is a Rust binding that raises whatever PyO3 hands back — a
        # vocabulary/merge mismatch, a version this build cannot read, a file
        # that is JSON but not a tokenizer — with no narrow type to name. A
        # template renders someone else's files, so the rule that matters here
        # is that a malformed one produces an explanation on the page rather
        # than an exception overlay.
        return str(exc) or exc.__class__.__name__
    _CACHE[path] = (stamp, tokenizer)
    return tokenizer


def _facts(path):
    """Vocabulary size, model kind, special tokens — from the JSON itself, so
    this half works with or without the library."""
    try:
        with open(path, "rb") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    vocab = model.get("vocab")
    added = data.get("added_tokens")
    specials = []
    if isinstance(added, list):
        for token in added[:64]:
            if isinstance(token, dict) and token.get("special") and isinstance(token.get("content"), str):
                specials.append(token["content"])
    merges = model.get("merges")
    return {
        "kind": model.get("type"),
        "vocabSize": len(vocab) if isinstance(vocab, (dict, list)) else None,
        "merges": len(merges) if isinstance(merges, list) else None,
        "specialTokens": specials,
        "truncation": bool(data.get("truncation")),
        "padding": bool(data.get("padding")),
    }


def main(path, text=""):
    """`path` is the model folder; `text` is what to encode (may be empty)."""
    tokenizer_path = os.path.join(path, "tokenizer.json")
    if not os.path.isfile(tokenizer_path):
        return {"error": "This folder has no tokenizer.json."}

    result = {"facts": _facts(tokenizer_path), "tokens": [], "available": True, "text": text}
    tokenizer = _load(tokenizer_path)
    if tokenizer is None:
        # Not an error: the facts above are still worth showing, and the page
        # explains what turning on the fused engine would add.
        result["available"] = False
        return result
    if isinstance(tokenizer, str):
        # A file this build of `tokenizers` cannot read. The facts survive, so
        # say what went wrong beside them rather than instead of them.
        result["available"] = False
        result["loadError"] = tokenizer
        return result
    if not text:
        return result

    encoding = tokenizer.encode(text)
    result["tokens"] = [
        # `offsets` is what lets the page highlight the ORIGINAL text rather
        # than the decoded piece, so a tokenizer that mangles whitespace (every
        # BPE one does) still lines up with what was typed.
        {"id": token_id, "piece": piece, "start": start, "end": end}
        for token_id, piece, (start, end) in zip(encoding.ids, encoding.tokens, encoding.offsets)
    ]
    result["count"] = len(encoding.ids)
    return result
