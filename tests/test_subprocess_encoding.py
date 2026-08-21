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
#
# `.venv` is in SKIP_DIRS for a reason worth spelling out, because it is the one
# entry that names a directory living INSIDE the shipped package: `projectenv`
# uv-syncs each AI runner environment in place at
# `fused_render/ai/runners/<runner>/.venv`, so as soon as a developer loads a
# local model, tens of thousands of third-party files appear under a root this
# sweep walks — and third-party code is full of unpinned `text=True` calls this
# repo cannot fix. CI has no runner venv, so the failure only ever appeared on
# developer machines. `test_git_posix_spawn._NOT_OUR_SOURCE` carries the same set
# for the same reason.
ROOTS = ["fused_render", "core_apps"]
SKIP_DIRS = {".venv", "__pycache__", "node_modules", "vendor", "shell-dist"}

# Documented exceptions, `<relpath>: <reason>`. Keep this EMPTY if you
# possibly can.
ALLOWED = {}


def _py_files(repo_root=REPO_ROOT):
    """Every shipped `*.py` under ROOTS, relative to `repo_root`.

    `repo_root` is a parameter so `test_an_installed_runner_venv_is_skipped` can
    point the same walk at a synthetic tree: SKIP_DIRS is a silent filter, and a
    silent filter needs something asserting it still filters what it claims to.
    """
    for root in ROOTS:
        base = os.path.join(repo_root, root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    path = os.path.join(dirpath, name)
                    yield os.path.relpath(path, repo_root)


def _bare_text_calls(relpath, repo_root=REPO_ROOT):
    """(lineno, func name) for every subprocess spawn in `relpath` that asks
    for text mode but neither pins an encoding nor spreads kwargs (a spread
    like `**SUBPROCESS_KWARGS` cannot be inspected here, so it is trusted —
    exactly as the claude_config-scoped version of this check already does).

    `repo_root` matches `_py_files`' parameter, and for the same reason."""
    with open(os.path.join(repo_root, relpath), encoding="utf-8") as f:
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


def test_an_installed_runner_venv_is_skipped(tmp_path):
    """The sweep does not read a runner's installed dependencies, but does read
    the runner source sitting right beside them.

    Both directions matter. Without the first, every developer who has loaded a
    local model fails this test on third-party code (see SKIP_DIRS' comment).
    Without the second, the exclusion could widen to swallow real modules and the
    suite would stay green while checking nothing — the worse of the two, because
    it is invisible. Both files below are genuine violations, so a sweep that
    pruned everything cannot pass by finding nothing.
    """
    vendored = (tmp_path / "fused_render" / "ai" / "runners" / "llamacpp_text"
                / ".venv" / "lib" / "python3.13" / "site-packages")
    vendored.mkdir(parents=True)
    (vendored / "evil.py").write_text(
        "import subprocess\nsubprocess.run(['x'], text=True)\n")
    runner = tmp_path / "fused_render" / "ai" / "runners" / "llamacpp_text"
    (runner / "worker.py").write_text(
        "import subprocess\nsubprocess.run(['x'], text=True)\n")

    # `os.path.join` for the expectation, not a "fused_render/ai/..." literal:
    # `_py_files` yields `os.path.relpath` strings, which carry the OS separator,
    # so a forward-slash literal here would pass on Linux and fail on Windows
    # only. `test_git_posix_spawn.test_the_sweeps_skip_a_runner_venv` — this
    # test's sibling — shipped exactly that bug and was caught by
    # `test-python-windows`; it normalises with `Path.as_posix()` instead,
    # because `_sources` there hands back `Path` objects rather than strings.
    # Two different styles, each matching what its own walker returns. Do not
    # unify them.
    swept = set(_py_files(str(tmp_path)))
    assert swept == {os.path.join("fused_render", "ai", "runners",
                                  "llamacpp_text", "worker.py")}
    # The offending line in the file that IS swept is still detected, so the
    # exclusion narrowed the sweep's reach without blunting the sweep itself.
    assert _bare_text_calls(sorted(swept)[0], str(tmp_path)) == [(2, "run")]

