"""The llama.cpp / GGUF text runner's own behaviour — what is true of
llama-cpp-python and nothing else.

The contract half (routes, states, progress, the port handshake) is
`worker_base`'s and is covered by `tests/test_ai_worker_base.py`. What is left
here is what this runner decides for itself: which ids it will fetch (a
curated table, PLUS an uncurated repo id resolved by its own file listing
since D412), how it renders a GGUF's own embedded chat template, and how it
streams and cancels without a producer thread.

Loaded by PATH with `worker_base` primed in `sys.modules`, the way
`tests/test_ai_transformers_worker.py` did before D416 removed it: the runner
finds its base off `sys.path` in an interpreter of its own, so importing it the
packaged way would be testing an import that never ships. Neither `llama_cpp`
nor `huggingface_hub` is installed in this test venv, per AI-11c — every path
that would touch the former works against `_loaded["llm"]` set directly to a
fake, and every path that would touch the latter fakes it via
`_fake_huggingface_hub` below.
"""
import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

WORKER_PATH = str(
    Path(__file__).resolve().parents[1]
    / "fused_render" / "ai" / "runners" / "llama_text.py"
)


@pytest.fixture()
def worker(monkeypatch):
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_file = lambda repo, filename, **kw: f"/blobs/{repo}/{filename}"
    base.serve_calls = []
    base.serve = lambda **kw: base.serve_calls.append(kw)
    base.recorded = {}
    base.set_state = lambda **fields: base.recorded.update(fields)
    # A read-only cache lookup in real life (`worker_base._cached_file`,
    # `try_to_load_from_cache` underneath — cannot start a download). The
    # fake mirrors that contract: a dict of already-cached (repo, file)
    # pairs a test populates, and a miss returns None rather than fetching
    # anything.
    base.cached_files = set()
    base._cached_file = (
        lambda repo, filename: f"/blobs/{repo}/{filename}"
        if (repo, filename) in base.cached_files else None)
    # No local hf cache folder in these tests unless a test says otherwise —
    # `_locally_cached_gguf_files`'s fast path reads `None` as "nothing on
    # disk" and falls through to the networked listing, the same way the real
    # function does for a repo hf's cache has never heard of.
    base.repo_folder = lambda model_id, repo_type="model": None
    # `_remote_gguf_sizes`'s one seam (SPEC AI-24 item 14's real wiring) —
    # `worker_base._repo_files` returns `(sha, [(name, size), ...])` in real
    # life; the fake defaults to an empty listing (so budget-aware
    # resolution falls straight through to the curated recipe, its own "no
    # listing, no verdict" branch) and a test overrides it to exercise the
    # picker.
    base._repo_files = lambda model_id, **kw: (None, [])

    monkeypatch.setitem(sys.modules, "worker_base", base)
    spec = importlib.util.spec_from_file_location(
        "llamacpp_text_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.worker_base = base
    monkeypatch.delenv(module._MEMORY_BUDGET_ENV, raising=False)
    return module


# -- the curated table, and D412's generic fallback for everything else -----
#
# A GGUF repo commonly publishes two dozen quantizations of one model.
# `formats.GGUF_RECIPES` curates 5 of them by hand, but since D412 a repo
# this table has never heard of resolves too — Piece 1's picker
# (`formats.pick_gguf_file`) runs over the repo's own file listing instead of
# refusing outright.


def _fake_huggingface_hub(monkeypatch, *, files=None, error=None):
    """A fake `huggingface_hub` good enough for `_resolve_uncurated_repo`'s
    networked path — `list_repo_files` either returns `files` or raises
    `error`, mirroring the one function this module calls."""
    fake = types.ModuleType("huggingface_hub")
    calls = []

    def list_repo_files(model_id, **kwargs):
        calls.append(model_id)
        if error is not None:
            raise error
        return list(files or [])

    fake.list_repo_files = list_repo_files
    fake.calls = calls
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    return fake


def test_download_fetches_exactly_the_one_curated_file(worker):
    """No snapshot call, and no second repo — `worker_base.download_file` is
    the whole of it (see the module docstring on why there is no external
    tokenizer/config fetch for these repos)."""
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


def test_download_resolves_an_uncurated_repo_via_the_picker(worker, monkeypatch):
    """The blocking limit this piece exists to remove: a repo `GGUF_RECIPES`
    has never heard of used to refuse by name unconditionally. Now it reads
    the repo's own file listing (`huggingface_hub.list_repo_files`, faked
    here — real `huggingface_hub` is a genuine dependency of the wider app,
    unlike `llama_cpp`, so this asserts on the CALL rather than on
    `sys.modules` membership, which an earlier test in the same process may
    already have set for reasons that have nothing to do with this one) and
    runs `formats.pick_gguf_file` over it."""
    fake = _fake_huggingface_hub(monkeypatch, files=[
        "README.md", "model-Q4_K_M.gguf", "model-Q8_0.gguf",
    ])
    path = worker.download("some-org/not-in-the-table")
    assert path == "/blobs/some-org/not-in-the-table/model-Q4_K_M.gguf"
    assert fake.calls == ["some-org/not-in-the-table"]


def test_an_uncurated_repo_with_no_loadable_gguf_is_refused_by_name(worker, monkeypatch):
    """A real repo, successfully listed, but nothing in it is a chat model —
    all `mmproj`/auxiliary, or a format this app does not serve. Refused
    with a sentence naming the repo, never a silent smallest-file guess."""
    _fake_huggingface_hub(monkeypatch, files=["README.md", "model-mmproj-F16.gguf"])
    with pytest.raises(RuntimeError, match="no GGUF file"):
        worker.download("some-org/only-a-projector")


def test_an_uncurated_repos_lookup_failure_is_a_different_refusal(worker, monkeypatch):
    """Named apart from "no GGUF file" on purpose (see the module's
    `_LOOKUP_FAILED`/`_NO_GGUF_MATCH` docstrings): a network problem means
    "try again", not "this repo will never resolve"."""
    _fake_huggingface_hub(monkeypatch, error=RuntimeError("offline"))
    with pytest.raises(RuntimeError, match="[Cc]ould not read"):
        worker.download("some-org/unreachable")


def test_an_uncurated_repo_already_on_disk_needs_no_network(worker, monkeypatch, tmp_path):
    """The local-cache-first fast path: a repo already fully downloaded
    through this engine resolves from its own snapshot directory, with NO
    `list_repo_files` call — asserted by making the fake's call raise, since
    `huggingface_hub` (unlike `llama_cpp`) is a real dependency of the wider
    app and may already sit in `sys.modules` for reasons unrelated to this
    test, so membership alone cannot prove this path took no network."""
    folder = tmp_path / "models--some-org--already-here"
    snapshot = folder / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "model-Q4_K_M.gguf").write_bytes(b"x")
    worker.worker_base.repo_folder = lambda model_id, repo_type="model": (
        str(folder) if model_id == "some-org/already-here" else None)

    def boom(*_args, **_kwargs):
        pytest.fail("the local-cache-first fast path called the Hub anyway")

    fake = types.ModuleType("huggingface_hub")
    fake.list_repo_files = boom
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)

    path = worker.download("some-org/already-here")
    assert path == "/blobs/some-org/already-here/model-Q4_K_M.gguf"


