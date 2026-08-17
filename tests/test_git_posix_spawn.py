"""Every git subprocess must reach `posix_spawn`, never `fork()`.

THE ROOT CAUSE of "the Git side panel is disabled for every repository".

With `libproj` resident in the server process — and it becomes resident the
moment any map / geotiff / zarr template or daemon imports rasterio or pyproj —
a plain `fork()` runs PROJ's `pthread_atfork` child handler, which SIGSEGVs
*before* `exec`. The child dies with signal 11, so:

    returncode == -11,  stdout == b"",  stderr == b""

No exception is raised, because the spawn itself succeeded. Every git call site
in the app fails CLOSED on that: `/api/fs/conditions` reports `"git": false`,
`/api/fs/git-repo` reports `is_repo_root: false` for a real root, and
`/api/fs/list` stops dimming `.git`. All of it silent, all of it for every
repository at once, and all of it indistinguishable from "not a repository" —
which is exactly how it presented, and why it was mis-diagnosed twice.

Measured in the live server:

    WARNING fused_render.templates.git.condition: the git mode is being hidden
    for /Users/iamsdas/Documents/Fused and it looks wrong: git exited -11 saying
    '(nothing)'

`close_fds=False` was believed to be the fix, and three of these modules already
passed it with a comment saying so. **It is necessary and NOT sufficient.**
CPython takes the posix_spawn path only when ALL of these hold
(`subprocess.py::_execute_child`):

    _USE_POSIX_SPAWN and os.path.dirname(executable) and preexec_fn is None
    and not close_fds and not pass_fds and cwd is None and ... and umask < 0

Two of those were being violated everywhere:

  * `os.path.dirname(executable)` — the argv started with the BARE NAME `"git"`,
    whose dirname is `""`. Falsy. So every call forked, `close_fds=False` or not.
  * `cwd is None` — the template gate passed `cwd=<the directory>`, which forces
    fork on its own even with an absolute executable.

So the rule this file pins is the whole rule, not the half of it that reads
plausibly: an ABSOLUTE git path, `close_fds=False`, and no `cwd=` (git already
gets `-C`). It is pinned as a BEHAVIOUR — the recorded kwargs are run through
CPython's own condition — rather than as a grep, so it cannot pass by accident.
"""
import importlib.util
import os
import subprocess

import pytest

_TPL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "git")


def assert_posix_spawnable(argv, kwargs, where):
    """CPython's own preconditions for taking the posix_spawn path.

    Mirrors `subprocess.py::_execute_child`'s condition, minus the parts no
    caller here sets (uid/gid/umask/process_group). A violation means this call
    forks — and a fork in a process with libproj resident dies with SIGSEGV
    before it ever execs git.
    """
    executable = argv[0]
    assert os.path.dirname(executable), (
        f"{where}: argv[0] is {executable!r} — a bare name has no dirname, so "
        "CPython forks instead of posix_spawn'ing. Resolve git to an absolute "
        "path.")
    assert os.path.isabs(executable), f"{where}: {executable!r} is not absolute"
    assert kwargs.get("close_fds") is False, (
        f"{where}: close_fds must be explicitly False to reach posix_spawn")
    assert kwargs.get("cwd") is None, (
        f"{where}: cwd={kwargs.get('cwd')!r} forces the fork path on its own — "
        "git already takes -C, so pass no cwd")
    assert kwargs.get("preexec_fn") is None, f"{where}: preexec_fn forces fork"
    assert not kwargs.get("pass_fds"), f"{where}: pass_fds forces fork"
    assert not kwargs.get("start_new_session"), (
        f"{where}: start_new_session forces fork")


@pytest.fixture
def recorder(monkeypatch):
    """Capture every subprocess spawn without running one."""
    calls = []

    class _Fake:
        returncode = 0
        stdout = b""
        stderr = b""

        def __init__(self):
            self.stdin = None

        def communicate(self, *a, **k):
            return b"", b""

    def record(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Fake()

    monkeypatch.setattr(subprocess, "run", record)
    monkeypatch.setattr(subprocess, "Popen", record)
    return calls


# ------------------------------------------------------- the server-side sites


def test_repo_toplevel_reaches_posix_spawn(recorder, tmp_path):
    from fused_render.server import gitignore

    gitignore._repo_toplevel(str(tmp_path))
    assert recorder, "no git call was recorded"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "gitignore._repo_toplevel")


