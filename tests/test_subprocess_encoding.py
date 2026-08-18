"""Every `subprocess.run`/`Popen`/`check_output`/`check_call` call in the
shipped source tree that asks for text mode must pin an encoding.

`text=True` (or `universal_newlines=True`) with no `encoding=` decodes the
child's stdout/stderr with `locale.getpreferredencoding(False)`. A
GUI-launched fused-render process inherits no LANG/LC_ALL, which resolves
that to ASCII — so the first non-ASCII byte a child prints (an em dash, a
curly quote, a unicode file path, a git commit message, an rclone remote
name) raises UnicodeDecodeError and kills whatever feature made the call.
This exact bug shipped once already: `claude_spawn.spawn_helper` crashed
app-creation this way (see its own comment for the full incident writeup).
`claude_config/lib.py`'s `SUBPROCESS_KWARGS` fixed every call in that one
package and was pinned by an equivalent AST check scoped to it — this test
is the same check widened to the whole shipped tree, since the bug turned
out to recur in about thirty other files the narrower guard never saw.

AST-based, not grep-based: this file's own docstring and every fix's commit
message say "text=True" in prose, and a regex would flag itself.
"""
import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every one of these is shipped/executed source: the main package, its
# templates (child processes that can't import fused_render at all, D166,
# so they pin the kwargs inline rather than sharing a constant), and the
# core_apps that ship as builtin html+py apps. Dev-only tooling (scripts/,
# prototypes/) is deliberately excluded — it runs from a developer's own
# terminal, which has a real LANG, not the GUI-launched server this bug
# needs.
ROOTS = ["fused_render", "core_apps"]
SKIP_DIRS = {"__pycache__", "node_modules", "vendor", "shell-dist"}

# Documented exceptions, `<relpath>: <reason>`. Keep this EMPTY if you
# possibly can.
ALLOWED = {}


def _py_files():
    for root in ROOTS:
        base = os.path.join(REPO_ROOT, root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    yield os.path.relpath(path, REPO_ROOT)


def _bare_text_calls(relpath):
    """(lineno, func name) for every subprocess spawn in `relpath` that asks
    for text mode but neither pins an encoding nor spreads kwargs (a spread
    like `**SUBPROCESS_KWARGS` cannot be inspected here, so it is trusted —
    exactly as the claude_config-scoped version of this check already does)."""
    with open(os.path.join(REPO_ROOT, relpath), encoding="utf-8") as f:
        src = f.read()
    out = []
    for node in ast.walk(ast.parse(src, filename=relpath)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute)
                and fn.attr in ("run", "Popen", "check_output", "check_call")
                and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            continue
        keywords = node.keywords
        has_text = any(kw.arg in ("text", "universal_newlines")
                       and isinstance(kw.value, ast.Constant) and kw.value.value is True
                       for kw in keywords)
        if not has_text:
            continue
        has_spread = any(kw.arg is None for kw in keywords)
        has_encoding = any(kw.arg == "encoding" for kw in keywords)
        if not has_spread and not has_encoding:
            out.append((node.lineno, fn.attr))
    return out


def test_every_subprocess_text_call_pins_an_encoding():
    violations = []
    checked = 0
    for relpath in _py_files():
        if relpath in ALLOWED:
            continue
        for lineno, func in _bare_text_calls(relpath):
            violations.append(f"{relpath}:{lineno} subprocess.{func}(text=True, ...) with no encoding=")
        checked += 1
    assert violations == []
    # A guard that silently walked zero files would pass forever.
    assert checked > 200