def test_load_refuses_an_uncurated_repo_with_nothing_loadable_before_importing_llama_cpp(
        worker, monkeypatch):
    """The curation/resolution check still comes first — the order the removed
    `torch_text.load` also kept, checking its format refusal before importing
    torch: a model this runner cannot resolve is a fact about the request, not
    about llama.cpp, whether the refusal is "not curated" or, since D412,
    "no GGUF file here to pick"."""
    assert "llama_cpp" not in sys.modules
    _fake_huggingface_hub(monkeypatch, files=["README.md"])
    with pytest.raises(RuntimeError, match="no GGUF file"):
        worker.load("some-org/not-in-the-table", "/blobs/whatever.gguf")
    assert "llama_cpp" not in sys.modules


# -- budget-aware curated resolution: item 14's REAL production wiring ------
# (SPEC AI-24, code review 2026-08-27 — "wire it for real")
#
# `_resolve_curated_recipe` is the honest seam: `_GGUF_RECIPES`' hard-coded
# filename is the FLOOR/DEFAULT, never silently swapped when it already
# fits — these tests prove the PRODUCTION path (`worker.download`, which is
# what a real "Download" click calls) picks by budget, not another isolated
# unit test of `formats.select_gguf_recipe`.


def test_the_curated_file_is_unchanged_when_no_budget_is_known(worker):
    """No `FUSED_AI_MEMORY_BUDGET_BYTES` in the environment (an older
    supervisor, or `fit.available_budget_bytes()` itself answered `None`) —
    the curated recipe is the floor/default and nothing here may guess."""
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


def test_the_curated_file_is_unchanged_when_it_already_fits_the_budget(
        worker, monkeypatch):
    """A GENEROUS budget must not change what downloads — the floor/default
    guarantee: a machine for which the curated recipe always worked sees
    ZERO behaviour change, even though a smaller quant also exists in the
    same listing (`select_gguf_recipe` alone, unconstrained by this
    guarantee, would happily pick the smaller one — this function must
    NOT let it, once the curated file already fits)."""
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", str(100 * 1024 ** 3))
    worker.worker_base._repo_files = lambda model_id, **kw: (
        "sha", [("gemma-4-E4B-it-Q4_K_M.gguf", 5 * 1024 ** 3),
               ("gemma-4-E4B-it-Q2_K.gguf", 2 * 1024 ** 3)])
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


def test_a_tight_budget_downgrades_to_the_best_fitting_alternative(
        worker, monkeypatch):
    """The real behaviour change item 14 exists to make: a budget too small
    for the curated file picks the best-quality ALTERNATIVE from the same
    repo's own listing that fits — never a worse-than-necessary one, and
    never the curated file it cannot hold."""
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", str(3 * 1024 ** 3))
    worker.worker_base._repo_files = lambda model_id, **kw: (
        "sha", [("gemma-4-E4B-it-Q4_K_M.gguf", 5 * 1024 ** 3),
               ("gemma-4-E4B-it-Q3_K_M.gguf", 4 * 1024 ** 3),
               ("gemma-4-E4B-it-Q2_K.gguf", 2 * 1024 ** 3)])
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q2_K.gguf"


def test_nothing_in_the_repo_fits_falls_back_to_the_curated_file(
        worker, monkeypatch):
    """No alternative fits either — the curated recipe is returned anyway
    rather than refusing the download outright, matching `_offload_
    schedule`'s existing "slower load, never a refused download" backoff
    philosophy `formats.py`'s own module note gives for the uncurated
    picker."""
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", "1")
    worker.worker_base._repo_files = lambda model_id, **kw: (
        "sha", [("gemma-4-E4B-it-Q4_K_M.gguf", 5 * 1024 ** 3),
               ("gemma-4-E4B-it-Q2_K.gguf", 2 * 1024 ** 3)])
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


def test_no_listing_at_all_leaves_the_curated_recipe_unchanged(worker, monkeypatch):
    """A budget IS known but the repo's listing could not be obtained
    (nothing cached locally, the remote call fails) — nothing to pick FROM,
    so this degrades to the curated default rather than raising."""
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", str(1 * 1024 ** 3))

    def boom(model_id, **kw):
        raise RuntimeError("offline")

    worker.worker_base._repo_files = boom
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


def test_a_leftover_local_file_does_not_skip_the_curated_size_floor_check(
        worker, monkeypatch, tmp_path):
    """Code review finding: a NON-EMPTY local listing was treated as a
    complete listing, so a leftover smaller quant left on disk from a prior
    downgraded pick short-circuited the remote fetch entirely. With only
    that leftover file known, `curated_size = sizes.get(recipe["file"])` is
    None (the curated file itself was never cached), the floor check is
    skipped, and `select_gguf_recipe` is asked to choose from the ONE
    leftover file it has evidence for — even though the budget is generous
    enough that the curated file would obviously have passed the floor
    check, had the remote listing actually been consulted.

    The curated recipe is the unconditional floor/default: a local listing
    that does not even include the curated file's own size cannot answer
    the floor question, and must fall back to the full remote listing
    rather than being treated as though it already settled things."""
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", str(100 * 1024 ** 3))
    folder = tmp_path / "models--unsloth--gemma-4-E4B-it-GGUF"
    snapshot = folder / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    leftover = snapshot / "gemma-4-E4B-it-Q2_K.gguf"
    leftover.write_bytes(b"x" * 1024)
    worker.worker_base.repo_folder = lambda model_id, repo_type="model": (
        str(folder) if model_id == "unsloth/gemma-4-E4B-it-GGUF" else None)
    worker.worker_base._cached_file = lambda repo, filename: (
        str(leftover) if (repo, filename) ==
        ("unsloth/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q2_K.gguf") else None)
    # The curated file is NOT cached locally, but the repo's full remote
    # listing (which the buggy code never reaches) shows it comfortably
    # fits the 100GB budget.
    worker.worker_base._repo_files = lambda model_id, **kw: (
        "sha", [("gemma-4-E4B-it-Q4_K_M.gguf", 5 * 1024 ** 3),
               ("gemma-4-E4B-it-Q2_K.gguf", 2 * 1024 ** 3)])
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


