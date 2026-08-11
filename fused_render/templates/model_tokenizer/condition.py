"""Gate for the model_tokenizer template (SPEC §38, MV-3/MV-6).

Answers "can this folder tokenize text?" — which is one question with two
shapes, because the folder the AI Models page links to is not the folder the
file lives in:

1. **The folder itself holds `tokenizer.json`** — a snapshot directory, or a
   checkout someone cloned themselves. One `isfile`.
2. **A Hugging Face cache repo** — `models--org--name`, whose files live one
   level down under `snapshots/<commit>/`. That is the shape the AI Models
   cards open, so a gate that only checked (1) would never offer this view from
   the page that exists to open models. `refs/main` names the revision, because
   that is the one a load would get — the same rule model_card's reader follows.

`tokenizer.json` is the modern fast-tokenizer format: self-describing, and the
`tokenizers` library loads it standalone, which is exactly the set of folders
this playground can serve. A model whose tokenizer is the older `vocab.txt` +
`merges.txt` pair is deliberately NOT offered — loading those needs the model
class that owns them, and offering a playground that then cannot tokenize is
worse than not offering it.

Bound to the universal `/` registry key, so this runs on every directory the
user opens: targeted probes and one bounded read of a 40-byte ref, never a
listing (over a mount, a gate may not enumerate at all).

`tokenizer_path` has a twin in `tokenize_text.py` — a template is a set of
scripts the engine runs, not a package, so the two files cannot share a module
(SPEC PY-15/D166). `tests/test_model_templates.py` pins them to the same answer,
because a gate that offers a view the reader then cannot serve is the one
failure this pair can have.
"""
import os

_REF_READ_LIMIT = 4096


def _main_commit(path):
    """The commit `refs/main` points at, or None. Restores the file's atime:
    "last read" is what the AI Models page prunes by, and a folder GATE must not
    be what marks a model as recently used (MV-5/HF-15)."""
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
        try:
            os.utime(ref, (before.st_atime, before.st_mtime))
        except OSError:
            pass
    commit = raw.decode("utf-8", errors="ignore").strip()
    # A ref holds a bare commit sha. Anything carrying a separator is not one,
    # and must never be joined onto a path.
    if not commit or commit in (".", "..") or "/" in commit or "\\" in commit:
        return None
    return commit


def tokenizer_path(path):
    """The `tokenizer.json` this folder tokenizes with, or None."""
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


def main(path):
    return os.path.isdir(path) and tokenizer_path(path) is not None
