"""Tokenize text with a folder's own `tokenizer.json`.

Two halves, separated deliberately, because they cost and fail differently:

* **Facts about the tokenizer** — vocabulary size, model kind (BPE, WordPiece,
  Unigram), special tokens, how many merges — are read straight from the JSON.
  They need no library and therefore always work. They are also the expensive
  half: `tokenizer.json` is routinely tens of MB (a 250k-entry vocabulary), so
  the page asks for them ONCE when it opens and passes `facts=False` on every
  keystroke after that.
* **Encoding text** needs `tokenizers`, which is not one of the app's bundled
  libraries, so it arrives through this folder's `pyproject.toml` and only
  under the fused engine (SPEC PY-16/PY-17). When it is missing, the answer
  says so in the reply rather than raising: the page still has the facts to
  show, and "this needs the fused engine" is information, not an error.

**Nothing is cached between calls, and nothing can be:** the engine runs this
script in a fresh subprocess per call (PY-6), so a module-level dict would be
dead code that reads as an optimisation. What keeps typing responsive is the
page's debounce, not re-parsing the facts on every keystroke, and the Rust
loader — which reads a large vocabulary in tens of milliseconds. Holding one
tokenizer open across keystrokes needs a resident process, which is a different
design from a script the engine runs.

Every read here restores the file's atime, for the reason the AI Models page
cares about (MV-5/HF-15): "last read" is what pruning by age is built on, and
typing in a playground is not using a model.

`tokenizer_path` has a twin in `condition.py` — a template is a set of scripts
the engine runs, not a package, so the two cannot share a module (SPEC
PY-15/D166). `tests/test_model_templates.py` pins them to the same answer.
"""
import json
import os

_REF_READ_LIMIT = 4096


def _restore_atime(path, before):
    if before is None:
        return
    try:
        os.utime(path, (before.st_atime, before.st_mtime))
    except OSError:
        pass


def _main_commit(path):
    """The commit `refs/main` points at, or None."""
    ref = os.path.join(path, "refs", "main")
    try:
        before = os.stat(ref)
    except OSError:
        return None
    try:
        with open(ref, "rb") as handle:
            raw = handle.read(_REF_READ_LIMIT)
    except OSError:
        return None
    finally:
        _restore_atime(ref, before)
    commit = raw.decode("utf-8", errors="ignore").strip()
    # A ref holds a bare commit sha. Anything carrying a separator is not one,
    # and must never be joined onto a path.
    if not commit or commit in (".", "..") or "/" in commit or "\\" in commit:
        return None
    return commit


def tokenizer_path(path):
    """The `tokenizer.json` this folder tokenizes with, or None.

    Two shapes: the folder itself (a snapshot, or someone's own checkout), or a
    Hugging Face CACHE REPO (`models--org--name`) whose files live under
    `snapshots/<commit>/` — the shape the AI Models cards open. `refs/main`
    decides the revision, because that is the one a load would get.
    """
    direct = os.path.join(path, "tokenizer.json")
    if os.path.isfile(direct):
        return direct
    if not os.path.isdir(os.path.join(path, "snapshots")):
        return None
    commit = _main_commit(path)
    if not commit:
        return None
    candidate = os.path.join(path, "snapshots", commit, "tokenizer.json")
    return candidate if os.path.isfile(candidate) else None


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
        # The Rust loader read the file, so it owes the same debt every read
        # here does.
        _restore_atime(path, before)
    return tokenizer


def _facts(path):
    """Vocabulary size, model kind, special tokens — from the JSON itself, so
    this half works with or without the library. The whole file is parsed, which
    is why the page asks for this once rather than per keystroke."""
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


def main(path, text="", facts=True):
    """`path` is the folder the view was opened on (a model folder or a cache
    repo), `text` is what to encode (may be empty), and `facts` asks for the
    tokenizer's own description — which the page requests only on load."""
    resolved = tokenizer_path(path)
    if resolved is None:
        return {"error": "This folder has no tokenizer.json."}

    result = {"tokens": [], "available": True, "text": text}
    if facts:
        result["facts"] = _facts(resolved)
    tokenizer = _load(resolved)
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
        result["count"] = 0
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