def test_remote_gguf_sizes_keeps_an_unsized_file_as_none_rather_than_dropping_it(
        worker):
    """Code review finding: `_remote_gguf_sizes` built its dict with `if
    isinstance(size, int)`, which DROPS a file the Hub listing did not size
    (a common shape for a shard whose size metadata is momentarily
    unavailable) instead of keeping it as `None`. `formats.select_gguf_
    recipe` needs the key present with a `None` value to run its
    partial-shard estimate (averaging the KNOWN shards to price the
    missing one); a silently dropped key looks identical to "this file does
    not exist", which makes a shard set look smaller — and therefore
    cheaper — than it really is."""
    worker.worker_base._repo_files = lambda model_id, **kw: (
        "sha", [("model-Q4_K_M-00001-of-00002.gguf", 3 * 1024 ** 3),
               ("model-Q4_K_M-00002-of-00002.gguf", None)])
    sizes = worker._remote_gguf_sizes("org/repo")
    assert sizes == {
        "model-Q4_K_M-00001-of-00002.gguf": 3 * 1024 ** 3,
        "model-Q4_K_M-00002-of-00002.gguf": None,
    }


def test_a_local_cache_hit_is_preferred_over_a_remote_listing(
        worker, monkeypatch, tmp_path):
    """Local cache first, exactly like `_resolve_uncurated_repo` already
    keeps for an uncurated repo — but ONLY once the local listing can
    actually answer the floor question (code review, finding 1): the
    curated file's OWN size has to be among what is on disk, or a local
    listing that merely contains some OTHER leftover file would silently
    skip the curated-size floor check (see
    `test_a_leftover_local_file_does_not_skip_the_curated_size_floor_check`
    for that regression). Here BOTH the curated file and a previously
    downgraded pick are already on disk (a machine that downloaded the
    curated file once, found it too big to load, and downgraded) — the
    local listing settles the floor question by itself, so no network
    listing is needed to re-derive the same answer."""
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", str(3 * 1024 ** 3))
    folder = tmp_path / "models--unsloth--gemma-4-E4B-it-GGUF"
    snapshot = folder / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    curated_file = snapshot / "gemma-4-E4B-it-Q4_K_M.gguf"
    curated_file.write_bytes(b"x" * (5 * 1024 ** 3))
    local_file = snapshot / "gemma-4-E4B-it-Q2_K.gguf"
    local_file.write_bytes(b"x" * 1024)
    worker.worker_base.repo_folder = lambda model_id, repo_type="model": (
        str(folder) if model_id == "unsloth/gemma-4-E4B-it-GGUF" else None)
    # The REAL paths these filenames resolve to, so `os.path.getsize` reads
    # the files actually created above — the fixture's own `_cached_file`
    # fake returns a fabricated `/blobs/...` string unrelated to any real
    # file on disk, which is fine for every OTHER test here (they never
    # `os.path.getsize` it) but wrong for this one.
    _cached = {
        ("unsloth/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q4_K_M.gguf"): str(curated_file),
        ("unsloth/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q2_K.gguf"): str(local_file),
    }
    worker.worker_base._cached_file = lambda repo, filename: _cached.get((repo, filename))

    def boom(model_id, **kw):
        pytest.fail("the local-cache-first fast path called the Hub anyway")

    worker.worker_base._repo_files = boom
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q2_K.gguf"


def test_a_downgraded_pick_is_deterministic_across_repeated_resolves(
        worker, monkeypatch):
    """Re-resolving the SAME curated id under the SAME budget and the SAME
    listing must pick the SAME file every time — the self-consistency a
    "Download" click and a later "Load" click both depend on, since each
    calls `_resolve_model_id` independently."""
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", str(3 * 1024 ** 3))
    worker.worker_base._repo_files = lambda model_id, **kw: (
        "sha", [("gemma-4-E4B-it-Q4_K_M.gguf", 5 * 1024 ** 3),
               ("gemma-4-E4B-it-Q2_K.gguf", 2 * 1024 ** 3)])
    first = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    second = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert first == second == (
        "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q2_K.gguf")


def test_load_also_resolves_the_budget_aware_pick_without_raising(worker, monkeypatch):
    """`load`'s own resolution must not regress independently of
    `download`'s — both go through `_resolve_model_id`, and a curated id
    whose budget-aware resolve picks an alternative must still pass `load`'s
    curation gate (it only checks that resolution SUCCEEDS; `gguf_path`
    itself is supplied separately by the caller, the same shape every other
    `load` test here already uses)."""
    _fake_llama_cpp(monkeypatch)
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", str(3 * 1024 ** 3))
    worker.worker_base._repo_files = lambda model_id, **kw: (
        "sha", [("gemma-4-E4B-it-Q4_K_M.gguf", 5 * 1024 ** 3),
               ("gemma-4-E4B-it-Q2_K.gguf", 2 * 1024 ** 3)])
    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", "/blobs/whatever.gguf")
    assert worker._loaded["llm"].kwargs["model_path"] == "/blobs/whatever.gguf"


def test_a_zero_or_negative_budget_reads_as_unknown(worker, monkeypatch):
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", "0")
    worker.worker_base._repo_files = lambda model_id, **kw: (
        "sha", [("gemma-4-E4B-it-Q4_K_M.gguf", 5 * 1024 ** 3)])
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


