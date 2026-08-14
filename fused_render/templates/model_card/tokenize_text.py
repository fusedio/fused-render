"""Encode text with the model's own `tokenizer.json`, for the card's tokenizer
section.

`path` is the **already-resolved** snapshot root — `inspect_model.py` worked out
which revision `refs/main` names when the card was drawn, and the page hands
that answer back here. So this script resolves nothing: no ref reading, no
second copy of the cache layout to drift from the first. It reads one file in
one folder.

Two halves, separated because they cost and fail differently:

* **Facts about the tokenizer** — vocabulary size, kind (BPE, WordPiece,
  Unigram), special tokens, merges — are read straight from the JSON, so they
  need no library and always work. They are also the expensive half:
  `tokenizer.json` is routinely tens of MB, so the page asks for them on the
  FIRST call only and never again.
* **Encoding** needs `tokenizers`, which is not one of the app's bundled
  libraries — it arrives through this folder's `pyproject.toml`, and therefore
  only under the fused engine (SPEC PY-16/PY-17). Its absence is a state the
  page explains, not an error: the facts are still worth showing.

The two are requested TOGETHER on that first call, because loading the file to
encode and parsing it for facts are one visit to one file — asking separately
would load a 40MB vocabulary twice to answer one keystroke.

**Nothing survives between calls, and nothing can:** the engine runs this in a
fresh subprocess per call (PY-6), so a module-level cache would be dead code
that reads as an optimisation. What keeps typing responsive is the page's
debounce and not re-parsing the facts. Holding one tokenizer open across
keystrokes needs a resident process, which is a different design from a script.

Reads restore the file's atime (MV-5/HF-15): "last read" is what the AI Models
page prunes by, and typing in a playground is not using a model.
"""
import json
import os


def _restore_atime(path, before):
    if before is None:
        return
    try:
        os.utime(path, (before.st_atime, before.st_mtime))
    except OSError:
        pass


def _load(path):
    """The `tokenizers.Tokenizer` for this file; None when the library is absent,
    or a string explaining why this particular file could not be loaded."""
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return None
    try:
        before = os.stat(path)
    except OSError:
        before = None  # nothing to put back; not a reason to claim the library is missing
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
    finally:
        # The Rust loader read the file, so it owes the same debt every read here
        # does.
        _restore_atime(path, before)
    return tokenizer


def _facts(path):
    """Vocabulary size, kind, special tokens — from the JSON itself, so this half
    works with or without the library. The whole file is parsed, which is why the
    page asks for it once rather than per keystroke."""
    try:
        before = os.stat(path)
    except OSError:
        return {}
    try:
        with open(path, "rb") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    finally:
        _restore_atime(path, before)
    if not isinstance(data, dict):
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


def main(path, text="", facts=False):
    """`path` is the resolved model root, `text` is what to encode (may be
    empty), and `facts` asks for the tokenizer's own description — which the page
    requests on its first call and never again."""
    tokenizer_path = os.path.join(path, "tokenizer.json")
    if not os.path.isfile(tokenizer_path):
        return {"error": "This model has no tokenizer.json."}

    result = {"tokens": [], "count": 0, "available": True, "text": text}
    if facts:
        result["facts"] = _facts(tokenizer_path)
    if not text:
        # Nothing to encode, so nothing to load. `available` stays unanswered
        # until there is a reason to answer it, which keeps a facts-only call
        # from paying for a load it would then throw away.
        return result

    tokenizer = _load(tokenizer_path)
    if tokenizer is None:
        # Not an error: the facts are still worth showing, and the page explains
        # what turning on the fused engine would add.
        result["available"] = False
        return result
    if isinstance(tokenizer, str):
        # A file this build of `tokenizers` cannot read. The facts survive, so
        # say what went wrong beside them rather than instead of them.
        result["available"] = False
        result["loadError"] = tokenizer
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