def test_git_ignored_reaches_posix_spawn(recorder, tmp_path):
    from fused_render.server import gitignore

    gitignore._git_ignored(str(tmp_path), ["a", "b"])
    assert recorder, "no git call was recorded"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "gitignore._git_ignored")


def test_ignore_oracle_reaches_posix_spawn(recorder, tmp_path):
    from fused_render.server import gitignore

    (tmp_path / ".git").mkdir()          # a real repo: no empty-GIT_DIR graft
    gitignore._IgnoreOracle(str(tmp_path))
    assert recorder, "no git call was recorded"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "gitignore._IgnoreOracle")


# ----------------------------------------------------- the git template's own


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_TPL, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_gate_reaches_posix_spawn(recorder, tmp_path):
    """The gate is the surface the user hit: it decides `"git": false`."""
    gate = _load("git_condition_spawn", "condition.py")
    gate.main(str(tmp_path))
    assert recorder, "the gate made no git call"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "templates/git/condition.py")


def test_the_reader_reaches_posix_spawn(recorder, tmp_path):
    reader = _load("git_log_spawn", "log.py")
    reader.main(file=str(tmp_path))
    assert recorder, "the reader made no git call"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "templates/git/log.py")


def test_the_writer_reaches_posix_spawn(recorder, tmp_path):
    ops = _load("git_ops_spawn", "ops.py")
    ops.main(file=str(tmp_path), op="fetch")
    assert recorder, "the writer made no git call"
    for argv, kwargs in recorder:
        assert_posix_spawnable(argv, kwargs, "templates/git/ops.py")


# ------------------------------------------------------------- the real thing


def test_a_signal_death_is_not_mistaken_for_a_missing_repo(monkeypatch, caplog,
                                                           tmp_path):
    """The symptom, reproduced exactly: rc -11, no output, no exception.

    It must still fail closed (it is not a repo answer we can trust), and it must
    now be LOUD — a silent -11 is what cost two investigations.
    """
    from fused_render.server import gitignore

    (tmp_path / ".git").mkdir()
    gitignore._reset_spawn_failure_throttle()

    class _Segv:
        returncode = -11
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Segv())
    with caplog.at_level("WARNING", logger="fused_render.server.gitignore"):
        assert gitignore._is_repo_root(str(tmp_path)) is False
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "-11" in joined, "a SIGSEGV'd git was reported without its exit status"


# --------------------------------------------------- every OTHER git spawn too
#
# The same latent bug sat at eight more sites. Two of them matter directly to the
# feature the user reported: `routers/git_show.py` renders a file as of a commit
# (GT-17, reached from the same git view) and `server/fs_mutate.py` performs the
# server's git writes. The rest — app_git, community, deeplink, claude_config,
# the bundle reader, the claude agent — would fail the same silent way.
#
# `app_git.py`'s module docstring records this exact crash being diagnosed in
# August ("every `git add` died rc=-11 with empty output") and being fixed with
# `close_fds=False`. That fix could never have worked while argv[0] stayed the
# bare name `"git"`: the posix_spawn path was still unreachable. It only LOOKED
# fixed because it was validated in a process where libproj was not resident. So
# this is pinned by test rather than by comment.


# ---------------------------------------------------------- the STATIC backstop
#
# The behavioural tests above only reach call sites a test can drive. The five
# sites that shipped forking on this branch were NOT among them, and the first
# version of this sweep passed anyway because it only looked at argv[0] — a
# backstop that goes green while the bug is live is worse than no backstop, so
# this one checks the whole rule.
#
# AST-based, and deliberately so. A regex over source lines misses the same
# violation written multi-line, which is this repo's prevailing style (the git
# gate's own call is wrapped across lines), and it cannot see a tuple argv or an
# `executable=` kwarg at all.
#
# `**_popen_kwargs()`-style indirection is resolved by reading the helper's dict
# literal out of the SAME file's AST — no import, so no module side effects. When
# a spawn's kwargs cannot be determined statically the sweep FAILS rather than
# skipping: "I could not check this" must never read as "this is fine", which is
# precisely how five forking sites slipped through.