def test_a_corrupt_budget_env_var_reads_as_unknown(worker, monkeypatch):
    monkeypatch.setenv("FUSED_AI_MEMORY_BUDGET_BYTES", "not-a-number")
    worker.worker_base._repo_files = lambda model_id, **kw: (
        "sha", [("gemma-4-E4B-it-Q4_K_M.gguf", 5 * 1024 ** 3)])
    path = worker.download("gemma-4-E4B-it-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


def test_a_repo_id_resolves_to_its_already_cached_recipe(worker):
    """The regression code review found: `unsloth/gemma-4-E4B-it-GGUF` downloaded
    through the filename id `gemma-4-E4B-it-Q4_K_M.gguf` is what the AI Models
    page then offers a Load button for UNDER ITS REPO ID (the local cache is
    keyed by repo, not by this table's filenames) — and before this fix that
    second Load died with the "not curated" refusal for the exact model a
    user just fetched through this engine."""
    worker.worker_base.cached_files.add(
        ("unsloth/gemma-4-E4B-it-GGUF", "gemma-4-E4B-it-Q4_K_M.gguf"))
    path = worker.download("unsloth/gemma-4-E4B-it-GGUF")
    assert path == "/blobs/unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-Q4_K_M.gguf"


def test_a_repo_id_with_exactly_one_recipe_resolves_even_cold(worker):
    """No ambiguity to refuse when a repo curates only one quantization —
    `unsloth/Qwen3.8-27B-GGUF` has exactly one entry in the catalog."""
    path = worker.download("unsloth/Qwen3.8-27B-GGUF")
    assert path == ("/blobs/unsloth/Qwen3.8-27B-GGUF/"
                    "Qwen3.8-27B-UD-Q3_K_XL.gguf")


def test_a_repo_id_with_two_recipes_and_nothing_cached_is_refused_by_name(
        worker, monkeypatch):
    """Two curated quantizations of ONE repo, with neither on disk yet: the
    bare repo id is genuinely ambiguous, and guessing would risk a
    multi-gigabyte download of the wrong one rather than a `FileNotFoundError`.

    The recipe table is PATCHED rather than read, and that is the point. The
    shipped shortlist happens to curate exactly one quantization per repo
    today — it curated two of the Qwen 4B until the 2026-08-21 refresh — so a
    test that reached into the real table for its ambiguous pair would go
    quietly vacuous the moment the curation stopped supplying one, which is
    precisely what happened. This branch of `_resolve_model_id` must keep
    working for the day a repo gains a second entry again.
    """
    monkeypatch.setattr(worker, "_GGUF_RECIPES", {
        "Model-Q4_K_M.gguf": {"repo": "org/Model-GGUF",
                              "file": "Model-Q4_K_M.gguf"},
        "Model-Q8_0.gguf": {"repo": "org/Model-GGUF",
                            "file": "Model-Q8_0.gguf"},
    })
    with pytest.raises(RuntimeError, match="ambiguous"):
        worker.download("org/Model-GGUF")


def test_load_also_resolves_a_repo_id_the_same_way(worker, monkeypatch):
    """`load`'s own curation check must not regress independently of
    `download`'s — both call `_resolve_model_id`."""
    _fake_llama_cpp(monkeypatch)
    worker.worker_base.cached_files.add(
        ("unsloth/Qwen3.8-27B-GGUF", "Qwen3.8-27B-UD-Q3_K_XL.gguf"))

    worker.load("unsloth/Qwen3.8-27B-GGUF", "/blobs/whatever.gguf")
    assert worker._loaded["llm"].kwargs["model_path"] == "/blobs/whatever.gguf"


def test_every_curated_recipe_is_also_in_the_catalog(worker):
    """The two tables answer different questions — this one "how do I fetch
    it", the catalog "should I suggest it" — and a model this runner can load
    that the catalog never mentions (or the reverse) is exactly the drift
    `formats.COMPONENT_REPOS`'s docstring warns about one level up.

    Uses the `worker` fixture rather than a second hand-rolled import: an
    EARLIER version of this test loaded the module a second time with
    `sys.modules.setdefault("worker_base", ...)`, which is not undone by
    `setdefault` and is not `monkeypatch`-tracked either — an empty stub
    module survived for the rest of the xdist worker's session and was ready
    to be handed to the next test's bare `import worker_base`, an
    order-dependent flake nothing here was exercising on purpose. The fixture
    already does this safely (`monkeypatch.setitem`).
    """
    from fused_render.ai import catalog

    recipe_ids = set(worker._GGUF_RECIPES)
    catalog_ids = {entry["id"] for entry in catalog.SUGGESTIONS["llamacpp-text"]}
    assert recipe_ids == catalog_ids


# -- the chat template, rendered from the GGUF's own metadata ---------------


class _FakeLlamaModel:
    """Mirrors `llama_cpp._internals.LlamaModel`, reachable as `Llama._model`
    — the surface this runner actually calls. `Llama` ITSELF has no
    `token_get_text`/`add_bos_token`/`add_eos_token` (checked against the
    installed 0.3.29): a fake that put those methods directly on `_FakeLlama`
    would be MORE capable than the real object, which is exactly what let a
    call to the wrong one pass every test while raising `AttributeError` in
    production (code review finding 1)."""

    def __init__(self, texts=None, add_bos=True, add_eos=False):
        self._texts = {1: "<bos>", 2: "<eos>"} if texts is None else texts
        self._add_bos = add_bos
        self._add_eos = add_eos

    def token_get_text(self, token_id):
        return self._texts.get(token_id, "")

    def add_bos_token(self):
        return self._add_bos

    def add_eos_token(self):
        return self._add_eos


class _FakeLlama:
    """Enough of `llama_cpp.Llama` to answer what this runner asks of it."""

    def __init__(self, chunks=(), metadata=None, tokens=5,
                add_bos_token=True, add_eos_token=False):
        self._chunks = list(chunks)
        self.metadata = metadata or {}
        self._tokens = tokens
        self._model = _FakeLlamaModel(add_bos=add_bos_token, add_eos=add_eos_token)
        self.tokenize_calls = []

    def token_bos(self):
        return 1

    def token_eos(self):
        return 2

    def tokenize(self, data, add_bos=True):
        self.tokenize_calls.append(add_bos)
        return list(range(self._tokens))

    def create_completion(self, prompt, **kwargs):
        self.last_call = {"prompt": prompt, **kwargs}
        for chunk in self._chunks:
            yield {"choices": [{"text": chunk}]}


_TEMPLATE = (
    "{% for m in messages %}{{ m.role }}: {{ m.content }}\n{% endfor %}"
    "{% if add_generation_prompt %}assistant:{% endif %}"
)


def test_the_chat_template_comes_from_the_ggufs_own_metadata(worker):
    """No file on disk to read — see the module docstring on why these
    curated repos ship no external tokenizer_config.json at all."""
    llm = _FakeLlama(metadata={"tokenizer.chat_template": _TEMPLATE})
    assert worker._chat_template(llm) == _TEMPLATE
    assert worker._chat_template(_FakeLlama(metadata={})) is None


def test_a_chat_prompt_is_rendered_through_the_templates_own_shape(worker):
    llm = _FakeLlama(metadata={"tokenizer.chat_template": _TEMPLATE})
    rendered = worker._prompt_text(
        llm, [{"role": "user", "content": "hi"}], "")
    assert rendered == "user: hi\nassistant:"


def test_reasoning_is_off_by_default(worker):
    """Three of this runner's curated models are Qwen3.5 GGUFs, whose upstream
    template defaults reasoning ON (AI-11d) — passed unconditionally here
    because, unlike transformers' `apply_chat_template`, Jinja never raises on
    a context variable a template does not reference."""
    template = "{{ enable_thinking }}"
    llm = _FakeLlama(metadata={"tokenizer.chat_template": template})
    assert worker._prompt_text(llm, [], "") == "False"


def test_a_raw_prompt_skips_the_template_entirely(worker):
    llm = _FakeLlama(metadata={"tokenizer.chat_template": _TEMPLATE})
    assert worker._prompt_text(llm, [], "once upon a time") == "once upon a time"


def test_a_model_with_no_chat_template_falls_back_to_a_plain_join(worker):
    llm = _FakeLlama(metadata={})
    rendered = worker._prompt_text(
        llm, [{"role": "user", "content": "hi"},
              {"role": "assistant", "content": "hello"}], "")
    assert rendered == "hi\n\nhello"


def test_a_broken_template_falls_back_rather_than_failing_the_reply(worker):
    """A bad template must cost the RENDER, never the generation.

    `jinja2.exceptions.UndefinedError` (raised by `{{ undefined.attr }}`) IS a
    `TemplateError` subclass — confirmed against the installed jinja2 — so
    `_prompt_text`'s narrowed `except jinja2.exceptions.TemplateError` still
    catches this real template defect and only this real template defect;
    see the "only a template's own failure is caught" tests below for what
    it must NOT catch.
    """
    llm = _FakeLlama(metadata={"tokenizer.chat_template": "{{ undefined.attr }}"})
    rendered = worker._prompt_text(
        llm, [{"role": "user", "content": "hi"}], "")
    assert rendered == "hi"


def test_a_programming_bug_in_render_is_not_swallowed_as_a_template_failure(worker):
    """The exact regression code review finding 1 named: `_prompt_text` used
    to catch bare `Exception`, so a call to a method that does not exist on
    the real `Llama` (see `_FakeLlamaModel`'s docstring) silently produced
    the plain-join fallback on EVERY chat request, with nothing on screen
    saying why. A `TypeError` from this module's own code must propagate."""
    llm = _FakeLlama(metadata={"tokenizer.chat_template": _TEMPLATE})

    def boom(*_args, **_kwargs):
        raise TypeError("not a template problem")

    import jinja2

    real_environment = jinja2.Environment
    try:
        jinja2.Environment = boom
        with pytest.raises(TypeError, match="not a template problem"):
            worker._prompt_text(llm, [{"role": "user", "content": "hi"}], "")
    finally:
        jinja2.Environment = real_environment


def test_bos_and_eos_tokens_reach_the_template_via_the_internal_model(worker):
    """The exact call path code review finding 1 named: `Llama` itself has no
    `token_get_text` (checked against the installed 0.3.29), so this has to
    reach `llm._model.token_get_text` — and the fake mirrors that shape
    rather than shortcutting it, so a regression to the wrong call raises
    `AttributeError` here instead of silently falling back to the plain join.

    `add_bos_token=False`/`add_eos_token=False`: the template has to render
    the literal text itself for a model that does not auto-add either, which
    is the one case `_bos_token_for_template`/`_eos_token_for_template` hand
    back a non-empty string.
    """
    llm = _FakeLlama(
        metadata={"tokenizer.chat_template": "{{ bos_token }}mid{{ eos_token }}"},
        add_bos_token=False, add_eos_token=False)
    assert worker._prompt_text(llm, [], "") == "<bos>mid<eos>"


def test_bos_token_is_omitted_when_create_completion_will_add_it_itself(worker):
    """`create_completion` decides on its own, every call, whether to
    prepend the real BOS token (`Llama._model.add_bos_token()`) — a template
    that ALSO renders the literal `bos_token` string would then put two BOS
    tokens in the sequence. `add_bos_token=True` (the common case) must
    render an EMPTY bos_token so the template's own text carries none."""
    llm = _FakeLlama(
        metadata={"tokenizer.chat_template": "[{{ bos_token }}]"},
        add_bos_token=True)
    assert worker._prompt_text(llm, [], "") == "[]"


def test_a_model_with_no_content_or_multimodal_content_does_not_crash_the_fallback(worker):
    """The fallback join is the HOT path once finding 1 is fixed (a model
    with no chat template still needs it), and `content` is not always a
    string on the wire: `None` (a tool-call-only turn) and a multimodal parts
    list both reached `"\n\n".join(...)` before this and raised `TypeError`
    on the one path that had no chat template to fall back FROM."""
    llm = _FakeLlama(metadata={})
    rendered = worker._prompt_text(llm, [
        {"role": "user", "content": None},
        {"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]},
    ], "")
    assert rendered == "\n\nhi"


# -- prompt token counting (SPEC AI-3) ---------------------------------------


def test_the_tokenized_prompt_length_is_the_input_token_count(worker):
    llm = _FakeLlama(tokens=7)
    assert worker._prompt_tokens(llm, "hello") == 7


def test_a_tokenizer_that_raises_costs_the_metric_not_the_generation(worker):
    class Boom(_FakeLlama):
        def tokenize(self, data, add_bos=True):
            raise RuntimeError("no")

    assert worker._prompt_tokens(Boom(), "hello") is None


def test_the_token_count_follows_the_models_own_bos_policy(worker):
    """A fixed `add_bos=True` counted a token `create_completion` might not
    actually add — metric drift on every model whose GGUF turns its own
    auto-BOS off, which is the same flag `_bos_token_for_template` reads for
    the same reason (code review finding 8)."""
    auto_bos = _FakeLlama(add_bos_token=True)
    worker._prompt_tokens(auto_bos, "hello")
    assert auto_bos.tokenize_calls == [True]

    manual_bos = _FakeLlama(add_bos_token=False)
    worker._prompt_tokens(manual_bos, "hello")
    assert manual_bos.tokenize_calls == [False]


# -- streaming and cancellation ----------------------------------------------


def test_generate_streams_chunks_then_a_done_frame(worker):
    llm = _FakeLlama(chunks=["Hel", "lo"], tokens=3)
    worker._loaded["llm"] = llm
    frames = []
    worker.generate({"prompt": "hi", "max_tokens": 8}, frames.append)
    chunks = [f for f in frames if f["type"] == "chunk"]
    done = frames[-1]
    assert [c["text"] for c in chunks] == ["Hel", "lo"]
    assert done["type"] == "done" and done["ok"] is True
    assert done["tokens"] == 2
    assert done["input_tokens"] == 3
    assert "seconds" in done


def test_generate_passes_sampling_params_straight_through(worker):
    llm = _FakeLlama(chunks=["x"])
    worker._loaded["llm"] = llm
    worker.generate(
        {"prompt": "hi", "max_tokens": 40, "temperature": 0.2, "top_p": 0.4},
        lambda payload: None)
    assert llm.last_call["max_tokens"] == 40
    assert llm.last_call["temperature"] == 0.2
    assert llm.last_call["top_p"] == 0.4


def test_no_model_loaded_answers_a_clean_failure(worker):
    frames = []
    worker.generate({"prompt": "hi"}, frames.append)
    assert frames == [{"type": "done", "ok": False, "error": "no model is loaded"}]


# -- images: this runner does not read them at all ---------------------------
# `generate` reads `messages`/`prompt`/`max_tokens`/`temperature`/`top_p` and
# nothing else. Silently dropping `images` on the floor would answer a
# picture the model never saw with a confident reply about nothing — the
# worst failure mode this app has, and exactly what an explicit refusal
# exists to stop.


def test_images_are_refused_rather_than_silently_dropped(worker):
    """Not a claim about llama.cpp's own limits (it has `libmtmd`, and
    llama-cpp-python exposes vision through `create_chat_completion`'s
    chat-handler table) — this runner deliberately streams through
    `create_completion` instead (module docstring), and a vision model would
    additionally need a second `mmproj` GGUF nothing here downloads. So the
    refusal is about THIS runner's wiring, and it must fire before a single
    token streams."""
    llm = _FakeLlama(chunks=["should never run"])
    worker._loaded["llm"] = llm
    frames = []
    worker.generate(
        {"prompt": "what is this?", "images": ["/Users/x/photo.png"]}, frames.append)
    assert frames[-1]["ok"] is False
    assert "llamacpp" in frames[-1]["error"].lower()
    assert not [f for f in frames if f["type"] == "chunk"], (
        "no tokens may stream once images are refused")


def test_an_empty_images_list_is_not_a_refusal(worker):
    """The shape the server sends when nothing is attached — omitted or
    empty, never a bare `[]`, but this runner's OWN contract should not
    depend on the server never sending one."""
    llm = _FakeLlama(chunks=["ok"])
    worker._loaded["llm"] = llm
    frames = []
    worker.generate({"prompt": "hi", "images": []}, frames.append)
    assert frames[-1]["ok"] is True


def test_cancellation_needs_no_thread_to_join(worker):
    """`create_completion`'s own generator IS the token loop here. transformers'
    `model.generate` owns its own loop and needs a producer thread to hand
    tokens back through `TextIteratorStreamer`; here, breaking out of this loop
    on `CANCEL` is the whole of stopping it."""
    llm = _FakeLlama(chunks=["a", "b", "c", "d"], tokens=2)
    worker._loaded["llm"] = llm

    seen = []

    def collector(payload):
        seen.append(payload)
        if payload.get("type") == "chunk" and len(seen) == 2:
            worker.worker_base.CANCEL.set()

    try:
        worker.generate({"prompt": "hi"}, collector)
    finally:
        worker.worker_base.CANCEL.clear()

    done = seen[-1]
    assert done["type"] == "done" and done["cancelled"] is True
    assert done["input_tokens"] == 2
    # Stopped after the second chunk rather than running the fake's four to
    # completion.
    assert len([f for f in seen if f["type"] == "chunk"]) == 2


def test_a_disconnected_write_propagates_with_nothing_left_running(worker):
    """No producer thread exists here, so a `write` that raises on a client
    disconnect simply ends the generator — there is nothing to join."""
    llm = _FakeLlama(chunks=["a", "b", "c"])
    worker._loaded["llm"] = llm

    def disconnected(payload):
        if payload["type"] == "chunk":
            raise BrokenPipeError("client disconnected")

    with pytest.raises(BrokenPipeError, match="client disconnected"):
        worker.generate({"prompt": "hi"}, disconnected)


# -- device and memory --------------------------------------------------------


def _fake_llama_cpp(monkeypatch, *, gpu_offload=False, fail_for=(),
                    expert_offload=True, kv_quant_supported=False,
                    reject_kv_quant=False, reject_kv_quant_on_gpu_only=False):
    """A fake `llama_cpp` module good enough for the offload-decision tests.

    `gpu_offload` stands in for `llama_supports_gpu_offload()` — the real
    llama.cpp API `load()` now asks before touching `n_gpu_layers`. `fail_for`
    is the set of `n_gpu_layers` values this fake's `Llama` refuses to
    construct with (raising, the same shape a real too-large GPU buffer
    allocation raises) — everything else succeeds.

    The nested `llama_cpp.llama_cpp` submodule is not padding: `_experts_on_cpu`
    patches `llama_model_default_params` THERE rather than on the package,
    because that is where `llama.py` reads it from, and a fake that only had
    the package attribute would let a regression to the wrong target pass.
    `expert_offload=False` removes the ggml symbol instead, standing in for a
    binding that has moved out from under the override.

    `kv_quant_supported` stands in for a binding new enough to expose
    `GGML_TYPE_Q8_0` — `_kv_cache_kwargs` reads this off the module with
    `getattr`, so its absence (the default, matching every OTHER test in
    this file) is what makes `load()` skip the quantized-cache pass
    entirely rather than a special case those tests would otherwise need.
    `reject_kv_quant` makes `Llama()` raise whenever it is asked for one —
    standing in for a build whose backend cannot honor `type_k`/`type_v`/
    `flash_attn` at all, at ANY `n_gpu_layers` — so `load()`'s fp16 fallback
    can be exercised regardless of what VRAM would otherwise allow.
    `reject_kv_quant_on_gpu_only` makes the same rejection happen only when
    `n_gpu_layers != 0` — standing in for a Vulkan-style GPU backend that
    rejects `flash_attn` plus a quantized V cache while its CPU rung (which
    never touches that backend) accepts the same kwargs without complaint.
    """
    fake_llama_cpp = types.ModuleType("llama_cpp")
    fake_llama_cpp.llama_supports_gpu_offload = lambda: gpu_offload
    if kv_quant_supported:
        fake_llama_cpp.GGML_TYPE_Q8_0 = 8
    calls = []
    kv_calls = []

    class Params:
        def __init__(self):
            self.tensor_buft_overrides = None

    inner = types.ModuleType("llama_cpp.llama_cpp")
    inner.llama_model_default_params = Params

    class Lib:
        """Stands in for the ctypes handle the binding already has open."""
        if expert_offload:
            @staticmethod
            def ggml_backend_cpu_buffer_type():
                return 0xB0F7

    inner._lib = Lib()
    fake_llama_cpp.llama_cpp = inner

    class Llama:
        def __init__(self, **kwargs):
            calls.append(kwargs["n_gpu_layers"])
            has_kv_quant = "type_k" in kwargs
            kv_calls.append(has_kv_quant)
            if reject_kv_quant and has_kv_quant:
                raise ValueError("Failed to load model from file: test (kv)")
            if reject_kv_quant_on_gpu_only and has_kv_quant \
                    and kwargs["n_gpu_layers"] != 0:
                raise ValueError("Failed to load model from file: test (kv, gpu)")
            if kwargs["n_gpu_layers"] in fail_for:
                raise ValueError("Failed to load model from file: test")
            self.kwargs = kwargs
            self.metadata = {}
            # Ask the factory the way the real `Llama.__init__` does — which
            # is the only thing `_experts_on_cpu`'s patch can act on — and keep
            # what it filled in, so a test can tell "the override was
            # installed" from "the rung merely ran".
            self.overrides = inner.llama_model_default_params().tensor_buft_overrides

    fake_llama_cpp.Llama = Llama
    fake_llama_cpp.calls = calls
    fake_llama_cpp.kv_calls = kv_calls
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_llama_cpp)
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_cpp", inner)
    return fake_llama_cpp


def _make_gguf(pairs):
    """A minimal but REAL GGUF header — `formats` parses these bytes for real.

    Only the two value types these tests need: 8 (string) and 4 (uint32).
    """
    import struct

    buf = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + \
        struct.pack("<Q", len(pairs))
    for key, value_type, value in pairs:
        buf += struct.pack("<Q", len(key.encode())) + key.encode()
        buf += struct.pack("<I", value_type)
        if value_type == 8:
            buf += struct.pack("<Q", len(value.encode())) + value.encode()
        else:
            buf += struct.pack("<I", value)
    return buf


def test_load_reports_cpu_when_the_build_has_no_gpu_backend_at_all(worker, monkeypatch):
    """`llama_supports_gpu_offload()` False (a CPU-only build, or a Vulkan
    build with no usable driver) means exactly one attempt at `n_gpu_layers=0`
    — no retry loop to enter, since there is no smaller candidate than CPU."""
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=False)

    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", "/blobs/model.gguf")
    assert worker.worker_base.recorded == {"device": "cpu"}
    assert worker._loaded["llm"].kwargs["model_path"] == "/blobs/model.gguf"
    assert fake.calls == [0]


