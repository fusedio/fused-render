"""What a runner DECLARES has to be enough to build a venv that imports.

`uv sync` from a runner's `pyproject.toml` is the only thing between a fresh
install and a working engine, and it is not covered by anything else here: the
lockfiles are gitignored, the venvs are built on the user's machine, and every
other AI test stubs the libraries out. So a package that resolves, downloads and
installs perfectly while being unimportable passes the entire suite — which is
exactly what shipped.

**The defect this file exists for.** `sherpa-onnx` 1.13.5 publishes TWO shapes of
wheel under one version: the `linux_armv7l` ones are self-contained (12MB, with
`libonnxruntime.so` bundled, declaring no dependencies) while every other wheel
is a ~2MB shim carrying only `_sherpa_onnx…so` and declaring
`Requires-Dist: sherpa-onnx-core==1.13.5` for the library it links against. Its
sdist's PKG-INFO declares no dependencies at all. `uv lock` produces a UNIVERSAL
resolution — one dependency set per package, for every platform at once — so it
takes a single metadata source and took the empty one: `sherpa-onnx-core` never
entered either lock, `uv sync` installed the shim without its other half, and the
first diarized transcription on either engine died with

    ImportError: dlopen(.../sherpa_onnx/lib/_sherpa_onnx.cpython-312-darwin.so):
    Library not loaded: @rpath/libonnxruntime.dylib

**Why it was not caught.** `uv pip install sherpa-onnx` resolves for the CURRENT
platform, reads the real wheel's METADATA and pulls the companion — so a hand-made
scratch venv works, every stubbed test passes, and the failure appears only in a
venv the shipped installer built. "It worked when I tested it" was true and
useless.

**What can and cannot be automated here, stated rather than implied.** The
declaration guard below is the whole of the automatic protection. A test that
built a runner venv and imported out of it was written first and then deleted:
`conftest.py` points `FUSED_RENDER_HOME` at a temp directory for the whole
session, so `envinstall.is_installed` is False for every runner and the test
skipped unconditionally — and CI could not run it anyway, having no Apple
Silicon for mlx and no appetite for a multi-minute network install per case. A
test that cannot fire is worse than no test, because it reads as protection;
that is the same rule `test_the_split_table_is_not_quietly_unused` enforces on
the table below. The from-scratch rebuild is therefore a MANUAL check, and D309
records both what it verified and that it is manual.
"""
import os

import pytest


def _import_toml():
    """tomllib (3.11+) or the tomli dependency that covers 3.10.

    Same shape as `tests/test_template_locks.py`'s and
    `tests/test_bundle_contents.py`'s, and copied rather than shared for the
    reason the first of those states by cross-referencing the second: the
    duplication is deliberate and acknowledged in place. `tests/` does have a
    convention for shared helpers (`_git_repo.py`, `_theme_sources.py`, …), so
    extracting a `tests/_toml.py` would be a reasonable cleanup — but it would
    edit two suites unrelated to this branch, and `test_engine.py`'s version is
    a skipif MARKER rather than a parser accessor and would not migrate anyway.

    **A bare `import tomllib` is what this exists to prevent, and writing one
    here is what shipped a red CI.** `requires-python` is >=3.10, tomllib is
    3.11+ stdlib, and on 3.10 the bare import raises ModuleNotFoundError at
    COLLECTION — which errors the whole module out rather than skipping it, so a
    dependency-integrity suite silently stops running on the interpreter version
    it is most likely to catch a packaging bug on. Local runs are on 3.12 and
    cannot see it.

    `tomli>=2.0; python_version < '3.11'` is a declared dependency of this
    project (`pyproject.toml`), so on 3.10 these tests RUN — the skip is only
    for an install that genuinely has neither. `allow_module_level=True`
    because this is called at import time, where a plain `pytest.skip` is an
    error rather than a skip.
    """
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            pytest.skip("needs tomllib (3.11+) or the tomli package",
                        allow_module_level=True)
    return tomllib


tomllib = _import_toml()

RUNNERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners",
)


#: Distributions whose OWN metadata will not carry a package they need into a
#: universal lock, and the companion that therefore has to be named by hand.
#:
#: A table rather than one assertion, because this is a CLASS of packaging
#: defect and not a fact about sherpa: any project that ships both bundled and
#: split wheels under one version, or whose sdist understates its dependencies,
#: lands here. Adding the next one is a data edit.
#:
#: `reason` is what the failure message says, because the person who trips this
#: test is most likely the person about to delete the "redundant" line.
SPLIT_DISTRIBUTIONS = {
    "sherpa-onnx": {
        "companion": "sherpa-onnx-core",
        "reason": (
            "sherpa-onnx's wheels declare `Requires-Dist: sherpa-onnx-core` but "
            "its sdist declares nothing, and its linux_armv7l wheels bundle the "
            "library instead — so uv's universal lock resolves it with NO "
            "dependencies and `uv sync` installs a sherpa_onnx that cannot "
            "import (Library not loaded: @rpath/libonnxruntime.dylib). The "
            "companion holds the actual libonnxruntime and must be declared "
            "directly. It is not a duplicate; see the runner's pyproject."
        ),
    },
}


def _runner_folders():
    return sorted(
        os.path.join(RUNNERS_DIR, name) for name in os.listdir(RUNNERS_DIR)
        if os.path.isfile(os.path.join(RUNNERS_DIR, name, "pyproject.toml"))
    )


def _declared(folder):
    """The distribution names a runner declares, normalized.

    Normalized because `huggingface_hub` and `huggingface-hub` are the same
    distribution to a resolver and different strings to a test — and a guard
    that can be defeated by an underscore is not a guard.
    """
    with open(os.path.join(folder, "pyproject.toml"), "rb") as handle:
        data = tomllib.load(handle)
    names = set()
    for spec in data.get("project", {}).get("dependencies", []):
        name = spec.split("[")[0].split(";")[0]
        for operator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            name = name.split(operator)[0]
        names.add(name.strip().replace("_", "-").lower())
    return names


def test_there_are_runner_folders_to_check():
    """The tests below iterate a directory listing, and an empty one would make
    every parametrized case vacuously pass."""
    assert len(_runner_folders()) >= 2, RUNNERS_DIR


@pytest.mark.parametrize("folder", _runner_folders(), ids=os.path.basename)
def test_a_runner_naming_a_split_distribution_names_its_other_half(folder):
    """The guard against `uv sync` building a venv that cannot import.

    Both whisper runners declare `sherpa-onnx` for diarization, and both need
    the companion for the same reason — "identical on both engines" (AI-10c)
    includes being installable on both. This iterates every runner rather than
    naming the two, so a third runner that reaches for sherpa gets the same
    answer without anyone remembering.
    """
    declared = _declared(folder)
    for primary, entry in SPLIT_DISTRIBUTIONS.items():
        if primary not in declared:
            continue
        assert entry["companion"] in declared, (
            f"{os.path.basename(folder)} declares {primary} without "
            f"{entry['companion']}.\n\n{entry['reason']}")


def test_the_split_table_is_not_quietly_unused():
    """A table nothing matches is a guard that has silently stopped guarding —
    the dependency renamed, the runner deleted, the entry left behind reading
    as protection. At least one runner must exercise each entry."""
    everything = set()
    for folder in _runner_folders():
        everything |= _declared(folder)
    for primary in SPLIT_DISTRIBUTIONS:
        assert primary in everything, (
            f"nothing declares {primary} any more — delete its entry rather "
            f"than leaving a rule that cannot fire")
