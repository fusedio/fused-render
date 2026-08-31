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
import ast
import os
import sys

import pytest


def _import_toml():
    """tomllib, with a `tomli` arm the >=3.11 floor makes unreachable.

    Same shape as `tests/test_template_locks.py`'s and
    `tests/test_bundle_contents.py`'s, and copied rather than shared for the
    reason the first of those states by cross-referencing the second: the
    duplication is deliberate and acknowledged in place. `tests/` does have a
    convention for shared helpers (`_git_repo.py`, `_theme_sources.py`, …), so
    extracting a `tests/_toml.py` would be a reasonable cleanup — but it would
    edit two suites unrelated to this branch, and `test_engine.py`'s version is
    a skipif MARKER rather than a parser accessor and would not migrate anyway.

    **A bare `import tomllib` is what this exists to prevent, and writing one
    here is what shipped a red CI.** Back when `requires-python` was >=3.10 the
    bare import raised ModuleNotFoundError at COLLECTION — erroring the whole
    module out rather than skipping it, so a dependency-integrity suite silently
    stopped running on the interpreter version most likely to catch a packaging
    bug. Local runs were on 3.12 and could not see it.

    The floor is >=3.11 now, so `tomllib` is always importable and `tomli` is no
    longer a declared dependency: the fallback and the skip are both unreachable.
    They stay as the shape that keeps a bare module-scope import from coming
    back. `allow_module_level=True` because this is called at import time, where
    a plain `pytest.skip` is an error rather than a skip.
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
    "ltx-pipelines-mlx": {
        "companion": "ltx-core-mlx",
        "reason": (
            "ltx-pipelines-mlx depends on ltx-core-mlx BY NAME with no version "
            "bound — neither package is on PyPI, so the dependency can only be "
            "satisfied by a `[tool.uv.sources]` entry this folder supplies "
            "itself; upstream's own workspace mapping (a root uv.lock) is not "
            "visible from a runner's bare `uv sync`. Naming only ltx-pipelines-"
            "mlx builds a venv `uv sync` cannot resolve at all, which is a "
            "louder failure than sherpa-onnx's silent import error but the "
            "same class of defect: a project whose own metadata will not carry "
            "its other half into this folder's environment."
        ),
    },
    "onnxruntime-rocm": {
        "companion": "numpy",
        "reason": (
            "onnxruntime-rocm's wheel declares NO dependencies at all — "
            "dry-resolving `onnxruntime-rocm>=1.22,<2` against PyPI returns "
            "exactly one package, itself — where onnxruntime and "
            "onnxruntime-gpu both carry numpy in transitively. That silence "
            "is not cosmetic: `onnxruntime/__init__.py` re-raises its own "
            "capi ImportError when numpy is missing, so `import onnxruntime` "
            "fails outright, not just the array arithmetic downstream. `uv "
            "sync` still resolves and installs a complete-looking venv; the "
            "onnx_embed_rocm/ worker spawns and loads the model and only dies "
            "on the first forward pass with `ModuleNotFoundError: No module "
            "named 'numpy'`. The companion must be declared directly; see "
            "onnx_embed_rocm/pyproject.toml."
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


def _declared_indexes(folder):
    """Every `[[tool.uv.index]]` table a runner declares, as raw dicts."""
    with open(os.path.join(folder, "pyproject.toml"), "rb") as handle:
        data = tomllib.load(handle)
    return data.get("tool", {}).get("uv", {}).get("index", [])


def test_there_are_runner_folders_to_check():
    """The tests below iterate a directory listing, and an empty one would make
    every parametrized case vacuously pass."""
    assert len(_runner_folders()) >= 2, RUNNERS_DIR


@pytest.mark.parametrize("folder", _runner_folders(), ids=os.path.basename)
def test_a_non_pypi_index_is_explicit(folder):
    """AI-2a's wheels-only rule, amended (D411): a non-PyPI index is
    admissible ONLY confined to the one distribution it exists for.

    `uv sync` runs bare, with no `--index`/`--extra` a caller could supply
    (PY-18), so everything about where a dependency comes from has to be
    expressible in the manifest sitting beside it — which is exactly why
    `explicit = true` matters. Without it, ANY extra index becomes a
    candidate for EVERY requirement in the graph. The removed
    `transformers_text/pyproject.toml` measured this directly (of 45 locked
    packages, 42 came from PyPI and only `torch` from the PyTorch mirror)
    precisely because its index was `explicit`; that folder went at D416 and the
    measurement stays here, since it is the evidence for the rule rather than a
    fact about torch. An index missing that flag is not a smaller version of the
    same risk — it is the mirror silently answering for packages nobody
    pointed it at, which is indistinguishable from a supply-chain substitution
    until something breaks. This is the general form of the rule
    `llamacpp_text/pyproject.toml`'s own index relies on, so the NEXT runner
    that reaches for a non-PyPI source is caught by the same check rather
    than needing a new one written for it.

    Silent on a PyPI-only runner (most of them): this asserts something about
    every DECLARED index, and a runner with none declares nothing to check —
    the same "absent rather than an empty entry" shape `engine_options.py`'s
    table uses.
    """
    for index in _declared_indexes(folder):
        url = str(index.get("url", ""))
        if "pypi.org" in url:
            continue
        assert index.get("explicit") is True, (
            f"{os.path.basename(folder)} declares the non-PyPI index {url!r} "
            f"without explicit = true — without it this index becomes a "
            f"candidate for every dependency in the graph, not only the one "
            f"it exists for."
        )


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


# -- the three diffusers_image* manifests' dependency parity -----------------
#
# Same shape as the onnx_embed four-way check below, for the same reason: these
# three folders are declared, in their own header comments, to be one
# dependency list with only `torch` (and ROCm's extra `triton-rocm`, which
# rides in for the reason that folder's header explains) swapped per hardware.
# A hand-maintained list that drifts between them would mean "which engine
# row the user picked" quietly also means "which quantization backends are
# available" — which is exactly the shape of the bitsandbytes omission this
# suite exists to catch: it was missing from all three, so nothing here would
# have caught it landing in only one or two either.
_DIFFUSERS_IMAGE_FOLDERS = ("diffusers_image", "diffusers_image_cuda",
                            "diffusers_image_rocm")

#: Names legitimately absent from the comparison because they encode WHERE
#: torch comes from rather than WHAT the runner needs: `torch` itself is
#: pinned identically in all three but sourced from a different index per
#: folder, and `triton-rocm` exists only because ROCm's torch wheel declares
#: it as a transitive dependency PyPI cannot satisfy (see
#: `diffusers_image_rocm/pyproject.toml`'s header) — it has no counterpart to
#: agree with on the other two folders by construction, not by drift.
_DIFFUSERS_IMAGE_HARDWARE_SPECIFIC = {"torch", "triton-rocm"}


def test_the_three_diffusers_image_manifests_agree_beyond_torch_and_triton():
    """The guard the bitsandbytes bug needed and did not have.

    `tonera/FLUX.2-klein-4B-int8-diffusers` — the sole, `recommended: True`
    `diffusers-image` suggestion — ships a torchao transformer AND a
    bitsandbytes-NF4 text encoder, so all three folders need both
    quantization backends or the model a bare `fused.ai.image()` starts
    fails with an ImportError raised before the transformer is ever built
    (see `catalog.py`'s entry). `bitsandbytes` could have been added to one
    folder and forgotten in the other two — the venvs are built independently
    on the user's machine, so nothing short of this comparison would notice —
    and this test exists so that class of drift is a red CI line instead of a
    hardware-specific failure report.
    """
    shared = None
    for name in _DIFFUSERS_IMAGE_FOLDERS:
        declared = _declared(os.path.join(RUNNERS_DIR, name))
        rest = sorted(d for d in declared
                       if d not in _DIFFUSERS_IMAGE_HARDWARE_SPECIFIC)
        if shared is None:
            shared = rest
        assert rest == shared, name


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


# -- the onnx_embed* four-way manifest parity (the numpy regression itself) ----
#
# `SPLIT_DISTRIBUTIONS` above catches "this distribution's own metadata won't
# carry a needed package". The two tests below catch the other half of the
# same numpy bug: a manifest can be short a package `runners/onnx_embed.py`
# itself imports directly, with no distribution's metadata to blame at all —
# and with four near-identical manifests, a hand-copied list is exactly what
# lets one of them fall behind the other three unnoticed.

_ONNX_EMBED_FOLDERS = ("onnx_embed", "onnx_embed_cuda", "onnx_embed_directml",
                       "onnx_embed_rocm")

_ONNX_EMBED_WORKER_PATH = os.path.join(RUNNERS_DIR, "onnx_embed.py")

#: Import names whose PyPI distribution name is not the import name itself.
#: Everything `runners/onnx_embed.py` imports besides `PIL` happens to match
#: its distribution 1:1 once `_declared`'s own normalization (underscores to
#: hyphens, lowercased) is applied.
_IMPORT_TO_DISTRIBUTION = {"PIL": "pillow"}

#: Local, same-folder modules `onnx_embed.py` imports off `sys.path` rather
#: than off PyPI — not something any manifest should ever list.
_ONNX_EMBED_LOCAL_MODULES = {"embed_common", "formats", "worker_base"}

#: The one import name legitimately satisfied by a distribution whose name it
#: is only a PREFIX of: `import onnxruntime` is answered by whichever of
#: `onnxruntime` / `onnxruntime-gpu` / `onnxruntime-directml` /
#: `onnxruntime-rocm` a given folder declares. Nothing else gets this
#: leniency — a generic prefix match would also let a declared `pillow-heif`
#: silently stand in for an imported `pillow`, which is the exact shape of
#: hole the second test below exists to close.
_PREFIX_MATCHED_IMPORT = "onnxruntime"


def _onnx_embed_third_party_imports():
    """Every top-level module `runners/onnx_embed.py` imports, module-level or
    lazily inside a function, that is neither stdlib nor one of this runner's
    own sibling files — parsed with `ast`, not copied by eye, because a
    hand-copied list is exactly what let `numpy` go undeclared in the first
    place."""
    with open(_ONNX_EMBED_WORKER_PATH) as handle:
        tree = ast.parse(handle.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    names -= sys.stdlib_module_names
    names -= _ONNX_EMBED_LOCAL_MODULES
    return {_IMPORT_TO_DISTRIBUTION.get(name, name) for name in names}


def test_the_four_onnx_embed_manifests_agree_beyond_their_onnxruntime_line():
    """The four `onnx_embed*` folders' dependency sets are meant to be one set
    with a single distribution swapped — the manifests say so themselves. If a
    dependency is added or dropped in one folder and not the other three,
    "which hardware" quietly starts also meaning "which tokenizer"."""
    shared = None
    for name in _ONNX_EMBED_FOLDERS:
        declared = _declared(os.path.join(RUNNERS_DIR, name))
        rest = sorted(d for d in declared if not d.startswith("onnxruntime"))
        if shared is None:
            shared = rest
        assert rest == shared, name


def test_every_third_party_import_of_onnx_embed_is_declared_in_all_four():
    """The regression itself: `onnx_embed.py` is free to `import` anything, but
    only `onnxruntime`'s own transitive dependencies rode along for free — and
    `onnxruntime-rocm` declares NONE (see `SPLIT_DISTRIBUTIONS` above), so a
    name the mainline wheel happened to pull (this is how `numpy` went
    missing) is invisible on every other distribution and fatal on that one.
    Every third-party import this runner makes, lazy ones included, must be a
    distribution named in all four manifests — matched EXACTLY, except for
    `onnxruntime` itself (see `_PREFIX_MATCHED_IMPORT`)."""
    imported = _onnx_embed_third_party_imports()
    assert imported, "the parser found nothing — it is broken, not the runner"
    for name in _ONNX_EMBED_FOLDERS:
        declared = _declared(os.path.join(RUNNERS_DIR, name))
        missing = set()
        for imp in imported:
            normalized = imp.replace("_", "-").lower()
            if normalized == _PREFIX_MATCHED_IMPORT:
                if not any(d.startswith(normalized) for d in declared):
                    missing.add(imp)
            elif normalized not in declared:
                missing.add(imp)
        assert not missing, f"{name}: undeclared imports {missing}"