def test_load_offloads_to_the_gpu_when_the_build_supports_it(worker, monkeypatch):
    """The blocking defect this test guards against: `n_gpu_layers` used to
    never be passed at all, which `llama-cpp-python` defaults to `0` — a
    182MB Vulkan wheel that only ever ran on the CPU. A fake nonexistent path
    means `formats.gguf_block_count` cannot read a header, so the schedule
    collapses to `(-1, False)` then `(0, False)` and the first attempt (`-1`,
    llama.cpp's own "all layers" sentinel) succeeds outright."""
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True)

    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", "/blobs/model.gguf")
    assert fake.calls == [-1]
    assert worker._loaded["llm"].kwargs["n_gpu_layers"] == -1
    assert worker.worker_base.recorded == {"device": "gpu"}


def test_load_backs_off_to_cpu_when_full_offload_does_not_fit(worker, monkeypatch):
    """The VRAM-sizing probe: a too-large GPU request raises (the same shape
    a real allocation failure does — caught, not a process abort, per the
    module docstring), and `load()` must retry smaller rather than let the
    Load button fail outright. With no header to fraction against, the only
    smaller candidate is `0`, so this also pins the "slow but working beats
    an OOM'd Load" policy in its plainest form."""
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, fail_for={-1})

    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", "/blobs/model.gguf")
    assert fake.calls == [-1, 0]
    assert worker.worker_base.recorded == {"device": "cpu"}


