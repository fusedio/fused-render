"""Templates must not import `fused_render` (SPEC PY-15).

A template's `.py` files run as a CHILD PROCESS of the app, and the app is not
the only thing that spawns them: the fused local execution backend strips
PYTHONPATH / PYTHONHOME / VIRTUAL_ENV from its children for venv hermeticity, and
some templates run in a uv venv of their own (zarr_aoi's tile daemon, pyramid's
worker). In every one of those interpreters `import fused_render` fails.

It used to fail SILENTLY, which is what made it worth a guard: the imports were
all written `try: from fused_render... except: <fallback>`, so under the fused
engine the fallback was the real behavior on every run — a read-only mount looked
writable, a mount-backed vault got walked or refused for the wrong reason, a tile
daemon read from the baseline port. Nothing raised; the answers were just wrong.

So a template asks the app about its environment through
`templates/shared/appenv.py` — env vars only, stdlib only — and this test pins
that there is no other route. AST-based, not grep-based: "fused_render" appears
all over the templates' prose (docstrings citing shell modules, comments
explaining why the boundary exists) and only real import statements count.
"""
import ast
import os

import pytest

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates")

# Documented exceptions, `<relpath>: <reason>`. Keep this EMPTY if you possibly
# can; an entry is a template that behaves differently depending on which engine
# ran it. Nothing more may be added without the reason being about the app, not
# about convenience.
ALLOWED = {
    # The reader gate is a GLOBAL preference switch, and `reader_enabled` is an
    # in-server concern rather than a mount fact. condition.py is exec'd
    # IN-PROCESS by server._run_condition (never through run_python), so the
    # package is genuinely importable here and using the app's own resolution
    # keeps one definition of the pref. It already falls back to a stdlib read of
    # prefs.json under `shared/appenv.home_dir()` if the import fails, so even
    # this site does not depend on the package being reachable.
    os.path.join("reader", "condition.py"):
        "in-server-only prefs read, with a stdlib fallback (not a mount fact)",
}


def _py_files():
    for dirpath, dirnames, filenames in os.walk(TEMPLATES):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", "vendor", "node_modules")]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.relpath(os.path.join(dirpath, name), TEMPLATES)


def _fused_render_imports(path):
    """Every real `import fused_render...` / `from fused_render... import ...` in
    `path`, as (lineno, source-ish) pairs. Relative imports are ignored — they
    cannot name `fused_render` — and so is every string that merely mentions it."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "fused_render":
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module \
                    and node.module.split(".")[0] == "fused_render":
                names = ", ".join(a.name for a in node.names)
                found.append((node.lineno, f"from {node.module} import {names}"))
    return found


def test_no_template_imports_fused_render():
    offenders = {}
    for rel in _py_files():
        hits = _fused_render_imports(os.path.join(TEMPLATES, rel))
        if hits and rel not in ALLOWED:
            offenders[rel] = hits
    assert not offenders, (
        "templates must reach the app through templates/shared/appenv.py "
        "(env vars, stdlib only) — see SPEC PY-15:\n"
        + "\n".join(f"  {rel}:{ln}: {src}"
                    for rel, hits in sorted(offenders.items())
                    for ln, src in hits))


def test_the_allowlist_has_no_stale_entries():
    """An allowlisted file that no longer imports the package must leave the list,
    or the exception outlives the reason for it."""
    stale = [rel for rel in ALLOWED
             if not _fused_render_imports(os.path.join(TEMPLATES, rel))]
    assert not stale, f"no longer imports fused_render, drop from ALLOWED: {stale}"


def test_the_guard_actually_sees_an_import(tmp_path):
    """The guard is only worth having if it can fail — a `from fused_render...`
    that the AST walk missed would make every other assertion here vacuous."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""A docstring mentioning fused_render, which is NOT an import."""\n'
        "# neither is this comment about fused_render.shell.mounts\n"
        "NAME = 'fused_render.calls'\n"
        "import os\n"
        "from . import sibling\n"
        "def f():\n"
        "    from fused_render.shell.mounts import is_mount_backed\n"
        "    import fused_render.calls\n",
        encoding="utf-8")
    hits = _fused_render_imports(str(sample))
    assert [src for _, src in hits] == [
        "from fused_render.shell.mounts import is_mount_backed",
        "import fused_render.calls",
    ], hits