_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}
_GIT_NAMES = {"git", "git.exe"}


def _is_git_argv(node):
    """Whether this call's program is git, however argv[0] is spelled."""
    import ast

    def names_git(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return os.path.basename(n.value) in _GIT_NAMES
        if isinstance(n, ast.Call):                     # _git_bin() / git_bin()
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in ("_git_bin", "git_bin"):
                return True
            if name == "which" and n.args:              # shutil.which("git")
                return names_git(n.args[0])
        if isinstance(n, ast.BoolOp):                   # which("git") or "git"
            return any(names_git(v) for v in n.values)
        return False

    for kw in node.keywords:
        if kw.arg == "executable" and names_git(kw.value):
            return True
    if not node.args:
        return False
    argv = node.args[0]
    if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
        return names_git(argv.elts[0])
    return names_git(argv)


def _dict_literal_of(tree, name):
    """The keys of a module-level `NAME = {...}` or `def NAME(): return {...}`.

    Returns a dict of key -> ast node, or None when it is not a plain literal we
    can read (which the caller must treat as a FAILURE, not a pass).
    """
    import ast

    def keys_of(d):
        if not isinstance(d, ast.Dict):
            return None
        out = {}
        for k, v in zip(d.keys, d.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return None
            out[k.value] = v
        return out

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return keys_of(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == name:
            for sub in node.body:
                if isinstance(sub, ast.Return):
                    return keys_of(sub.value)
    return None


def _spawn_keywords(tree, node):
    """(kwargs as key -> ast node, unresolved) for one spawn call."""
    import ast

    kwargs, unresolved = {}, []
    for kw in node.keywords:
        if kw.arg is not None:
            kwargs[kw.arg] = kw.value
            continue
        # **something
        target = kw.value
        name = None
        if isinstance(target, ast.Call):
            f = target.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        elif isinstance(target, ast.Name):
            name = target.id
        merged = _dict_literal_of(tree, name) if name else None
        if merged is None:
            unresolved.append(name or "<expression>")
        else:
            kwargs.update(merged)
    return kwargs, unresolved


def _sources():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "fused_render"
    return root, sorted(root.rglob("*.py"))


def test_every_git_spawn_in_the_repo_can_posix_spawn():
    """The whole rule, statically, at every git spawn in the package.

    Checks argv[0] is resolved AND `close_fds=False` AND no `cwd=` — the three
    clauses that must hold together. Checking only the first is what let five
    sites ship forking with a green suite.
    """
    import ast

    root, files = _sources()
    problems = []
    for py in files:
        try:
            tree = ast.parse(py.read_text(errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _SPAWNERS):
                continue
            if not _is_git_argv(node):
                continue
            where = f"{py.relative_to(root)}:{node.lineno}"
            kwargs, unresolved = _spawn_keywords(tree, node)
            if unresolved:
                problems.append(
                    f"{where}: cannot determine spawn kwargs (**{', **'.join(unresolved)}"
                    ") — make the helper a plain dict literal in this file so this "
                    "check can read it, rather than leaving the site unverifiable")
                continue

            # argv[0] must be resolved, not a bare name.
            argv = node.args[0] if node.args else None
            if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
                first = argv.elts[0]
                if isinstance(first, ast.Constant) and not os.path.dirname(first.value):
                    problems.append(f"{where}: argv[0] is the bare name "
                                    f"{first.value!r} — CPython forks")

            cf = kwargs.get("close_fds")
            if cf is None:
                problems.append(f"{where}: no close_fds — defaults to True, so "
                                "CPython forks and the child SIGSEGVs before exec")
            elif not (isinstance(cf, ast.Constant) and cf.value is False):
                problems.append(f"{where}: close_fds is not the literal False")

            cwd = kwargs.get("cwd")
            if cwd is not None and not (isinstance(cwd, ast.Constant)
                                        and cwd.value is None):
                problems.append(f"{where}: passes cwd=, which forces the fork "
                                "path on its own — use `-C <root>` instead")

            for forcer in ("preexec_fn", "pass_fds", "start_new_session"):
                val = kwargs.get(forcer)
                if val is not None and not (isinstance(val, ast.Constant)
                                            and not val.value):
                    problems.append(f"{where}: {forcer}= forces the fork path")

    assert not problems, (
        f"{len(problems)} git spawn(s) would fork, and a fork in a process with "
        "libproj resident dies with SIGSEGV before exec:\n  "
        + "\n  ".join(problems))


def test_the_sweep_actually_catches_each_violation(tmp_path):
    """The backstop's own regression test.

    A sweep that cannot fail is not a backstop, and this branch shipped one that
    could not. So the detector is pointed at deliberately broken sources —
    including the multi-line and tuple spellings the old regex missed — and must
    object to every one.
    """
    import ast

    bad = [
        # multi-line argv: the spelling the regex version could not see
        'subprocess.run(\n    ["git", "-C", root,\n     "status"],\n'
        '    close_fds=False)',
        # tuple argv
        'subprocess.run(("git", "status"), close_fds=False)',
        # absolute argv but no close_fds
        'subprocess.run([_git_bin(), "status"])',
        # absolute argv, close_fds ok, but cwd passed
        'subprocess.run([_git_bin(), "status"], close_fds=False, cwd=root)',
        # kwargs hidden behind an unreadable helper
        'subprocess.run([_git_bin(), "status"], **make_kwargs(root))',
        # executable= spelling
        'subprocess.run(["x"], executable="git", close_fds=False)',
    ]
    for src in bad:
        tree = ast.parse(src)
        call = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in _SPAWNERS)
        assert _is_git_argv(call), f"not recognised as a git spawn:\n{src}"
        kwargs, unresolved = _spawn_keywords(tree, call)
        argv = call.args[0] if call.args else None
        bare = (isinstance(argv, (ast.List, ast.Tuple)) and argv.elts
                and isinstance(argv.elts[0], ast.Constant)
                and not os.path.dirname(argv.elts[0].value))
        cf = kwargs.get("close_fds")
        bad_cf = cf is None or not (isinstance(cf, ast.Constant) and cf.value is False)
        cwd = kwargs.get("cwd")
        bad_cwd = cwd is not None and not (isinstance(cwd, ast.Constant)
                                          and cwd.value is None)
        assert unresolved or bare or bad_cf or bad_cwd, (
            f"the sweep would have PASSED this:\n{src}")

    # …and must not object to the correct form.
    good = 'subprocess.run([_git_bin(), "-C", root, "status"], close_fds=False)'
    tree = ast.parse(good)
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    kwargs, unresolved = _spawn_keywords(tree, call)
    assert not unresolved
    assert isinstance(kwargs["close_fds"], ast.Constant)
    assert kwargs["close_fds"].value is False
    assert "cwd" not in kwargs


def test_the_sweep_resolves_a_kwargs_helper():
    """`**_popen_kwargs()` must be READ, not waved through — four modules use it,
    and that indirection is where the missed sites hid their missing close_fds."""
    import ast

    src = (
        'def _popen_kwargs():\n'
        '    return {"stdin": subprocess.DEVNULL, "close_fds": False}\n'
        'subprocess.run([_git_bin(), "status"], **_popen_kwargs())\n')
    tree = ast.parse(src)
    call = next(n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "run")
    kwargs, unresolved = _spawn_keywords(tree, call)
    assert not unresolved, unresolved
    assert kwargs["close_fds"].value is False

    # The same helper WITHOUT close_fds must be caught, not merged silently.
    src_bad = src.replace(', "close_fds": False', "")
    tree_bad = ast.parse(src_bad)
    call_bad = next(n for n in ast.walk(tree_bad)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "run")
    kwargs_bad, unresolved_bad = _spawn_keywords(tree_bad, call_bad)
    assert not unresolved_bad
    assert "close_fds" not in kwargs_bad


def test_git_bin_is_absolute_and_cached():
    from fused_render.server import gitignore

    first = gitignore.git_bin()
    assert os.path.isabs(first) or first == "git"   # "git" only with no PATH
    assert gitignore.git_bin() is first             # cached, not re-resolved
    if first != "git":
        assert os.path.basename(first) in ("git", "git.exe")
        assert os.access(first, os.X_OK)