def test_load_reports_partial_offload_when_fewer_than_all_layers_fit(
        worker, monkeypatch, tmp_path):
    """A real GGUF header this time (32 layers, `qwen35.block_count`), so the
    schedule has an intermediate step between "all" and "0" — and landing on
    it must say so rather than claiming a full GPU load."""
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(_make_gguf([
        ("general.architecture", 8, "qwen35"),
        ("qwen35.block_count", 4, 32),
    ]))

    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, fail_for={32})

    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", str(gguf_path))
    # 32 (all) fails, the next step in the schedule (2/3 of 32 = 21) succeeds.
    assert fake.calls == [32, 21]
    assert worker.worker_base.recorded == {"device": "gpu (partial)"}


def test_load_does_not_swallow_a_failure_no_smaller_offload_can_fix(worker, monkeypatch):
    """Every candidate failing — including `0` — is not a sizing problem, it
    is a real one (a corrupt download, an unreadable file), and must reach
    the caller rather than being retried forever or silently eaten."""
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, fail_for={-1, 0})

    with pytest.raises(ValueError, match="Failed to load model"):
        worker.load("gemma-4-E4B-it-Q4_K_M.gguf", "/blobs/model.gguf")
    assert fake.calls == [-1, 0]


def test_the_offload_schedule_adds_an_expert_rung_only_for_a_mixture_of_experts_model(
        worker):
    """Position is the whole point, so it is asserted rather than described.

    The expert rung goes directly ABOVE pure CPU and below every dense step:
    full offload is roughly three times faster than any split and must be
    tried first, and the fractional steps beat it whenever they fit — it earns
    its place only against the bottom rung, which it beats on VRAM and
    throughput at once. See `_offload_schedule`'s docstring for the numbers.
    """
    dense = worker._offload_schedule(24, False)
    experts = worker._offload_schedule(24, True)

    assert dense == ((24, False), (16, False), (8, False), (0, False))
    assert experts == ((24, False), (16, False), (8, False), (-1, True), (0, False))
    # ...directly above CPU, and CPU still last whatever else changes.
    assert experts[-2:] == ((-1, True), (0, False))
    assert all(not park for _count, park in dense)


