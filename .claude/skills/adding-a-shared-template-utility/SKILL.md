---
name: adding-a-shared-template-utility
description: Use when adding, editing, or vendoring a helper in fused_render/templates/shared/ — a module both built-in templates AND arbitrary user .py files need one copy of (env facts, a stdlib API client). Covers the stdlib-only constraint, why it must never import fused_render, the sibling-import-by-path rule, the two path-seeding sites that have to change together, and why export.py never ships it.
---

# Adding a Shared Template Utility

## Overview

`fused_render/templates/shared/` is where ONE copy of a helper lives when both a built-in template and an arbitrary user `.py` need it — `appenv.py` (env facts: home dir, mounts, origin) and `fused_ai.py` (the Python client for `fused.ai`) are the two that exist today. It looks like an ordinary shared-code folder. It is not: every file in it runs as a subprocess with no guarantee the `fused_render` package is importable, and both consumer shapes (a template shipped inside the app, a user file anywhere on disk) have to resolve the same import identically. The rules below are currently scattered across `appenv.py`'s and `fused_ai.py`'s own docstrings and SPEC PY-15/PY-19 — write them down here instead of re-deriving them per contribution.

## Where it goes, and how each consumer reaches it

- **A built-in template** (`fused_render/templates/<name>/*.py`) reaches it with the established relative dance:
  ```python
  import sys, os
  sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
  import appenv
  ```
  This only works because the template physically lives inside `templates/`, one level above `shared/`.
- **A user `.py`**, anywhere on disk, just does `import fused_ai` (or `import appenv`) with no path setup at all. Both execution engines append `templates/shared` onto that module's own `sys.path` before it runs — see "Path seeding" below — so the import resolves the same way `import pandas` does.

## Stdlib only — not a style preference

A user `.py` may run inside a hermetic `uv` venv built from ITS OWN `pyproject.toml` (PY-16) and containing exactly what that manifest declares, nothing unioned in from a baseline. A shared utility that imports `requests`, or anything beyond the standard library, would need every app that ever imports it to declare that dependency in its own manifest — which no contributor adding a shared helper is in a position to arrange, and which silently breaks the first app that doesn't. `json`, `os`, `socket`, `time`, `urllib` are fine; a third-party package is not.

## Never `import fused_render`

The subprocess a template or a user `.py` runs in has `PYTHONPATH` stripped (SPEC PY-15 / D166) — deliberately, for venv hermeticity under the fused engine, and as a side effect under the built-in executor too. A shared file that tries `from fused_render.shell.mounts import ...` works when the package happens to be importable and silently takes its fallback branch when it is not, which is exactly the kind of engine-dependent behavior that goes unnoticed until someone runs the other engine. `_child.py` has a dedicated diagnostic for a user file that imports the package (naming the interpreter, `PYTHONPATH`, and `sys.path[:3]`) precisely because this mistake is common enough to need one.

**The one sanctioned channel back to the app is `appenv.py`**: the server exports resolved facts as `FUSED_RENDER_*` env vars before it starts serving, and `appenv.py` reads only those, per call, never caching them at import time. If your utility needs a fact about the running app (a directory, the server's origin), it goes through `appenv`, not through `fused_render`.

## Import a sibling by PATH, not by name

The shared dir is **appended** to a user module's `sys.path` — not inserted at position 0 — so that a user's own same-named module wins if one exists (a user genuinely has an `appenv.py` of their own beside their script, and their copy must shadow the shipped one). This means a bare `import appenv` from inside another shared file resolves whatever `appenv` sits FIRST on `sys.path`, which can be the user's, not yours — and the user's stand-in almost certainly does not define the functions your file expects, so the failure is an `AttributeError` a user should never see from a dependency they don't know exists.

**This was a real review finding, not a hypothetical** (`fused_ai.py`'s original `import appenv`, on this repo's Python-client branch). The fix: load your own sibling by its file location, sidestepping `sys.path` order entirely.

```python
import importlib.util, os

def _load_sibling_appenv():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "appenv.py")
    spec = importlib.util.spec_from_file_location("_my_module_appenv", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

appenv = _load_sibling_appenv()
```

Any new shared file that depends on another file in this same directory needs this pattern, not a plain `import`.

## Path seeding lives in two places — change both, in lockstep

Nothing above works for a user `.py` unless something puts `templates/shared` on its `sys.path` first, and that happens in exactly two places, one per execution engine:

- `fused_render/_child.py` — the built-in executor's worker, which does `sys.path.insert(0, module_dir)` then `sys.path.append(<shared dir>)`.
- `fused_render/engine.py` — the fused engine's generated wrapper string, which does the identical insert-then-append.

**One-sided is the documented trap.** A fix landed in only one of these works under whichever engine you tested and silently falls back (or breaks) under the other — the exact failure mode `_child.py`'s own module docstring calls out for the retired PYTHONPATH-injection mechanism (PY-6a). Both derive the shared dir from their OWN file's location, never an env var: `_child.py` is invoked as a standalone script, and `engine.py` bakes a literal path into generated source for a subprocess that may not have inherited the server's environment at all. If you touch the seeding logic itself (not just add a new shared file), touch both.

## `export.py` does not copy `templates/shared/`

An exported page ships as a static bundle with no server behind it. `export.py` never copies `templates/shared/` into that bundle, so a shared utility — however useful — is simply absent from an exported `.py`, and `import fused_ai` (or your new module) fails there before it could do anything. This is moot rather than dangerous for a stdlib-only AI client (there is no server on the other end anyway), but it means **do not build a shared utility that an exported page is expected to use** — there is no path that makes that work today.

## When NOT to put it here

If only one template needs the helper, it belongs beside that template, not in `shared/` — a file only one consumer reaches does not need the append-precedence dance, the stdlib constraint stops applying as tightly (that template's own `pyproject.toml`, if it has one, can declare a real dependency), and a second copy nobody else uses is a maintenance cost with no payoff. Move a helper into `shared/` only once a second, genuinely independent consumer (another template, or the class of arbitrary user `.py` files) actually needs the same code.