# ------------------------------------------------------- the sanctioned route
# (`shared/appenv.py` being stdlib-only is pinned in tests/test_template_appenv.py,
# alongside the rest of its contract.)

# Each migrated site, asked about BOTH a read-only-mount path (MOUNTED) and an
# equivalent local one (LOCAL). Both answers matter: several of these sites fail
# CLOSED when they cannot tell, so the mounted answer alone would be satisfied by
# a site whose appenv import is broken. The local answer is what proves the site
# can actually reach appenv and is discriminating rather than merely refusing.
MIGRATED = [
    # (relpath, expression over `mod` returning [mounted, local], expected)
    (os.path.join("markdown", "graph.py"),
     "[mod.main(action='note', file=MOUNTED)['error'],"
     " mod.main(action='note', file=LOCAL)['error']]",
     ["mount_unsupported", None]),
    # The claude snapshot gate consults appenv.is_mount_backed: a mounted
    # target is refused with a sentence, a local one gets the empty "allowed"
    # answer — which is the discriminating pair this table wants. (The
    # sidecar-path rows that used to sit here went with the sidecar, D335;
    # annotate.py no longer touches appenv directly at all, so it has no row.)
    (os.path.join("claude", "agent.py"),
     "[bool(mod._snap_target(MOUNTED)), bool(mod._snap_target(LOCAL))]",
     [True, False]),
    (os.path.join("zarr_aoi", "tile_server.py"),
     "[mod.appenv.is_mount_backed(MOUNTED), mod.appenv.is_mount_backed(LOCAL)]",
     [True, False]),
    (os.path.join("graph", "condition.py"),
     "[mod.main(os.path.dirname(MOUNTED)), mod.main(os.path.dirname(LOCAL))]",
     [False, True]),
]


@pytest.mark.parametrize("rel,expr,expected", MIGRATED,
                         ids=[m[0] for m in MIGRATED])
def test_a_migrated_site_answers_with_fused_render_unimportable(
        rel, expr, expected, tmp_path):
    """The end-to-end property, in the interpreter the fused backend actually
    gives a template: PYTHONPATH cleared, every `fused_render`-bearing sys.path
    entry stripped, cwd outside the repo.

    The no-import guard above is necessary but not sufficient — a site could pass
    it and still fail at runtime by forgetting to put `../shared` on sys.path,
    which is exactly the silent wrong-answer failure PY-15 exists to end. So each
    site is asked a real question about a real read-only mount path and must get
    it right with the package unreachable.
    """
    import json
    import subprocess
    import sys
    import textwrap

    # Two structurally identical trees, one under the read-only mountpoint the
    # env names and one plainly local — `index.md` in both so the graph gate has
    # a reason to say True for the local one.
    mounts = tmp_path / "home" / "mounts"
    mounted_dir = mounts / "pub"
    local_dir = tmp_path / "local"
    for d in (mounted_dir, local_dir):
        d.mkdir(parents=True)
        (d / "index.md").write_text("# i\n", encoding="utf-8")
        (d / "note.md").write_text("# x\n", encoding="utf-8")

    script = textwrap.dedent(f"""
        import importlib.util, json, os, sys
        sys.path = [p for p in sys.path
                    if not os.path.isdir(os.path.join(p, "fused_render"))]
        try:
            import fused_render
            raise SystemExit("fused_render was importable; test setup is wrong")
        except ImportError:
            pass

        MOUNTED = {str(mounted_dir / "note.md")!r}
        LOCAL = {str(local_dir / "note.md")!r}
        spec = importlib.util.spec_from_file_location(
            "_under_test", {os.path.join(TEMPLATES, rel)!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print(json.dumps({{"value": {expr}}}))
    """)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["FUSED_RENDER_HOME_DIR"] = str(tmp_path / "home")
    env["FUSED_RENDER_MOUNTS_DIR"] = str(mounts)
    env["FUSED_RENDER_RO_MOUNTS"] = str(mounted_dir)

    out = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path),
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1])["value"] == expected