def test_the_expert_rung_is_offered_even_when_the_header_gave_no_layer_count(worker):
    """The rung needs no layer count — it moves TENSORS, not layers — so an
    unreadable header costs the fractional steps but not this one."""
    assert worker._offload_schedule(None, True) == (
        (-1, False), (-1, True), (0, False))
    assert worker._offload_schedule(None, False) == ((-1, False), (0, False))


def test_load_parks_the_experts_on_the_cpu_when_no_dense_split_fits(
        worker, monkeypatch, tmp_path):
    """The rung's reason for existing: rather than fall from the smallest
    dense split straight to pure CPU, a MoE model keeps every layer on the GPU
    and puts only its expert tensors in system RAM."""
    # The id is the real curated mixture-of-experts row, but it is the header
    # written below — not the id — that makes this test a MoE one. The id only
    # has to be CURATED: an uncurated one resolves by asking the Hub, and a
    # test must never reach the network. (It did, once, when `main` retired the
    # id this originally used.)
    gguf_path = tmp_path / "moe.gguf"
    gguf_path.write_bytes(_make_gguf([
        ("general.architecture", 8, "lfm2moe"),
        ("lfm2moe.block_count", 4, 24),
        ("lfm2moe.expert_count", 4, 32),
    ]))
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, fail_for={24, 16, 8})

    worker.load("LFM2.5-8B-A1B-Q4_K_M.gguf", str(gguf_path))

    assert fake.calls == [24, 16, 8, -1]
    assert worker.worker_base.recorded == {"device": "gpu (experts on cpu)"}
    # The override was actually installed, not merely attempted — this is what
    # separates the rung working from the rung silently doing nothing.
    assert worker._loaded["llm"].overrides is not None


def test_load_restores_the_params_factory_it_patched(worker, monkeypatch, tmp_path):
    """`_experts_on_cpu` patches a module global, so a leak would silently
    give expert placement to the NEXT model this process loads — including a
    dense one, where the pattern matches nothing but the patch is still wrong."""
    gguf_path = tmp_path / "moe.gguf"
    gguf_path.write_bytes(_make_gguf([
        ("general.architecture", 8, "lfm2moe"),
        ("lfm2moe.block_count", 4, 24),
        ("lfm2moe.expert_count", 4, 32),
    ]))
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, fail_for={24, 16, 8})
    before = fake.llama_cpp.llama_model_default_params

    worker.load("LFM2.5-8B-A1B-Q4_K_M.gguf", str(gguf_path))

    assert fake.llama_cpp.llama_model_default_params is before


def test_load_never_parks_experts_for_a_dense_model(worker, monkeypatch, tmp_path):
    """A dense GGUF has no `expert_count` key at all, so the rung must not
    appear — there is nothing to park, and a pattern matching no tensor would
    buy an extra failed load attempt for nothing."""
    gguf_path = tmp_path / "dense.gguf"
    gguf_path.write_bytes(_make_gguf([
        ("general.architecture", 8, "qwen35"),
        ("qwen35.block_count", 4, 32),
    ]))
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, fail_for={32, 21})

    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", str(gguf_path))

    assert fake.calls == [32, 21, 10]
    assert worker.worker_base.recorded == {"device": "gpu (partial)"}
    assert worker._loaded["llm"].overrides is None


def test_expert_offload_the_binding_cannot_supply_still_loads_the_model(
        worker, monkeypatch, tmp_path):
    """Losing the ggml symbol costs throughput, never correctness — so the
    load proceeds without the override. The device must then NOT claim the
    experts are on the CPU, since they are not."""
    gguf_path = tmp_path / "moe.gguf"
    gguf_path.write_bytes(_make_gguf([
        ("general.architecture", 8, "lfm2moe"),
        ("lfm2moe.block_count", 4, 24),
        ("lfm2moe.expert_count", 4, 32),
    ]))
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, fail_for={24, 16, 8},
                           expert_offload=False)

    worker.load("LFM2.5-8B-A1B-Q4_K_M.gguf", str(gguf_path))

    assert fake.calls == [24, 16, 8, -1]
    assert worker._loaded["llm"].overrides is None
    assert worker.worker_base.recorded == {"device": "gpu"}


def test_load_requests_a_q8_0_kv_cache_with_flash_attention_when_the_binding_supports_it(
        worker, monkeypatch):
    """The whole point: `GGML_TYPE_Q8_0` on the binding must actually reach
    `Llama()` as `type_k`/`type_v`, paired with `flash_attn=True` — the
    combination llama.cpp itself requires for a quantized V cache (see
    `_kv_cache_kwargs`'s docstring for the upstream check this pins to)."""
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, kv_quant_supported=True)

    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", "/blobs/model.gguf")

    kwargs = worker._loaded["llm"].kwargs
    assert kwargs["type_k"] == 8
    assert kwargs["type_v"] == 8
    assert kwargs["flash_attn"] is True
    # One rung succeeded on the first pass — no fallback pass was needed.
    assert fake.calls == [-1]
    assert fake.kv_calls == [True]


def test_load_never_asks_for_a_quantized_kv_cache_when_the_binding_lacks_the_enum(
        worker, monkeypatch):
    """The default fake (every OTHER test in this file) has no
    `GGML_TYPE_Q8_0` attribute — `_kv_cache_kwargs` must answer `{}` rather
    than guess a raw `8`, so a binding that never advertised the enum is
    asked for nothing beyond what it already supported."""
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True)

    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", "/blobs/model.gguf")

    kwargs = worker._loaded["llm"].kwargs
    assert "type_k" not in kwargs
    assert "type_v" not in kwargs
    assert "flash_attn" not in kwargs
    assert fake.kv_calls == [False]


def test_load_falls_back_to_the_default_fp16_cache_when_quantization_fails_every_rung(
        worker, monkeypatch, tmp_path):
    """A build whose backend cannot honor the quantized cache at ALL must not
    be mistaken for one that merely needs fewer GPU layers — `load()` retries
    the SAME rung without the KV kwargs before moving to a smaller one,
    rather than exhausting the whole ladder over a capability gap a plain
    fp16 cache would not have hit."""
    gguf_path = tmp_path / "model.gguf"
    gguf_path.write_bytes(_make_gguf([
        ("general.architecture", 8, "qwen35"),
        ("qwen35.block_count", 4, 32),
    ]))
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, kv_quant_supported=True,
                           reject_kv_quant=True)

    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", str(gguf_path))

    # The very first rung (32, all layers) is tried quantized, fails, and is
    # immediately retried at the SAME rung with no KV kwargs, which succeeds
    # — no smaller rung is ever tried, because the fp16 fallback happens
    # per-rung rather than as a second pass over the whole ladder.
    assert fake.calls == [32, 32]
    assert fake.kv_calls == [True, False]
    kwargs = worker._loaded["llm"].kwargs
    assert "type_k" not in kwargs
    assert worker.worker_base.recorded == {"device": "gpu"}


def test_load_still_raises_when_no_rung_fits_in_either_kv_cache_variant(
        worker, monkeypatch):
    """Exhausting both KV variants at every rung down to plain CPU is a real
    failure, not a sizing problem, and must reach the caller rather than
    being swallowed the way a too-large GPU request is."""
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, kv_quant_supported=True,
                           fail_for={-1, 0})

    with pytest.raises(ValueError, match="Failed to load model"):
        worker.load("gemma-4-E4B-it-Q4_K_M.gguf", "/blobs/model.gguf")
    # Rung -1: quantized fails, fp16 fallback also fails (still `fail_for`,
    # the KV kwargs were never why these failed). Rung 0: same story.
    assert fake.calls == [-1, -1, 0, 0]
    assert fake.kv_calls == [True, False, True, False]


def test_load_prefers_a_gpu_rung_at_fp16_over_falling_all_the_way_to_cpu_at_q8_0(
        worker, monkeypatch):
    """A backend (the concrete worry is Vulkan) that rejects `flash_attn`
    plus a quantized V cache on every GPU rung, but whose CPU rung —
    `_offload_schedule` always ends on `n_gpu_layers=0` — accepts a quantized
    cache just fine, because CPU never touches that backend at all.

    A per-WHOLE-LADDER fallback would walk every GPU rung with the (rejected)
    quantized cache, land on CPU, succeed there, and return — silently
    parking a GPU-capable machine on the CPU. Retrying the fp16 cache at EACH
    rung before giving up on it is what makes the full-offload rung succeed
    at fp16 so the worker never reaches the CPU rung."""
    fake = _fake_llama_cpp(monkeypatch, gpu_offload=True, kv_quant_supported=True,
                           reject_kv_quant_on_gpu_only=True)

    worker.load("gemma-4-E4B-it-Q4_K_M.gguf", "/blobs/model.gguf")

    # Only the top rung (-1, all layers) is ever tried: quantized fails
    # there, the fp16 retry at that SAME rung succeeds — the CPU rung (`0`)
    # is never reached at all.
    assert fake.calls == [-1, -1]
    assert fake.kv_calls == [True, False]
    kwargs = worker._loaded["llm"].kwargs
    assert "type_k" not in kwargs
    assert kwargs["n_gpu_layers"] == -1
    assert worker.worker_base.recorded == {"device": "gpu"}


def test_memory_defers_entirely_to_rss(worker):
    """mmap'd weights are already counted in RSS (`use_mmap=True` is the
    default), so there is no second accounting system to add on top — unlike
    torch's CUDA/MPS allocators, which `worker_base` takes the larger of."""
    assert worker.memory() is None


def test_memory_is_actually_wired_into_serve(worker):
    """`memory()` returning None is only meaningful if `worker_base.serve`
    is actually told about it — `torch_image.main` passes `memory=memory`,
    and this runner's own `main()` used to omit the
    kwarg entirely, which made the function dead code no future real
    measurement could ever reach (code review finding 7)."""
    worker.main()
    assert worker.worker_base.serve_calls[-1]["memory"] is worker.memory
