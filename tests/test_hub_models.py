"""/api/ai-models/hub/* — searching the Hub for models this app can run, joined
to the local cache (SPEC §39).

The Hub itself is never called: `httpx.get` is replaced per test, because the
point under test is what this module DOES with an answer — how it joins, what it
leaves out, and how it behaves when the far side is unreachable, rate-limiting,
or sending something unexpected. A test that reached huggingface.co would be
testing huggingface.co.

The section at the bottom is the D313 constraint, and it is the one that would
be easiest to lose: every result must be something this machine could download
AND load, which is a rule about rows the Hub is free to send anyway. The tests
name the actual repos from the complaint that produced it — `all-MiniLM-L6-v2`,
`bert-base-uncased` — so a regression fails with the symptom rather than with an
abstraction of it.
"""
import json
import os
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from fused_render.ai import fit
from fused_render.ai import hw_detect
from fused_render.ai import registry
from fused_render.ai import tasks as ai_tasks
from fused_render.server import create_app
from fused_render.ai import hub_cache as ai_models_mod
from fused_render.server.routers import hub_models as hub


def _search(client, body=None):
    """One search. A guarded POST, not a GET (see the endpoint's docstring):
    search is the only read in this module that leaves the machine, carrying the
    user's Hub token, so it takes the shape its effect deserves."""
    return client.post("/api/ai-models/hub/search", json=body or {},
                       headers={"X-Fused": "1"})


@pytest.fixture(autouse=True)
def _clear_cache():
    hub._cache.clear()
    yield
    hub._cache.clear()


@pytest.fixture(autouse=True)
def _no_token(monkeypatch, tmp_path):
    """A developer's real token must not decide what these tests assert.

    The search reads whatever `huggingface_hub.get_token()` finds (D402), so the
    STORE has to be redirected and not just the environment: hf resolves
    `HF_TOKEN_PATH` once at import, so setting `HF_HOME` here does nothing to an
    hf that another test already imported — it would leave these tests reading
    the login of whoever ran them.
    """
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    home = tmp_path / "hf-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HF_HOME", str(home))
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_TOKEN_PATH", str(home / "token"))
    monkeypatch.setattr(constants, "HF_STORED_TOKENS_PATH", str(home / "stored_tokens"))


@pytest.fixture(autouse=True)
def _no_format_filter(monkeypatch):
    """Every test here starts with an active text engine that filters no format,
    NO secondary GGUF-capable runner available either, and the handful that care
    about the format filter (or D779's secondary-runner pick) override one or
    both.

    Without this the module's assertions depend on the HOST, and D416 is what
    made that bite. `hub._model_row` narrows a text-generation search by the
    active runner's `hub_filter_tags` (D412), and until D416 the runner a
    non-Apple machine resolved to was `transformers-text`, which declares none —
    so every safetensors fixture in this file survived the filter by accident of
    what the developer's laptop happened to be. With the transformers rows gone,
    Linux and Windows resolve to `llamacpp-text` and its `("gguf",)` tag, which
    dropped 30 tests here while the code under test was behaving exactly as
    designed. Pinning it makes the DEFAULT explicit and the format-filter tests
    the deliberate exception they already read as (`_gguf_runner` below), and it
    is the same reasoning `_no_token` above applies to a developer's Hub login.

    `available_runners` is pinned to `()` for the identical reason (D779, the
    fix-builder round that added it): left real, this Mac's own registry (both
    `mlx-text` AND `llamacpp-text` genuinely available here) would make the
    secondary-runner GGUF pick fire on `siblings` fixtures that were never
    written to exercise it, and pass or fail by an accident of which machine
    ran the suite — exactly the trap D779's own test file comment warns about.
    """
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner(tags=()))
    monkeypatch.setattr(hub, "available_runners", lambda capability: ())


@pytest.fixture()
def hub_cache(tmp_path, monkeypatch):
    cache = tmp_path / "hub"
    cache.mkdir()
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    return cache


@pytest.fixture()
def client():
    return TestClient(create_app(start_dir="/"))


def _reply(rows, status=200, body=None):
    """A stand-in `httpx.get` returning one canned Hub answer."""
    def fake(url, **kwargs):
        fake.calls.append((url, kwargs))
        content = json.dumps(rows).encode() if body is None else body
        return httpx.Response(status, content=content,
                              request=httpx.Request("GET", url))
    fake.calls = []
    return fake


def _hit(model_id, **extra):
    """A Hub row that SURVIVES the supported-tag filter.

    Since D313 a result is dropped unless its `pipeline_tag` maps to a
    capability some runner serves, so a bare `{"id": ...}` is no longer a row
    the page ever sees — it is the untagged case, which is now deliberately
    filtered out. Tests about the join, the sizes or the failure modes are not
    about that rule, so they build their fixtures through here and say
    `pipeline_tag` only when the tag is the thing under test.
    """
    return {"id": model_id, "pipeline_tag": "text-generation", **extra}


def _cached_repo(cache, dirname, commit="c1", size=64):
    """A cache repo with a materialised snapshot — i.e. one that is genuinely
    downloaded rather than half-pulled."""
    blob = cache / dirname / "blobs" / "b1"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x" * size)
    snapshot = cache / dirname / "snapshots" / commit
    snapshot.mkdir(parents=True)
    try:
        os.symlink(blob, snapshot / "model.safetensors")
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    refs = cache / dirname / "refs"
    refs.mkdir()
    (refs / "main").write_text(commit)
    return cache / dirname


# -- the join ---------------------------------------------------------------


def test_a_result_already_on_disk_says_so(client, hub_cache, monkeypatch):
    # The whole reason this lives in the app rather than a browser tab: the Hub
    # does not know what is on your disk, and the AI Models page does.
    _cached_repo(hub_cache, "models--org--have", size=1024)
    monkeypatch.setattr(httpx, "get", _reply([
        {"id": "org/have", "pipeline_tag": "text-generation", "downloads": 10},
        {"id": "org/have-not", "pipeline_tag": "text-generation", "downloads": 5},
    ]))
    models = _search(client).json()["models"]
    by_id = {m["id"]: m for m in models}
    assert by_id["org/have"]["local"]["state"] == "downloaded"
    # The blob plus the repo's bookkeeping (refs/main) — the number comes
    # straight from the local scan, which is the point: one measurement, two
    # tabs.
    assert by_id["org/have"]["local"]["size"] >= 1024
    assert by_id["org/have"]["local"]["dir"] == "models--org--have"
    assert by_id["org/have-not"]["local"] == {"state": "none"}


def test_a_half_pulled_repo_is_partial_not_downloaded(client, hub_cache, monkeypatch):
    # An interrupted download leaves blobs and no snapshot. Calling that
    # "downloaded" would send someone to a model that cannot load.
    blob = hub_cache / "models--org--partial" / "blobs" / "b1"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x" * 32)
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/partial")]))
    models = _search(client).json()["models"]
    assert models[0]["local"]["state"] == "partial"


def test_a_repo_with_a_revision_and_an_unfinished_fetch_is_partial_too(
    client, hub_cache, monkeypatch
):
    """"Has at least one snapshot" was the wrong line (D424).

    Our own fetcher links each file into `snapshots/<commit>/` as it lands, so a
    cancelled pull has a revision — and this tab said "downloaded" over a repo
    holding a part file and no weights. The residue of the stopped fetch is what
    answers now, and it is the AI Models listing's own reading, so the two tabs
    cannot disagree about one folder.
    """
    repo = _cached_repo(hub_cache, "models--org--partial")
    (repo / "blobs" / "weights.fusedpart").write_bytes(b"x" * 32)
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/partial")]))

    assert _search(client).json()["models"][0]["local"]["state"] == "partial"


def test_the_join_costs_what_the_results_cost_not_what_the_cache_costs(
    client, hub_cache, monkeypatch
):
    """A search must not pay for the whole cache's metadata.

    The AI Models listing answers "is this downloaded" too — and also reads
    every repo's model card, config.json and safetensors headers to say what
    each model is FOR. None of that reaches a Hub row, and a debounced keystroke
    cannot pay for it across a cache of hundreds of repos. So the join
    enumerates names once and measures only the repos that actually appear in
    the results.
    """
    for i in range(5):
        _cached_repo(hub_cache, f"models--org--m{i}")
    monkeypatch.setattr(
        ai_models_mod, "_repo_meta",
        lambda *a, **k: pytest.fail("the join read a repo's model metadata"))
    measured = []
    real_scan = ai_models_mod._scan_repo
    monkeypatch.setattr(
        hub, "_scan_repo", lambda root: (measured.append(root), real_scan(root))[1])
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/m2"), _hit("org/absent")]))

    models = _search(client).json()["models"]
    assert {m["id"]: m["local"]["state"] for m in models} == {
        "org/m2": "downloaded", "org/absent": "none"}
    # One repo was in the results and present; the other four were never touched,
    # and the absent one cost nothing at all.
    assert [os.path.basename(p) for p in measured] == ["models--org--m2"]


def test_the_local_half_is_never_served_stale(client, hub_cache, monkeypatch):
    # The Hub's answer is cached for a window; what is on this disk is not. A
    # model deleted a second ago must stop claiming to be downloaded, or the
    # card links somewhere that no longer exists.
    repo = _cached_repo(hub_cache, "models--org--m")
    fake = _reply([_hit("org/m")])
    monkeypatch.setattr(httpx, "get", fake)
    assert _search(client).json()["models"][0]["local"]["state"] == "downloaded"

    import shutil
    shutil.rmtree(repo)
    again = _search(client).json()["models"][0]
    assert again["local"] == {"state": "none"}
    assert len(fake.calls) == 1  # …and the Hub was not asked a second time


# -- what a row says --------------------------------------------------------


def test_a_task_reads_the_same_here_as_on_the_local_cards(client, hub_cache, monkeypatch):
    # One vocabulary. `image-text-to-text` is the Hub's jargon for a
    # vision-language model, and it is unreadable until the same table that
    # explains it on a downloaded model explains it here.
    monkeypatch.setattr(httpx, "get", _reply([
        {"id": "org/vlm", "pipeline_tag": "image-text-to-text"}]))
    row = _search(client).json()["models"][0]
    assert row["task"] == "image + text to text"
    assert row["taskHelp"] == ai_tasks.help_for("image-text-to-text")


def test_size_is_recovered_from_the_dtype_map(client, hub_cache, monkeypatch):
    # 8B parameters at BF16 is 16GB, and saying so before the click is the
    # number that matters on a page whose sibling feature exists because disks
    # fill up.
    #
    # The premise: hardware is pinned to 32GB/no-GPU (`_pin_hardware`), which
    # comfortably fits 16GB, so this row survives the default unfit filter on
    # ANY runner. Without this the assertion depends on the host's real RAM —
    # a CI box smaller than the dev Mac judges the row `verdict: "no"`, the
    # default filter drops it, and `models` comes back empty before either
    # assert below ever runs.
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/big",
        safetensors={"parameters": {"BF16": 8_000_000_000}, "total": 8_000_000_000},
    )]))
    row = _search(client).json()["models"][0]
    assert row["params"] == 8_000_000_000
    assert row["estimatedSize"] == 16_000_000_000


def test_a_packed_dtype_map_reports_no_size(client, hub_cache, monkeypatch):
    # `mlx-community/Lens-3.8B-4bit` and its `-8bit` sibling, as returned by
    # the Hub live: an identical dtype map for two different quantizations,
    # almost entirely U32 (a packed storage container, not a real per-weight
    # width). The old unguarded sum reported ~15.24 GiB for BOTH — larger
    # than the real BF16 original's ~7.65 GiB (4,104,225,152 params * 2
    # bytes), a ~2x-6.6x over-report. `estimatedSize` must come back absent
    # rather than lie, and specifically must not exceed the unquantized
    # original's own real size.
    _pin_hardware(monkeypatch)
    packed_safetensors = {
        "parameters": {"BF16": 27_361_664, "U32": 4_076_863_488},
        "total": 4_104_225_152,
    }
    bf16_original_bytes = 4_104_225_152 * 2  # 8,208,450,304 — the real size
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("mlx-community/Lens-3.8B-4bit", safetensors=packed_safetensors),
        _hit("mlx-community/Lens-3.8B-8bit", safetensors=packed_safetensors),
    ]))
    rows = _search(client).json()["models"]
    ids = {row["id"]: row for row in rows}
    for row in ids.values():
        assert row["estimatedSize"] is None
        # Never larger than the real unquantized original — the whole point.
        assert (row["estimatedSize"] or 0) <= bf16_original_bytes


def test_a_minority_packed_dtype_still_reports_a_size(client, hub_cache, monkeypatch):
    # A small integer buffer (e.g. a quantization scale tensor) alongside a
    # float-dominated repo must not make the whole size vanish — only a
    # packed-dtype MAJORITY (by naive bytes) refuses to compute.
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/mostly-float",
        safetensors={
            "parameters": {"BF16": 8_000_000_000, "U8": 1_000_000},
            "total": 8_001_000_000,
        },
    )]))
    row = _search(client).json()["models"][0]
    # 8e9 * 2 + 1e6 * 1 = 16,001,000,000 bytes — packed share is ~0.006%.
    assert row["estimatedSize"] == 16_001_000_000


def test_a_repo_with_no_safetensors_metadata_reports_no_size(client, hub_cache, monkeypatch):
    # A size we cannot compute is left out. A guessed one would be a number
    # someone plans a download around.
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/gguf", safetensors=None)]))
    row = _search(client).json()["models"][0]
    assert row["estimatedSize"] is None and row["params"] is None


def test_missing_fields_are_absent_not_fatal(client, hub_cache, monkeypatch):
    # The Hub returns what it returns, and an older deployment may refuse an
    # expand[] field entirely. Nothing here indexes blindly.
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/bare")]))
    row = _search(client).json()["models"][0]
    assert row["id"] == "org/bare"
    assert row["downloads"] is None and row["likes"] is None
    assert row["library"] is None and row["estimatedSize"] is None


def test_a_row_with_no_id_is_dropped(client, hub_cache, monkeypatch):
    # A row the page could not act on is a row it should not be given.
    monkeypatch.setattr(httpx, "get", _reply([{"likes": 3}, _hit("org/real")]))
    models = _search(client).json()["models"]
    assert [m["id"] for m in models] == ["org/real"]


# -- D412: the GGUF pick, only when the active runner needs one -------------
#
# `siblings` is a NEW `_EXPAND` field, so every hit in this section carries
# it — verified live that the Hub returns the full filename list in the LIST
# response itself, which is what makes this resolvable per-row with no
# second request.


def _gguf_runner(tags=("gguf",), code="stand-in"):
    """A stand-in for whatever runner `registry.for_capability` (or
    `registry.available_runners`) resolves to, carrying only the fields
    `_model_row` reads. Not a real `Runner` — this module's own resolution is
    under test, not the registry's. `code` matters once there are TWO stand-ins
    in play (D779's secondary-runner pick tells them apart by `.code`)."""
    import types as _types

    return _types.SimpleNamespace(hub_filter_tags=tags, code=code)


def test_a_gguf_repo_resolves_to_the_pickers_choice_when_llamacpp_is_active(
        client, hub_cache, monkeypatch):
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([_hit("unsloth/x-GGUF", siblings=[
        {"rfilename": "x-Q8_0.gguf"}, {"rfilename": "x-Q4_K_M.gguf"},
        {"rfilename": "README.md"},
    ])]))
    row = _search(client).json()["models"][0]
    assert row["file"] == "x-Q4_K_M.gguf"


def test_a_gguf_repo_with_nothing_loadable_is_dropped_when_llamacpp_is_active(
        client, hub_cache, monkeypatch):
    """The fifth drop reason (D412): a repo whose ONLY GGUF is auxiliary
    (here, a projector) is not actionable by the active engine, so it is
    dropped exactly like a `.tflite` repo already is for a different
    reason — never offered with a Download button that cannot resolve."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/only-a-projector", siblings=[
        {"rfilename": "m-mmproj-F16.gguf"},
    ])]))
    models = _search(client).json()["models"]
    assert models == []


def test_a_gguf_row_carries_no_file_when_no_available_runner_speaks_gguf(
        client, hub_cache, monkeypatch):
    """When the capability's active runner declares no format tag at all —
    the `mlx-text` case — AND no other runner available here does either
    (D779's `available_runners`, empty per the autouse fixture), a repo is
    not resolved or dropped by the picker, whatever its `siblings` look
    like: `file` is simply absent from the answer, the same as it always was
    before D412 and D779."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner(tags=()))
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/whatever", siblings=[
        {"rfilename": "m-mmproj-F16.gguf"},
    ])]))
    row = _search(client).json()["models"][0]
    assert row["file"] is None


def test_a_gguf_repo_resolves_via_an_available_but_not_preferred_runner(
        client, hub_cache, monkeypatch):
    """D779 — the reviewer-caught defect: `mlx-text` (no format tag) is the
    ACTIVE runner, but `llamacpp-text` is genuinely AVAILABLE here (just not
    preferred). The GGUF pick must still resolve against it — `file`, and
    everything downstream that depends on it (`quant`), must not be `None`
    just because llama.cpp is the second choice rather than the first."""
    active = _gguf_runner(tags=(), code="mlx-text")
    secondary = _gguf_runner(tags=("gguf",), code="llamacpp-text")
    monkeypatch.setattr(hub, "for_capability", lambda capability: active)
    monkeypatch.setattr(hub, "available_runners", lambda capability: (active, secondary))
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/gguf-on-a-mac", siblings=[
        {"rfilename": "x-Q8_0.gguf"}, {"rfilename": "x-Q4_K_M.gguf"},
    ])]))
    row = _search(client).json()["models"][0]
    assert row["file"] == "x-Q4_K_M.gguf"
    assert row["quant"] == "Q4_K_M"


def test_a_gguf_repo_stays_unresolved_when_only_the_active_runner_is_available(
        client, hub_cache, monkeypatch):
    """The mirror case: `available_runners` reports ONLY the active runner
    (nothing else here can load GGUF at all) — the secondary-pick loop must
    not somehow resolve against itself or fabricate a runner. `file` stays
    `None`, same as the no-secondary-runner-at-all case."""
    active = _gguf_runner(tags=(), code="mlx-text")
    monkeypatch.setattr(hub, "for_capability", lambda capability: active)
    monkeypatch.setattr(hub, "available_runners", lambda capability: (active,))
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/whatever", siblings=[
        {"rfilename": "x-Q4_K_M.gguf"},
    ])]))
    row = _search(client).json()["models"][0]
    assert row["file"] is None


def test_a_secondary_runner_finding_nothing_loadable_does_not_drop_the_row(
        client, hub_cache, monkeypatch):
    """D412's drop is the ACTIVE runner's own verdict, not a secondary
    runner's. `mlx-text` is active and declares no tag (so it never asks for
    a pick at all); `llamacpp-text` is available but finds nothing loadable
    among the siblings (an auxiliary-only GGUF, same fixture shape as the
    D412 drop test above). The row must survive with `file=None` — it is
    NOT the same as the active runner itself failing its own pick."""
    active = _gguf_runner(tags=(), code="mlx-text")
    secondary = _gguf_runner(tags=("gguf",), code="llamacpp-text")
    monkeypatch.setattr(hub, "for_capability", lambda capability: active)
    monkeypatch.setattr(hub, "available_runners", lambda capability: (active, secondary))
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/only-a-projector", siblings=[
        {"rfilename": "m-mmproj-F16.gguf"},
    ])]))
    models = _search(client).json()["models"]
    assert len(models) == 1
    assert models[0]["file"] is None


def test_a_gguf_row_carries_no_file_when_nothing_serves_the_capability_here(
        client, hub_cache, monkeypatch):
    """`for_capability` returning None (nothing registered could run here)
    must not crash the join — the row still surfaces on capability
    existence alone, per D313, with `file` simply unset."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: None)
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/whatever")]))
    row = _search(client).json()["models"][0]
    assert row["file"] is None


# -- code review finding 1: a GGUF row scores real data, not three defaults -


def test_gguf_row_uses_the_huds_own_gguf_metadata_for_params(client, hub_cache, monkeypatch):
    """The Hub's `expand[]=gguf` (new in `_EXPAND`) reports the checkpoint's
    real, quantization-invariant parameter count off the GGUF header itself
    — a genuine measured fact, safe to show in the Params column, and
    nothing here has to guess it from the repo's own name."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/gguf-only", siblings=[{"rfilename": "x-Q4_K_M.gguf"}],
        gguf={"total": 1_235_814_432, "architecture": "llama"},
    )]))
    row = _search(client).json()["models"][0]
    assert row["file"] == "x-Q4_K_M.gguf"
    assert row["params"] == 1_235_814_432
    # `estimatedSize` stays None — the client's lazy per-file lookup still
    # owns the displayed Size cell (D778); this fix only feeds the RANKING
    # axes off the real params + the resolved file's own quant token.
    assert row["estimatedSize"] is None


def test_gguf_row_with_unrecognized_quant_token_never_claims_easy(client, hub_cache, monkeypatch):
    """The regression this fix must never let back in: an unsuffixed or
    full-precision GGUF file (`formats.gguf_quant_token` returns `None` for
    both BY DESIGN — its own docstring — and `pick_gguf_file`'s pass-3
    fallback selects exactly this shape) must not feed `params` into
    `fit.verdict`/`speed.estimate_tok_s` with no real quantization evidence.
    Before this fix, `_weight_bytes` hit its unconditional-guess branch and
    multiplied `params` by `DEFAULT_BYTES_PER_PARAM` (0.58, "4-bit-ish"),
    turning a real 7B F16 checkpoint (~14GB) into a ~4.1GB estimate.

    RAM is pinned to 16GB (deliberately just above `RESERVE_BYTES` = 8e9, so
    the ~8GB usable pool is real headroom) rather than this suite's usual
    32GB: on 32GB even the WRONG 4.1GB guess and the RIGHT ~14GB figure both
    read as comfortably fitting, so that premise cannot distinguish "under-
    reported" from "correctly estimated" — 16GB is the smallest pin where
    the two readings disagree (no fit at all vs. a false "easy")."""
    _pin_hardware(monkeypatch, ram_gb=16.0)
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/gguf-unsuffixed", siblings=[{"rfilename": "model.gguf"}],
        gguf={"total": 7_000_000_000, "architecture": "llama"},
    )]))
    row = _search(client).json()["models"][0]
    assert row["file"] == "model.gguf"
    assert row["quant"] is None
    # The real, quantization-invariant params count is still shown...
    assert row["params"] == 7_000_000_000
    # ...but must not be turned into a guessed footprint: no verdict at all,
    # and certainly never "easy".
    assert row["fit"] is None
    assert row["speedEstimate"] is None


def test_gguf_row_with_no_gguf_metadata_at_all_still_has_no_params(client, hub_cache, monkeypatch):
    """No crash, and no invented number, when the Hub genuinely has nothing
    under `gguf` for this repo (a shape older or unusual repos can have)."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/gguf-no-meta", siblings=[{"rfilename": "x-Q4_K_M.gguf"}],
    )]))
    row = _search(client).json()["models"][0]
    assert row["file"] == "x-Q4_K_M.gguf"
    assert row["params"] is None


def test_gguf_row_with_real_params_scores_above_no_params_via_capability_alone(
        client, hub_cache, monkeypatch):
    """`params` (the Hub's real `gguf.total`) still moves the ranking: a row
    that knows its real parameter count scores above one that does not, on
    the capability axis alone. B (bugbot) changes what happens to `fit` for
    the meta row specifically — a recognised quant token (`Q4_K_M`) paired
    with a real params count is now judgeable (`params x
    quant_bytes_per_param`, `sizeSource: "estimated"`) — but the no-meta row
    has no params at all, so it stays unjudgeable regardless of its quant
    token, exactly as before."""
    _pin_hardware(monkeypatch, ram_gb=32.0)
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    same = dict(downloads=1000, createdAt="2026-08-01T00:00:00.000Z")
    # 7B sits well above `_CAPABILITY_DEFAULT` (30.0) on this machine's own
    # capability curve (`_capability_anchor_params(32.0)` ~= 11.2B params) —
    # a tiny model's real params can score BELOW the "unknown" default here,
    # so this figure is chosen deliberately, not incidentally.
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/gguf-with-meta", siblings=[{"rfilename": "x-Q4_K_M.gguf"}],
             gguf={"total": 7_000_000_000}, **same),
        _hit("org/gguf-no-meta", siblings=[{"rfilename": "y-Q4_K_M.gguf"}], **same),
    ]))
    body = _search(client).json()
    by_id = {m["id"]: m for m in body["models"]}
    with_meta = by_id["org/gguf-with-meta"]
    no_meta = by_id["org/gguf-no-meta"]
    assert with_meta["fit"] is not None
    assert with_meta["sizeSource"] == "estimated"
    assert no_meta["fit"] is None
    assert no_meta["speedEstimate"] is None
    assert with_meta["params"] == 7_000_000_000
    assert no_meta["params"] is None
    assert with_meta["matchScore"] > no_meta["matchScore"]


def test_gguf_row_with_recognized_quant_and_no_cache_estimates_from_params(
        client, hub_cache, monkeypatch):
    """B (bugbot): D1249's gate refused to turn ANY GGUF row's `params` into
    a footprint, on the reasoning that an unrecognised quant token would
    silently fall back to `DEFAULT_BYTES_PER_PARAM` (0.58, "4-bit-ish") — a
    guess that could be badly wrong. D1250 then filled `QUANT_BYTES_PER_PARAM`
    with real bytes/param figures for the GGUF suffixes this codebase already
    ranks, which reopens exactly the case the old gate could not tell apart
    from a guess: a RECOGNISED token paired with a REAL params count is not a
    guess, it is `params x quant_bytes_per_param(token)` — the same formula
    `_weight_bytes` already trusts for a curated catalog entry. A 30B
    `Q8_K_XL` file's real bytes/param (1.06, D1250) puts its footprint at
    ~31.8GB, which must read as unfit (`"no"`) on a 32GB machine — the OLD
    default-bpp guess (0.58) would have read 17.4GB, comfortably "easy",
    which is exactly the under-report this round closes."""
    _pin_hardware(monkeypatch, ram_gb=32.0)
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/gguf-q8-k-xl", siblings=[{"rfilename": "x-Q8_K_XL.gguf"}],
        gguf={"total": 30_000_000_000, "architecture": "llama"},
    )]))
    row = _search(client).json()["models"][0]
    assert row["file"] == "x-Q8_K_XL.gguf"
    assert row["quant"] == "Q8_K_XL"
    assert row["params"] == 30_000_000_000
    assert row["sizeSource"] == "estimated"
    assert row["fit"] is not None
    assert row["fit"]["verdict"] == "no"
    assert row["fit"]["footprintBytes"] == pytest.approx(
        30_000_000_000 * 1.06 + fit.RUNTIME_OVERHEAD_BYTES, rel=0.01)
    assert row["speedEstimate"] is not None


def test_gguf_row_with_unrecognized_quant_still_reports_no_derived_fit(
        client, hub_cache, monkeypatch):
    """An unrecognised quant token (`fit._quant_key` returns `None`) must
    stay unjudgeable — the whole point of D1249's original gate — rather
    than silently falling back to `DEFAULT_BYTES_PER_PARAM`."""
    _pin_hardware(monkeypatch, ram_gb=32.0)
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/gguf-fake", siblings=[{"rfilename": "x-IQ9_FAKE.gguf"}],
        gguf={"total": 30_000_000_000, "architecture": "llama"},
    )]))
    row = _search(client).json()["models"][0]
    assert row["fit"] is None
    assert row["speedEstimate"] is None
    assert row["sizeSource"] is None


def test_gguf_estimated_row_ranks_below_a_fitting_q4_row(client, hub_cache, monkeypatch):
    """The estimated footprint must actually feed `matchScore` (D780's
    composite), not merely be computed and discarded: a 30B Q8_K_XL row that
    reads unfit must rank below a 7B Q4_K_M row that comfortably fits."""
    _pin_hardware(monkeypatch, ram_gb=32.0)
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    same = dict(downloads=1000, createdAt="2026-08-01T00:00:00.000Z")
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/big-q8", siblings=[{"rfilename": "x-Q8_K_XL.gguf"}],
             gguf={"total": 30_000_000_000, "architecture": "llama"}, **same),
        _hit("org/small-q4", siblings=[{"rfilename": "y-Q4_K_M.gguf"}],
             gguf={"total": 7_000_000_000, "architecture": "llama"}, **same),
    ]))
    body = _search(client).json()
    by_id = {m["id"]: m for m in body["models"]}
    assert by_id["org/big-q8"]["fit"]["verdict"] == "no"
    assert by_id["org/small-q4"]["fit"]["verdict"] in ("easy", "tight")
    assert by_id["org/small-q4"]["matchScore"] > by_id["org/big-q8"]["matchScore"]


def test_gguf_row_uses_a_cached_real_file_size_when_hub_size_already_resolved_one(
        client, hub_cache, monkeypatch):
    """SPEC item 3: a real byte count for this exact (id, file) pair, already
    cached by `api_hub_size` (the lazy per-card lookup — see its own
    docstring), is reused here rather than left on the floor — the row's
    `fit`/`speedEstimate` are judged off THOSE real bytes, never a
    `params * bpp` estimate, even when the estimate would ALSO be judgeable
    (B, bugbot: a recognised quant plus known params now estimates on its
    own — this test's "unknown" row has no `params` at all, so it stays
    unjudgeable by either path, proving a cache miss with nothing else to go
    on is still refused)."""
    _pin_hardware(monkeypatch, ram_gb=32.0)
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/gguf-known-size", siblings=[{"rfilename": "x-Q8_K_XL.gguf"}],
             gguf={"total": 30_000_000_000}),
        _hit("org/gguf-unknown-size", siblings=[{"rfilename": "y-Q8_K_XL.gguf"}]),
    ]))
    # ~31.5GB real bytes for the known row — the same figure this file's own
    # docstrings use as the "real" contrast against the 17.4GB guess.
    real_bytes = 31_500_000_000
    key = ("size", hub.hub_endpoint(), "org/gguf-known-size", "x-Q8_K_XL.gguf", False)
    hub._store(key, {"usedStorage": None, "fileSize": real_bytes})

    body = _search(client).json()
    by_id = {m["id"]: m for m in body["models"]}
    known = by_id["org/gguf-known-size"]
    unknown = by_id["org/gguf-unknown-size"]

    assert known["fit"] is not None
    # `footprintBytes` is the weight bytes PLUS `fit.RUNTIME_OVERHEAD_BYTES`
    # (0.5GB) — the "download" rung's own fixed runtime allowance, unrelated
    # to which source (real cache vs. a guess) supplied the weight bytes.
    assert known["fit"]["footprintBytes"] == real_bytes + fit.RUNTIME_OVERHEAD_BYTES
    # The guessed figure `Q8_K_XL`'s recognised bpp would have produced for
    # 30B params (~32GB weights, ~32.5GB with overhead) must NOT be what won
    # — proves the real cached bytes were used, not `params * bpp`.
    guessed_weight_bytes = 30_000_000_000 * fit.quant_bytes_per_param("Q8_K_XL")
    assert known["fit"]["footprintBytes"] != guessed_weight_bytes + fit.RUNTIME_OVERHEAD_BYTES
    assert known["speedEstimate"] is not None
    assert known["sizeSource"] == "cached"

    assert unknown["fit"] is None
    assert unknown["speedEstimate"] is None
    assert unknown["sizeSource"] is None


# -- D793: a GGUF row outside text generation is rankable and findable ------
#
# Every test above resolves a `file`, because `_gguf_runner` stands in for
# llama.cpp and text generation is the one capability whose runners declare
# the `gguf` format tag. The three below are the OTHER capabilities, where
# no runner declares it: nothing asks `pick_gguf_file` for anything, so
# `file` stays None. `params` must NOT follow it into None: without a real
# parameter count, three of the five ranking axes fall back to
# missing-evidence constants, identical for every such repo, and the whole
# format ends up in one flat band the truncation to `count` then cuts off.


def test_a_fileless_gguf_row_still_reports_the_hubs_own_params(
        client, hub_cache, monkeypatch):
    """A `text-to-image` GGUF republish — `leejet/FLUX.2-klein-4B-GGUF`'s
    shape. No runner for that capability speaks GGUF, so the picker never
    runs and `file` is None; `gguf.total` is a fact about the REPO, not
    about this machine's runners, so it is read anyway."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner(tags=()))
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "leejet/FLUX.2-klein-4B-GGUF", pipeline_tag="text-to-image",
        siblings=[{"rfilename": "flux-2-klein-4b-Q4_0.gguf"}],
        gguf={"total": 3_875_544_576},
    )]))
    row = _search(client).json()["models"][0]
    assert row["file"] is None
    assert row["params"] == 3_875_544_576
    assert row["format"] == "gguf"
    # The deleted-derivation rule follows the PARAMS, not the `file`: a
    # `gguf.total` count with no real bytes beside it must not become a
    # `params x DEFAULT_BYTES_PER_PARAM` verdict any more than a
    # file-resolved one may.
    assert row["fit"] is None
    assert row["speedEstimate"] is None


def test_a_fileless_gguf_row_outranks_an_identical_one_with_no_metadata(
        client, hub_cache, monkeypatch):
    """The ranking half of D793. Two `text-to-image` GGUF repos, identical
    in downloads and age, one with `gguf.total` and one without: the blend
    must be able to tell them apart. Before this fix neither had `params`,
    so both scored off `_FIT_DEFAULT` + `_capability_score(None)` +
    `_speed_score(None, None)` and tied — and with recency and popularity
    the only live axes, every GGUF repo of every quality landed in one flat
    band at the truncation boundary."""
    _pin_hardware(monkeypatch, ram_gb=32.0)
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner(tags=()))
    # 9B, chosen the same way the file-resolved test above chooses 7B: well
    # above `_CAPABILITY_DEFAULT` on a 32GB machine's own curve, so the
    # comparison measures real params beating "unknown" rather than the
    # accident of a tiny model scoring under the default.
    same = dict(pipeline_tag="text-to-image", downloads=1000,
                createdAt="2026-08-01T00:00:00.000Z",
                siblings=[{"rfilename": "m-Q4_0.gguf"}])
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/gguf-with-meta", gguf={"total": 9_078_581_248}, **same),
        _hit("org/gguf-no-meta", **same),
    ]))
    by_id = {m["id"]: m for m in _search(client).json()["models"]}
    assert by_id["org/gguf-with-meta"]["params"] == 9_078_581_248
    assert by_id["org/gguf-no-meta"]["params"] is None
    assert by_id["org/gguf-with-meta"]["matchScore"] > by_id["org/gguf-no-meta"]["matchScore"]


def test_a_repo_publishing_both_formats_is_not_labelled_gguf(
        client, hub_cache, monkeypatch):
    """`format` names what the Download button would FETCH, and for a repo
    shipping both uploads that is the safetensors one — which is also what
    `params`, `estimatedSize`, `quant` and `fit` on this row already
    describe. Calling it `"gguf"` would split it away from its own base
    model's family (the mirror/variant grouping the frontend's search screen
    draws on) on the strength of a secondary upload nothing else here reads."""
    _pin_hardware(monkeypatch, ram_gb=32.0)
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner(tags=()))
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/both-formats", pipeline_tag="text-to-image",
        siblings=[{"rfilename": "m-Q4_0.gguf"}],
        safetensors={"parameters": {"BF16": 4_000_000_000}, "total": 4_000_000_000},
        gguf={"total": 4_000_000_000},
    )]))
    row = _search(client).json()["models"][0]
    assert row["format"] is None
    # Read off the safetensors map, which is also why the row is judgeable.
    assert row["estimatedSize"] == 8_000_000_000
    assert row["fit"] is not None


def test_a_safetensors_only_repo_carries_no_format(client, hub_cache, monkeypatch):
    """`format` is only ever set from something the Hub actually said. A
    repo with no `gguf` metadata gets None — never a guess from its name."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/plain", safetensors={"parameters": {"BF16": 1_000_000}, "total": 1_000_000},
    )]))
    assert _search(client).json()["models"][0]["format"] is None


# -- item 9c: in-repo weight variants ----------------------------------------


def test_a_multi_quant_gguf_repo_counts_each_quant_as_a_variant(client, hub_cache, monkeypatch):
    """A GGUF repo shipping several quantizations of the same checkpoint —
    `Q4_K_M`, `Q5_K_M`, `Q8_0` — is three variants, one per `.gguf` sibling."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/multi-quant",
        siblings=[
            {"rfilename": "model-Q4_K_M.gguf"},
            {"rfilename": "model-Q5_K_M.gguf"},
            {"rfilename": "model-Q8_0.gguf"},
        ],
    )]))
    assert _search(client).json()["models"][0]["variantCount"] == 3


def test_an_mmproj_sibling_is_not_counted_as_its_own_variant(client, hub_cache, monkeypatch):
    """A vision-projector `mmproj` GGUF shipped alongside a multimodal repo's
    real quantizations is a helper file, not a weight variant of its own."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/vlm-gguf",
        siblings=[
            {"rfilename": "model-Q4_K_M.gguf"},
            {"rfilename": "model-Q8_0.gguf"},
            {"rfilename": "mmproj-model-f16.gguf"},
        ],
    )]))
    assert _search(client).json()["models"][0]["variantCount"] == 2


def test_a_single_safetensors_repo_is_one_variant(client, hub_cache, monkeypatch):
    """The overwhelming default: no `.gguf` siblings and no bit-width/dtype
    subfolder convention, so this reads as the one weight set it plainly is."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/plain-st",
        safetensors={"parameters": {"BF16": 1_000_000}, "total": 1_000_000},
        siblings=[{"rfilename": "model.safetensors"}, {"rfilename": "config.json"}],
    )]))
    assert _search(client).json()["models"][0]["variantCount"] == 1


def test_a_sharded_gguf_quant_counts_once_not_once_per_shard(client, hub_cache, monkeypatch):
    """Item 5: `_count_variants` reuses `formats.pick_gguf_file`'s own split-
    shard exclusion (`GGUF_SPLIT_RE`) — a multi-part `-00001-of-00003.gguf`
    shard set is ONE quantization, not three, so a sharded repo does not
    inflate its variant count by however many parts that one quant happens
    to be split into."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/sharded-gguf",
        siblings=[
            {"rfilename": "model-Q8_0-00001-of-00003.gguf"},
            {"rfilename": "model-Q8_0-00002-of-00003.gguf"},
            {"rfilename": "model-Q8_0-00003-of-00003.gguf"},
            {"rfilename": "model-Q4_K_M.gguf"},
        ],
    )]))
    assert _search(client).json()["models"][0]["variantCount"] == 2


def test_a_draft_or_projector_gguf_sibling_is_not_counted_as_its_own_variant(
        client, hub_cache, monkeypatch):
    """Item 5: `_count_variants` now excludes the SAME auxiliary markers
    `formats.GGUF_AUXILIARY_RE` does (`mmproj`/`mtp`/`draft`/`projector`),
    not just its own narrower ad-hoc `("mmproj", "vision")` list — a
    speculative-decoding draft model shipped alongside the real quants must
    not inflate the count either."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/with-draft",
        siblings=[
            {"rfilename": "model-Q4_K_M.gguf"},
            {"rfilename": "model-Q8_0.gguf"},
            {"rfilename": "draft-model-Q4_0.gguf"},
        ],
    )]))
    assert _search(client).json()["models"][0]["variantCount"] == 2


# -- item 6: the variant list itself -----------------------------------------


def test_a_gguf_repos_variants_array_lists_each_file_and_its_quant(
        client, hub_cache, monkeypatch):
    """Item 6: a GGUF row's `variants` array names every candidate file (the
    same set `variantCount` above counts), each paired with its own published
    quant token off the filename — not a guess from the repo name."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/multi-quant",
        gguf={"total": 4_000_000_000},
        siblings=[
            {"rfilename": "model-Q4_K_M.gguf"},
            {"rfilename": "model-Q8_0.gguf"},
        ],
    )]))
    model = _search(client).json()["models"][0]
    assert model["variantCount"] == 2
    by_file = {v["file"]: v["quant"] for v in model["variants"]}
    assert by_file == {
        "model-Q4_K_M.gguf": "Q4_K_M",
        "model-Q8_0.gguf": "Q8_0",
    }
    assert all(v["downloadable"] for v in model["variants"])


def test_a_sharded_quants_variant_entry_is_marked_not_downloadable(
        client, hub_cache, monkeypatch):
    """Item 3 (code review): a multi-part shard set's collapsed entry (shard
    part 1) must be flagged `downloadable: False` — offering it for download
    would fetch one unusable shard, since `pick_gguf_file` refuses the same
    file as non-servable."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/sharded",
        gguf={"total": 4_000_000_000},
        siblings=[
            {"rfilename": "model-Q8_0-00001-of-00003.gguf"},
            {"rfilename": "model-Q8_0-00002-of-00003.gguf"},
            {"rfilename": "model-Q8_0-00003-of-00003.gguf"},
            {"rfilename": "model-Q4_K_M.gguf"},
        ],
    )]))
    model = _search(client).json()["models"][0]
    by_file = {v["file"]: v["downloadable"] for v in model["variants"]}
    assert by_file == {
        "model-Q8_0-00001-of-00003.gguf": False,
        "model-Q4_K_M.gguf": True,
    }


def test_a_non_gguf_repos_variants_array_is_null(client, hub_cache, monkeypatch):
    """A safetensors/MLX row has no per-file quant listing to offer — `variants`
    is null rather than a fake single-entry list, mirroring how `_count_variants`
    still gives it a `variantCount` of 1 from its subfolder/dtype convention."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/plain-st",
        safetensors={"parameters": {"BF16": 1_000_000}, "total": 1_000_000},
        siblings=[{"rfilename": "model.safetensors"}, {"rfilename": "config.json"}],
    )]))
    model = _search(client).json()["models"][0]
    assert model["variantCount"] == 1
    assert model["variants"] is None


def test_the_on_disk_gguf_file_is_named_in_local_state(client, hub_cache, monkeypatch):
    """Item 6: when exactly one `.gguf` file sits in a repo's default snapshot,
    `local.file` names it — so a variant list can mark which one is already on
    disk without re-deriving it from `local.files`' bare count."""
    repo_dir = hub_cache / "models--org--have-gguf"
    blob = repo_dir / "blobs" / "b1"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x" * 64)
    snapshot = repo_dir / "snapshots" / "c1"
    snapshot.mkdir(parents=True)
    try:
        os.symlink(blob, snapshot / "model-Q4_K_M.gguf")
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    refs = repo_dir / "refs"
    refs.mkdir()
    (refs / "main").write_text("c1")
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/have-gguf")]))
    model = _search(client).json()["models"][0]
    assert model["local"]["file"] == "model-Q4_K_M.gguf"


def test_local_file_is_none_when_more_than_one_gguf_is_on_disk(client, hub_cache, monkeypatch):
    """An ambiguous multi-GGUF snapshot refuses to guess which file "is the
    one" rather than naming the wrong one."""
    repo_dir = hub_cache / "models--org--two-guffs"
    blobs = repo_dir / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "b1").write_bytes(b"x" * 64)
    (blobs / "b2").write_bytes(b"y" * 64)
    snapshot = repo_dir / "snapshots" / "c1"
    snapshot.mkdir(parents=True)
    try:
        os.symlink(blobs / "b1", snapshot / "model-Q4_K_M.gguf")
        os.symlink(blobs / "b2", snapshot / "model-Q8_0.gguf")
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support symlinks")
    refs = repo_dir / "refs"
    refs.mkdir()
    (refs / "main").write_text("c1")
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/two-guffs")]))
    model = _search(client).json()["models"][0]
    assert model["local"]["file"] is None


def test_a_bitwidth_subfoldered_repo_counts_each_folder_as_a_variant(client, hub_cache, monkeypatch):
    """`mlx-community`'s own convention: several bit-width subfolders under
    one repo, each a distinct weight variant."""
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "mlx-community/some-model",
        siblings=[
            {"rfilename": "4bit/model.safetensors"},
            {"rfilename": "4bit/config.json"},
            {"rfilename": "8bit/model.safetensors"},
            {"rfilename": "8bit/config.json"},
        ],
    )]))
    assert _search(client).json()["models"][0]["variantCount"] == 2


# -- the request ------------------------------------------------------------


def test_the_query_is_encoded_not_concatenated(client, hub_cache, monkeypatch):
    # A search for `a&b` is a search, not a second parameter.
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"q": "a&filter=evil b/c"})
    url = fake.calls[0][0]
    assert "search=a%26filter%3Devil+b%2Fc" in url
    assert url.startswith("https://huggingface.co/api/models?")


def test_the_sort_is_a_fixed_set(client, hub_cache, monkeypatch):
    # The client names a sort; it never passes a field through to the Hub.
    monkeypatch.setattr(httpx, "get", _reply([]))
    assert _search(client, {"sort": "likes"}).status_code == 200
    bad = _search(client, {"sort": "author"})
    assert bad.status_code == 400 and "sort" in bad.json()["error"]


def test_the_limit_is_bounded(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    body = _search(client, {"limit": 5000}).json()
    assert body["query"]["limit"] == hub._MAX_LIMIT


def test_identical_queries_inside_the_window_ask_once(client, hub_cache, monkeypatch):
    # Search-as-you-type would otherwise put one request per keystroke on a
    # public API.
    fake = _reply([_hit("org/m")])
    monkeypatch.setattr(httpx, "get", fake)
    for _ in range(3):
        _search(client, {"q": "llama"})
    assert len(fake.calls) == 1
    _search(client, {"q": "llamas"})
    assert len(fake.calls) == 2  # …a different query is a different question


def test_include_unfit_is_always_on_now_an_old_client_sending_false_is_ignored(
        client, hub_cache, monkeypatch):
    """Item 7 (fix round 5): the "Show models that will not fit" toggle is
    gone from the frontend — the server always behaves as `includeUnfit:
    True` regardless of what an old saved page/client sends, so a `false`
    from one no longer changes the fetch size or drops any `verdict: "no"`
    row."""
    fake = _reply([_hit("org/m")])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"task": "text-generation", "includeUnfit": True})
    _search(client, {"task": "text-generation", "includeUnfit": False, "q": "distinct"})
    limits = [parse_qs(urlsplit(url).query)["limit"][0] for url, _ in fake.calls]
    assert limits == ["24", "24"]


def test_a_token_is_sent_but_never_returned(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setattr(httpx, "get", fake)
    body = _search(client).json()
    assert fake.calls[0][1]["headers"]["Authorization"] == "Bearer hf_secret"
    assert body["authenticated"] is True
    assert "hf_secret" not in json.dumps(body)


def test_the_token_hf_holds_is_the_one_sent(client, hub_cache, monkeypatch):
    """D402: the search sends whatever `get_token()` finds — a login made from
    Preferences, or a `hf auth login` in a terminal, indistinguishable here by
    design. This app stores no token of its own, so there is no second
    resolution that could disagree with the download beside it."""
    from huggingface_hub._login import _save_token, _set_active_token

    _save_token(token="hf_from_hfs_own_store", token_name="fused-render")
    _set_active_token(token_name="fused-render", add_to_git_credential=False)
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    body = _search(client).json()
    assert fake.calls[0][1]["headers"]["Authorization"] == "Bearer hf_from_hfs_own_store"
    assert body["authenticated"] is True
    assert "hf_from_hfs_own_store" not in json.dumps(body)


def test_an_environment_token_still_wins(client, hub_cache, monkeypatch):
    # hf's own order, which this app no longer has any opinion about: the
    # variable beats the store, here and inside every worker, because both ask
    # the same library.
    from huggingface_hub._login import _save_token, _set_active_token

    _save_token(token="hf_from_hfs_own_store", token_name="fused-render")
    _set_active_token(token_name="fused-render", add_to_git_credential=False)
    monkeypatch.setenv("HF_TOKEN", "hf_from_the_environment")
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client)
    assert (fake.calls[0][1]["headers"]["Authorization"]
            == "Bearer hf_from_the_environment")


@pytest.mark.parametrize("endpoint,expected", [
    ("https://hf-mirror.example", "https://hf-mirror.example"),
    ("file:///etc", "https://huggingface.co"),
    ("not a url", "https://huggingface.co"),
    ("", "https://huggingface.co"),
])
def test_the_endpoint_override_must_be_an_http_url(monkeypatch, endpoint, expected):
    # HF_ENDPOINT is the standard mirror override and comes from the user's own
    # environment — but it is still checked before it becomes a request.
    monkeypatch.setenv("HF_ENDPOINT", endpoint)
    assert hub.hub_endpoint() == expected


# -- D412: the runner-declared filter tag, ANDed onto the task filter -------


def test_the_gguf_tag_is_anded_onto_the_hub_request_when_llamacpp_is_active(
        client, hub_cache, monkeypatch):
    """Confirmed live: the Hub ANDs multiple `filter=` values, and the router
    already sends `urlencode(..., doseq=True)` — so a runner declaring a
    format tag turns into a SECOND `filter=` on the wire, not a new
    parameter shape."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"task": "text-generation"})
    url = fake.calls[0][0]
    assert "filter=text-generation" in url and "filter=gguf" in url


def test_no_format_tag_is_added_when_the_active_runner_declares_none(
        client, hub_cache, monkeypatch):
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner(tags=()))
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"task": "text-generation"})
    url = fake.calls[0][0]
    # `expand[]=gguf` (fix for code review finding 1) rides every request
    # unconditionally now — it costs nothing and the join needs it for any
    # row that turns out to resolve a GGUF file, so its presence here is not
    # evidence of a format FILTER; only `filter=gguf` would be.
    assert "filter=text-generation" in url and "filter=gguf" not in url


def test_no_format_tag_is_added_without_a_task_filter(client, hub_cache, monkeypatch):
    """A bare keyword search spans every supported tag at once — there is no
    SINGLE capability to resolve a runner for, so the Hub-side narrowing is
    skipped and `_model_row`'s own per-row check is the only gate, the same
    two-layer shape the pipeline-tag filter itself already has."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"q": "llama"})
    assert "filter=" not in fake.calls[0][0]


def test_the_cache_does_not_survive_an_engine_switch(client, hub_cache, monkeypatch):
    """A preference switched live (CT-5, no restart) changes which runner
    serves the capability — the SAME query/task/sort/count must not be
    served from a cache entry built under the OTHER engine's filter."""
    fake = _reply([_hit("org/m")])
    monkeypatch.setattr(httpx, "get", fake)

    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    _search(client, {"task": "text-generation"})
    assert len(fake.calls) == 1

    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner(tags=()))
    _search(client, {"task": "text-generation"})
    assert len(fake.calls) == 2  # a different engine choice is a different question


# -- D843: `capability`, resolved to every tag it reaches --------------------


def test_a_capability_with_one_tag_behaves_like_the_old_task_filter(client, hub_cache, monkeypatch):
    # D1235: IMAGE_GENERATION stopped being a one-tag capability once
    # `image-to-image` joined `text-to-image` under it (tasks.py), so
    # SPEECH_TO_TEXT (still one tag: `automatic-speech-recognition`) is the
    # example here now; the two-tag case is covered just below.
    fake = _reply([_hit("org/m", pipeline_tag="automatic-speech-recognition")])
    monkeypatch.setattr(httpx, "get", fake)
    resp = _search(client, {"capability": registry.SPEECH_TO_TEXT})
    assert resp.status_code == 200
    assert len(fake.calls) == 1
    assert "filter=automatic-speech-recognition" in fake.calls[0][0]


def test_image_generation_now_reaches_two_tags(client, hub_cache, monkeypatch):
    """D1235: `image-to-image` joined `text-to-image` under IMAGE_GENERATION,
    so a capability search over it fetches both tags — one Hub request per
    tag, same as any other multi-tag capability."""
    fake = _reply([_hit("org/m", pipeline_tag="text-to-image")])
    monkeypatch.setattr(httpx, "get", fake)
    resp = _search(client, {"capability": registry.IMAGE_GENERATION})
    assert resp.status_code == 200
    assert len(fake.calls) == 2
    assert any("filter=text-to-image" in c[0] for c in fake.calls)
    assert any("filter=image-to-image" in c[0] for c in fake.calls)


def test_an_unrecognised_capability_400s(client, hub_cache):
    resp = _search(client, {"capability": "not-a-real-capability"})
    assert resp.status_code == 400
    assert "not-a-real-capability" in resp.json()["error"]


def test_embeddings_capability_searches_every_tag_it_reaches(client, hub_cache, monkeypatch):
    """`embeddings` is the one capability reached by three tags at once
    (`ai_tasks.tags_for_capability`'s docstring) — no single `filter=` can
    express it, so this is one Hub request per tag, merged."""
    def fake(url, **kwargs):
        fake.calls.append((url, kwargs))
        q = parse_qs(urlsplit(url).query)
        tag = q["filter"][0]
        rows = {
            "feature-extraction": [_hit("org/fe", pipeline_tag="feature-extraction")],
            "sentence-similarity": [_hit("org/ss", pipeline_tag="sentence-similarity")],
            "zero-shot-image-classification": [
                _hit("org/zs", pipeline_tag="zero-shot-image-classification")],
        }[tag]
        return httpx.Response(200, content=json.dumps(rows).encode(),
                              request=httpx.Request("GET", url))
    fake.calls = []
    monkeypatch.setattr(httpx, "get", fake)
    resp = _search(client, {"capability": registry.EMBEDDINGS})
    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()["models"]}
    assert ids == {"org/fe", "org/ss", "org/zs"}
    assert len(fake.calls) == 3


def test_embeddings_search_dedupes_a_repo_seen_through_more_than_one_tag(
        client, hub_cache, monkeypatch):
    def fake(url, **kwargs):
        fake.calls.append((url, kwargs))
        q = parse_qs(urlsplit(url).query)
        tag = q["filter"][0]
        # The same repo id turns up behind two different tag pages — a real
        # repo cannot claim two `pipeline_tag`s, but nothing stops the Hub
        # from returning the same id for two different keyword-search pages.
        rows = {
            "feature-extraction": [_hit("org/dupe", pipeline_tag="feature-extraction")],
            "sentence-similarity": [_hit("org/dupe", pipeline_tag="feature-extraction")],
            "zero-shot-image-classification": [],
        }[tag]
        return httpx.Response(200, content=json.dumps(rows).encode(),
                              request=httpx.Request("GET", url))
    fake.calls = []
    monkeypatch.setattr(httpx, "get", fake)
    resp = _search(client, {"capability": registry.EMBEDDINGS})
    ids = [m["id"] for m in resp.json()["models"]]
    assert ids.count("org/dupe") == 1


def test_capability_search_drops_a_row_whose_classified_capability_differs(
        client, hub_cache, monkeypatch):
    """D851 (fix round 6, item 1): the Hub's `filter=<tag>` matches ANY tag in
    a repo's tag list, so a `capability: "embeddings"` search (which resolves
    to `feature-extraction`/`sentence-similarity`/
    `zero-shot-image-classification`) also pulls back a text-generation repo
    that merely carries `feature-extraction` among its other tags. The row's
    own classified capability (`pipeline_tag: "text-generation"`) is the
    ground truth and must exclude it, even though the Hub's tag filter let it
    through."""
    def fake(url, **kwargs):
        fake.calls.append((url, kwargs))
        q = parse_qs(urlsplit(url).query)
        tag = q["filter"][0]
        rows = {
            "feature-extraction": [
                _hit("org/real-embeddings", pipeline_tag="feature-extraction"),
                _hit("org/off-capability", pipeline_tag="text-generation",
                     tags=["feature-extraction"]),
            ],
            "sentence-similarity": [],
            "zero-shot-image-classification": [],
        }[tag]
        return httpx.Response(200, content=json.dumps(rows).encode(),
                              request=httpx.Request("GET", url))
    fake.calls = []
    monkeypatch.setattr(httpx, "get", fake)
    resp = _search(client, {"capability": registry.EMBEDDINGS})
    assert resp.status_code == 200
    ids = {m["id"] for m in resp.json()["models"]}
    assert ids == {"org/real-embeddings"}


def test_speech_to_text_capability_resolves_its_one_tag(client, hub_cache, monkeypatch):
    fake = _reply([_hit("org/whisper", pipeline_tag="automatic-speech-recognition")])
    monkeypatch.setattr(httpx, "get", fake)
    resp = _search(client, {"capability": registry.SPEECH_TO_TEXT})
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["models"]] == ["org/whisper"]
    assert "filter=automatic-speech-recognition" in fake.calls[0][0]


# -- fit, speed and age (task 1) --------------------------------------------


def test_a_row_with_params_carries_fit_speed_and_created(client, hub_cache, monkeypatch):
    # 8B params at BF16 is a real footprint fit.verdict can judge, and a
    # text-generation row is exactly the capability speed.estimate_tok_s covers.
    #
    # The premise: 32GB/no-GPU (`_pin_hardware`) fits 16GB comfortably on any
    # runner. Without it this reads the real host's memory — CI's judges the
    # row `verdict: "no"`, the default filter drops it, and `models` is empty
    # before `[0]` below ever runs (the bug this test was written to catch).
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/big",
        createdAt="2026-08-01T00:00:00.000Z",
        safetensors={"parameters": {"BF16": 8_000_000_000}, "total": 8_000_000_000},
    )]))
    row = _search(client).json()["models"][0]
    assert row["created"] == "2026-08-01T00:00:00.000Z"
    assert row["fit"] is not None
    assert set(row["fit"]) == {"verdict", "basis", "footprintBytes", "score", "runMode", "poolBytes", "poolName"}
    assert row["speedEstimate"] is not None
    assert "tokensPerSecond" in row["speedEstimate"]


def test_a_row_with_no_params_and_no_size_carries_nulls_not_a_guess(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/gguf", safetensors=None)]))
    row = _search(client).json()["models"][0]
    assert row["fit"] is None
    assert row["speedEstimate"] is None
    assert row["created"] is None


def test_speed_estimate_is_absent_for_a_non_text_capability(client, hub_cache, monkeypatch):
    # The premise: 32GB/no-GPU (`_pin_hardware`) fits the 8GB fixture below
    # on any runner, so this row is never dropped by the unfit default before
    # the `fit is not None` assertion gets a chance to run.
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([
        {"id": "org/pic", "pipeline_tag": "text-to-image",
         "safetensors": {"parameters": {"BF16": 4_000_000_000}, "total": 4_000_000_000}},
    ]))
    row = _search(client).json()["models"][0]
    assert row["speedEstimate"] is None
    # fit still applies — it is not text-generation-only.
    assert row["fit"] is not None


def test_the_hardware_and_footprint_store_are_read_once_per_request_not_per_row(
        client, hub_cache, monkeypatch):
    from fused_render.ai import footprints, hw_detect

    load_store_calls = []
    cached_hardware_calls = []
    monkeypatch.setattr(hub.footprints, "load_store",
                        lambda: (load_store_calls.append(1), None)[1])
    monkeypatch.setattr(hub.hw_detect, "cached_hardware",
                        lambda: (cached_hardware_calls.append(1), None)[1])
    monkeypatch.setattr(httpx, "get", _reply([_hit(f"org/m{i}") for i in range(5)]))
    _search(client)
    assert load_store_calls == [1]
    assert cached_hardware_calls == [1]


# -- one entry per model family (task 3) -------------------------------------


def test_a_base_model_tag_is_parsed_into_basemodel_and_relation(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "unsloth/x-GGUF", tags=["base_model:quantized:org/x"])]))
    row = _search(client).json()["models"][0]
    assert row["baseModel"] == "org/x"
    assert row["relation"] == "quantized"


def test_a_finetune_relation_is_parsed_too(client, hub_cache, monkeypatch):
    # MLX ports on this machine mostly declare `finetune`, not `quantized` — a
    # grouping keyed on `quantized` alone would split exactly these families.
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "mlx-community/gemma-3-12b-it-4bit", tags=["base_model:finetune:google/gemma-3-12b-it"])]))
    row = _search(client).json()["models"][0]
    assert row["baseModel"] == "google/gemma-3-12b-it"
    assert row["relation"] == "finetune"


def test_a_relation_less_base_model_tag_still_groups(client, hub_cache, monkeypatch):
    # The Hub emits `base_model:<id>` with no second colon when a model
    # card sets `base_model:` metadata but never `base_model_relation:` —
    # `rest.partition(":")` on this form yields an empty `sep`, which an
    # earlier version of `_base_model` treated as malformed and dropped,
    # silently ungrouping a large share of repos.
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "someorg/Qwen3-8B-custom", tags=["base_model:Qwen/Qwen3-8B"])]))
    row = _search(client).json()["models"][0]
    assert row["baseModel"] == "Qwen/Qwen3-8B"
    assert row["relation"] is None


def test_a_row_with_no_base_model_tag_carries_nulls(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/standalone", tags=["region:us"])]))
    row = _search(client).json()["models"][0]
    assert row["baseModel"] is None
    assert row["relation"] is None


def test_a_row_with_no_tags_at_all_carries_nulls_not_a_500(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/bare", tags=None)]))
    row = _search(client).json()["models"][0]
    assert row["baseModel"] is None
    assert row["relation"] is None


# -- D804: a family is not left straddling the `limit` boundary -------------
#
# `sort=downloads` throughout: with no explicit sort the composite "best"
# score reorders `models` itself, which would make the fixtures' own list
# order say nothing about what survives truncation. Under `downloads`
# nothing here re-sorts (see `api_hub_search`), so the row order below IS
# the rank order, and `limit` cuts the array at exactly the index it names.


def test_a_below_boundary_variant_is_pulled_in_with_its_kept_base(
        client, hub_cache, monkeypatch):
    rows = [
        _hit("org/base", downloads=100),
        _hit("org/base-4bit", downloads=1, tags=["base_model:quantized:org/base"]),
    ]
    monkeypatch.setattr(httpx, "get", _reply(rows))
    body = _search(client, {"sort": "downloads", "limit": 1}).json()
    assert body["query"]["limit"] == 1
    assert [m["id"] for m in body["models"]] == ["org/base", "org/base-4bit"]


def test_a_below_boundary_base_is_pulled_up_by_its_kept_variant(
        client, hub_cache, monkeypatch):
    # The reverse direction (c): the higher-ranked row is the REPUBLISH, and
    # its base sits below the cut. The base still has to surface — D802 makes
    # it the family's primary the moment it is present — so it comes back
    # even though nothing about its own rank would have kept it.
    rows = [
        _hit("org/quant", downloads=100, tags=["base_model:quantized:org/original"]),
        _hit("org/original", downloads=1),
    ]
    monkeypatch.setattr(httpx, "get", _reply(rows))
    body = _search(client, {"sort": "downloads", "limit": 1}).json()
    ids = [m["id"] for m in body["models"]]
    assert set(ids) == {"org/quant", "org/original"}
    # D859: `_pull_in_family_members` decides membership only — the caller
    # now re-sorts the union by the same key (`downloads` desc) the page was
    # already ranked on, so the pulled-in base lands at ITS OWN rank (last,
    # since it has the lower download count), not adjacent to the variant
    # that named it.
    assert ids == ["org/quant", "org/original"]


def test_the_untagged_mirror_signal_pulls_in_a_republish_too(
        client, hub_cache, monkeypatch):
    # No `base_model:` tag on either side — the mirror key (trailing name
    # segment + exact params + quant) is the only signal there is, same as
    # the frontend search screen's own untagged fallback.
    #
    # D810: this test is about the mirror-pull-in signal, not about fit — but
    # both hits carry a real BF16 dtype total (~14GB), so without a pinned
    # machine the fit verdict follows whatever RAM the runner actually has.
    # On a small CI runner that verdict is "no", and the default
    # `includeUnfit=false` search path (`hub_models.py`) drops both rows
    # before the mirror-pull-in logic this test targets ever sees them —
    # never green on CI, always green on a 32GB+ dev Mac. `_pin_hardware`
    # (the same helper the fit-specific tests below use) fixes the premise so
    # the assertion tests only what it claims to.
    _pin_hardware(monkeypatch, ram_gb=32.0)
    dtype = {"parameters": {"BF16": 7_000_000_000}, "total": 7_000_000_000}
    rows = [
        _hit("first-org/Weights-7B", downloads=100, safetensors=dtype),
        _hit("second-org/Weights-7B", downloads=1, safetensors=dtype),
    ]
    monkeypatch.setattr(httpx, "get", _reply(rows))
    body = _search(client, {"sort": "downloads", "limit": 1}).json()
    ids = {m["id"] for m in body["models"]}
    assert ids == {"first-org/Weights-7B", "second-org/Weights-7B"}


def test_the_per_family_cap_keeps_only_the_highest_ranked_overflow(
        client, hub_cache, monkeypatch):
    variants = [_hit(f"org/anchor-v{i}", downloads=100 - i,
                     tags=["base_model:quantized:org/anchor"])
               for i in range(15)]
    rows = [_hit("org/anchor", downloads=1000)] + variants
    monkeypatch.setattr(httpx, "get", _reply(rows))
    body = _search(client, {"sort": "downloads", "limit": 1}).json()
    ids = [m["id"] for m in body["models"]]
    assert ids[0] == "org/anchor"
    assert len(ids) == 1 + hub._FAMILY_PULL_IN_PER_FAMILY_CAP
    kept_variants = ids[1:]
    assert kept_variants == [f"org/anchor-v{i}" for i in range(hub._FAMILY_PULL_IN_PER_FAMILY_CAP)]
    for i in range(hub._FAMILY_PULL_IN_PER_FAMILY_CAP, 15):
        assert f"org/anchor-v{i}" not in ids


def test_a_candidate_matching_nothing_stays_cut(client, hub_cache, monkeypatch):
    rows = [
        _hit("org/kept", downloads=100),
        _hit("someone-else/unrelated-thing", downloads=1),
    ]
    monkeypatch.setattr(httpx, "get", _reply(rows))
    body = _search(client, {"sort": "downloads", "limit": 1}).json()
    ids = [m["id"] for m in body["models"]]
    assert ids == ["org/kept"]


# -- ranking by fit, trending, and hiding what cannot run (task 2) ----------


def _pin_hardware(monkeypatch, *, ram_gb=32.0):
    """Pin the machine `fit.verdict`/`speed.estimate_tok_s` judge a footprint
    against, so a row's fit/speed verdict is a property of the FIXTURE, not
    of whatever box happens to run the suite.

    Without this, a test whose fixture sits between "obviously fits" and
    "obviously doesn't" reads the real host: a dev Mac (32GB) and a CI
    runner (as little as 7GB) disagree about `verdict`, and since the
    unfit-by-default filter (D-numbered above) drops a `verdict: "no"` row
    before the assertions ever run, the failure shows up as an `IndexError`
    on an empty `models` list — CI-only, and unexplained unless you already
    know the premise.

    32GB/no-GPU (CPU-only) matches the dev machine this suite was written
    against, made explicit rather than left to be true by accident of the
    host — see D416, `_no_format_filter` above, for the identical shape of
    bug this same file already learned from once.
    """
    monkeypatch.setattr(hub.fit, "machine_ram_gb", lambda: ram_gb)
    monkeypatch.setattr(hub.fit, "_wired_limit_mb", lambda: None)
    monkeypatch.setattr(hub.hw_detect, "cached_hardware", lambda: hw_detect.HardwareInfo(
        gpus=[], total_vram_gb=0.0, bandwidth_gb_s=None, detected_at=0.0))


def _fitted(model_id, score, safetensors_gb, **extra):
    """A Hub row whose safetensors size makes `fit.verdict` produce a
    deterministic score — big enough for a clearly-"no" row, small enough for
    a clearly-"easy" one. `params`/dtype don't matter here, only the resulting
    byte total, so this fabricates a BF16 map sized to reach roughly
    `safetensors_gb` GB."""
    count = int(safetensors_gb * 1e9 / 2)  # BF16: 2 bytes/param
    return _hit(model_id, safetensors={"parameters": {"BF16": count}, "total": count},
                **extra)


def test_sort_fit_orders_by_descending_score_and_still_asks_the_hub_for_downloads(
        client, hub_cache, monkeypatch):
    fake = _reply([
        _fitted("org/tiny", score=100, safetensors_gb=1),
        _fitted("org/huge", score=0, safetensors_gb=4000),
    ])
    monkeypatch.setattr(httpx, "get", fake)
    body = _search(client, {"sort": "fit", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/tiny", "org/huge"]
    url = fake.calls[0][0]
    assert "sort=downloads" in url


def test_a_pulled_in_base_never_outranks_the_variant_that_named_it(
        client, hub_cache, monkeypatch):
    """D859: `_pull_in_family_members` used to reinsert a pulled-in base
    immediately BEFORE the kept variant that named it, regardless of score —
    a placement rule for `HubResults.tsx`'s family grouping, which
    `HubSearchScreen.tsx` (flat rows, no grouping) never honoured. With
    `limit=1` the low-scoring base is cut, then pulled back in by the
    variant's `baseModel` tag (case (c)); the response must still be
    non-increasing in `matchScore`, with the base AFTER the variant, not
    before it.
    """
    _pin_hardware(monkeypatch, ram_gb=32.0)
    fake = _reply([
        _fitted("org/variant", score=100, safetensors_gb=1,
                tags=["base_model:quantized:org/base"]),
        _fitted("org/base", score=0, safetensors_gb=4000),
    ])
    monkeypatch.setattr(httpx, "get", fake)
    body = _search(client, {"sort": "fit", "limit": 1, "includeUnfit": True}).json()
    ids = [m["id"] for m in body["models"]]
    scores = [m["matchScore"] for m in body["models"]]
    assert ids == ["org/variant", "org/base"]
    assert scores == sorted(scores, reverse=True)


def test_sort_trending_reaches_the_wire_as_trendingscore(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"sort": "trending"})
    url = fake.calls[0][0]
    assert "sort=trendingScore" in url


def test_a_verdict_no_row_is_always_shown_now(client, hub_cache, monkeypatch):
    """Item 7 (fix round 5): the "Show models that will not fit" toggle is
    gone — a `verdict: "no"` row is never dropped any more, `includeUnfit`
    (still accepted for an old client) changes nothing, and the per-row fit
    verdict itself (untouched by this change) is the only warning left."""
    fake = _reply([
        _fitted("org/fits", score=100, safetensors_gb=1),
        _fitted("org/toobig", score=0, safetensors_gb=4000),
    ])
    monkeypatch.setattr(httpx, "get", fake)
    default = _search(client).json()
    assert {m["id"] for m in default["models"]} == {"org/fits", "org/toobig"}

    old_client = _search(client, {"includeUnfit": False, "q": "distinct"}).json()
    assert {m["id"] for m in old_client["models"]} == {"org/fits", "org/toobig"}


def test_a_model_already_on_disk_is_never_hidden(
        client, hub_cache, monkeypatch):
    # The stated reason this search exists is the local join — "you already
    # have this one". A `verdict: "no"` row that is downloaded (or
    # mid-download) must never disappear: someone who pulled a 70B repo
    # months ago, or is mid-pull right now, searches its name to check on
    # it, and the page must not say nothing matches. (Item 7, round 5: this
    # is no longer a "default" — every row is always shown, on disk or not.)
    _cached_repo(hub_cache, "models--org--toobig")
    blob = hub_cache / "models--org--partial" / "blobs" / "b1"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x" * 32)
    fake = _reply([
        _fitted("org/toobig", score=0, safetensors_gb=4000),
        _fitted("org/partial", score=0, safetensors_gb=4000),
        _fitted("org/nowhere", score=0, safetensors_gb=4000),
    ])
    monkeypatch.setattr(httpx, "get", fake)
    body = _search(client).json()
    ids = {m["id"] for m in body["models"]}
    assert ids == {"org/toobig", "org/partial", "org/nowhere"}


def test_limit_still_counts_rows_actually_returned_after_the_fit_filter(
        client, hub_cache, monkeypatch):
    rows = [_fitted(f"org/fits{i}", score=100, safetensors_gb=1) for i in range(3)]
    rows += [_fitted("org/toobig", score=0, safetensors_gb=4000)]
    monkeypatch.setattr(httpx, "get", _reply(rows))
    body = _search(client, {"limit": 2}).json()
    assert len(body["models"]) == 2


# -- when the far side is unhappy -------------------------------------------


def test_an_unreachable_hub_is_a_sentence_not_a_500(client, hub_cache, monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "get", boom)
    body = _search(client).json()
    assert body["models"] == []
    assert "huggingface.co" in body["error"]


@pytest.mark.parametrize("status,needle", [
    (403, "token"), (429, "rate-limiting"), (500, "500")])
def test_an_unhappy_hub_explains_itself(client, hub_cache, monkeypatch, status, needle):
    monkeypatch.setattr(httpx, "get", _reply([], status=status))
    body = _search(client).json()
    assert body["models"] == [] and needle in body["error"]


@pytest.mark.parametrize("body", [b"<html>nope</html>", b'{"not": "a list"}'])
def test_an_unexpected_reply_does_not_reach_the_page(client, hub_cache, monkeypatch, body):
    monkeypatch.setattr(httpx, "get", _reply(None, body=body))
    payload = _search(client).json()
    assert payload["models"] == [] and payload["error"]


def test_an_error_is_not_cached(client, hub_cache, monkeypatch):
    # A failed search must not pin the failure for the length of the window: the
    # network comes back, and the next keystroke should find out.
    monkeypatch.setattr(httpx, "get", _reply([], status=500))
    assert _search(client).json()["error"]
    fake = _reply([_hit("org/m")])
    monkeypatch.setattr(httpx, "get", fake)
    assert _search(client).json()["models"][0]["id"] == "org/m"


# -- the filters ------------------------------------------------------------


def test_every_offered_filter_resolves_to_an_explained_label(client):
    """Same drift guard as the local glossary: a filter the page offers must be
    a tag the Hub recognises AND a label this app can explain.

    Reversing the glossary to get the tags is the tempting version and it is
    wrong — "image generation" and "video generation" are readings of a
    diffusers `_class_name`, not tags anyone publishes under, so a filter built
    from one would return nothing at all.
    """
    tasks = client.get("/api/ai-models/hub/tasks").json()["tasks"]
    assert tasks, "no filters offered"
    unexplained = [t["tag"] for t in tasks if not t["help"]]
    assert not unexplained, f"filters with no explanation: {unexplained}"
    # Every tag is the Hub's spelling: lowercase, hyphenated, no spaces.
    assert all(t["tag"] == t["tag"].strip().lower() and " " not in t["tag"] for t in tasks)


def test_a_task_filter_is_passed_through(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"task": "text-generation"})
    assert "filter=text-generation" in fake.calls[0][0]


# -- only what this app can run (D313) ---------------------------------------


def test_the_menu_offers_only_tags_something_here_can_run(client):
    """The user's complaint, as a test: "search functionality of things we
    don't support".

    The menu used to list every tag the Hub recognises — twenty-six of them, of
    which this app can load four — so the control that looked most like the
    point of the feature was mostly a list of ways to get results with no
    working button. (A fifth, video generation, joined since — see the "text
    to video" mapping below.)
    """
    tasks = client.get("/api/ai-models/hub/tasks").json()["tasks"]
    offered = [t["tag"] for t in tasks]
    # Every offered tag resolves to a capability something here serves. Asked of
    # the registry, which is the same authority the Load button uses. This is a
    # claim about the REGISTRY, not about THIS machine — video generation has no
    # "everywhere" row, so the tag is offered (and refuses at Download time with
    # "needs Apple Silicon") on a machine that cannot actually serve it, same as
    # every other capability's menu entry is offered independent of whether ITS
    # runner resolves here.
    for tag in offered:
        assert ai_tasks.classify(tag).supported, tag
    # And the ones that made the complaint are gone, by name — MINUS the two
    # that legitimately joined. `feature-extraction` and `sentence-similarity`
    # were on this list for as long as the embedding capability meant dual
    # encoders only: what wears them is a text encoder, and nothing here could
    # open one, so offering the filter promised a Load button that then refused.
    # Both engines load a prose encoder now (SPEC §40's widening), so the promise
    # is real and the two moved out of this list into the `capability_for_tag`
    # check below. `image-feature-extraction` did NOT move and is added here in
    # their place: an image-only encoder (DINOv2/v3) has no text tower for either
    # load path to read.
    for absent in ("fill-mask", "image-feature-extraction",
                   "text-classification", "summarization", "image-classification"):
        assert absent not in offered
    # …while the five the Engines tab is about are all reachable. EMBEDDINGS
    # arrives through THREE tags, which is unique to it (see
    # `registry.EMBEDDINGS`'s docstring): `zero-shot-image-classification` for a
    # dual encoder, `feature-extraction` and `sentence-similarity` for a prose
    # one. VIDEO_GENERATION arrives through "text-to-video", the tag `ltx-video`
    # serves.
    assert {"zero-shot-image-classification", "feature-extraction",
            "sentence-similarity"} <= set(offered)
    assert {ai_tasks.capability_for_tag(t) for t in offered} == {
        registry.TEXT_GENERATION, registry.IMAGE_GENERATION, registry.SPEECH_TO_TEXT,
        registry.EMBEDDINGS, registry.VIDEO_GENERATION}


def test_the_menu_follows_the_vocabulary_rather_than_a_second_list(client, monkeypatch):
    """A runner appearing or disappearing must move this menu on its own.

    The tags and the answer now live in ONE table (`ai/tasks.py`): a row gains a
    `capability` and the filter appears, loses it and the filter goes. This
    module keeps no list of its own — it used to, and the copy had already gone
    stale (it still offered `text2text-generation`, a tag the Hub retired).
    """
    monkeypatch.setattr(
        ai_tasks, "supported_tags", lambda: ("summarization",))
    assert [t["tag"] for t in client.get("/api/ai-models/hub/tasks").json()["tasks"]] == [
        "summarization"]


def test_a_result_is_never_something_this_app_cannot_run(client, hub_cache, monkeypatch):
    """The hard guarantee. The menu constrains what a user can ASK for; this
    constrains what comes back, including for an unfiltered query where the Hub
    is free to answer with anything it likes."""
    monkeypatch.setattr(httpx, "get", _reply([
        {"id": "org/chat", "pipeline_tag": "text-generation"},
        {"id": "org/vlm", "pipeline_tag": "image-text-to-text"},
        {"id": "org/pic", "pipeline_tag": "text-to-image"},
        {"id": "org/ears", "pipeline_tag": "automatic-speech-recognition"},
        # A prose embedding model, which this app now genuinely loads — this row
        # used to be in the rejected group below and moved up with SPEC §40's
        # widening of the embeddings capability.
        {"id": "sentence-transformers/all-MiniLM-L6-v2", "pipeline_tag": "feature-extraction"},
        # …and the ones the user was looking at when they complained, which are
        # still nothing this app can run.
        {"id": "google-bert/bert-base-uncased", "pipeline_tag": "fill-mask"},
        {"id": "cross-encoder/ms-marco", "pipeline_tag": "text-classification"},
        # An image-ONLY encoder: the neighbour of `feature-extraction` that did
        # NOT become loadable, because it has no text tower for either load path
        # to read.
        {"id": "facebook/dinov2-base", "pipeline_tag": "image-feature-extraction"},
    ]))
    models = _search(client).json()["models"]
    assert [m["id"] for m in models] == [
        "org/chat", "org/vlm", "org/pic", "org/ears",
        "sentence-transformers/all-MiniLM-L6-v2"]


def test_a_result_with_no_pipeline_tag_is_dropped(client, hub_cache, monkeypatch):
    # We cannot promise a Download button for a repo we cannot classify, and
    # "probably a text model" is exactly the guess that hands someone a
    # diffusion checkpoint to load as a chat model.
    monkeypatch.setattr(httpx, "get", _reply([
        {"id": "org/mystery"}, {"id": "org/mystery2", "pipeline_tag": None},
        _hit("org/known")]))
    assert [m["id"] for m in _search(client).json()["models"]] == ["org/known"]


def test_every_result_carries_the_capability_that_would_load_it(client, hub_cache, monkeypatch):
    # It is what the page hands to the download route, so a null here is a
    # button that cannot be wired rather than a missing nicety.
    monkeypatch.setattr(httpx, "get", _reply([
        {"id": "org/chat", "pipeline_tag": "text-generation"},
        {"id": "org/vlm", "pipeline_tag": "image-text-to-text"},
        {"id": "org/pic", "pipeline_tag": "text-to-image"},
        {"id": "org/ears", "pipeline_tag": "automatic-speech-recognition"},
    ]))
    got = {m["id"]: m["capability"] for m in _search(client).json()["models"]}
    assert got == {
        "org/chat": registry.TEXT_GENERATION,
        # A vision-language checkpoint is a text model when you only give it
        # text — the same rule the Local tab's Load button follows.
        "org/vlm": registry.TEXT_GENERATION,
        "org/pic": registry.IMAGE_GENERATION,
        "org/ears": registry.SPEECH_TO_TEXT,
    }


def test_a_private_repo_is_dropped(client, hub_cache, monkeypatch):
    """Private stays out, and it is a different case from gated (D316).

    A private repo is visible here only because this machine happens to hold a
    token that can see it, and there is no step an ordinary account can take to
    reach it — no licence to accept, no queue to join. A card for one could
    never be actioned by the person reading it.
    """
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/blocked", private=True), _hit("org/open")]))
    assert [m["id"] for m in _search(client).json()["models"]] == ["org/open"]


@pytest.mark.parametrize("value,expected", [("auto", "auto"), ("manual", "manual"),
                                            (True, "manual")])
def test_a_gated_repo_survives_and_says_which_kind_of_gate(client, hub_cache, monkeypatch,
                                                           value, expected):
    """Gated repos come back, carrying the gate (D316).

    They were dropped on the rule that every card must be downloadable now, and
    that rule was drawn one step too tight: a gate you open by signing in and
    accepting a licence is not the same as a repo nobody can have. Some of the
    best-known models on the Hub are `auto`-gated, and a search that silently
    omits them is answering a question the user did not ask.

    `manual` is the distinct case the Hub does tell us about — access is granted
    by the repo's owner, not by a click — so it travels as its own value rather
    than being flattened into "gated". An unrecognised truthy gate is read as
    `manual`: the stricter of the two, because guessing "just sign in" about a
    gate nobody here understands is the guess that wastes someone's time.
    """
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/llama", gated=value)]))
    rows = _search(client).json()["models"]
    assert [r["id"] for r in rows] == ["org/llama"]
    assert rows[0]["gated"] == expected
    # Still a real capability, so the card knows what it would be downloading.
    assert rows[0]["capability"] == registry.TEXT_GENERATION


def test_an_ungated_result_says_so_rather_than_saying_nothing(client, hub_cache, monkeypatch):
    # Null, not absent and not False: the page tests one field for "is there a
    # gate and what kind", and a missing key would make "no gate" and "a Hub
    # that did not tell us" the same answer.
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/open"), _hit("org/also", gated=False)]))
    assert [r["gated"] for r in _search(client).json()["models"]] == [None, None]


def test_a_result_in_a_format_no_runner_reads_is_dropped(client, hub_cache, monkeypatch):
    """The tag is right and the format is unreadable — the case the tag filter
    cannot see.

    `litert-community/FLUX.2-klein-4B-LiteRT` is the repo from the complaint: a
    `text-to-image` model, so `capability_for_task` passes it, published as
    `.tflite` graphs, which nothing in `runners/` imports under any
    circumstances. The card offered a Download button and the load that followed
    could only fail.
    """
    monkeypatch.setattr(httpx, "get", _reply([
        {"id": "litert-community/FLUX.2-klein-4B-LiteRT",
         "pipeline_tag": "text-to-image", "library_name": "litert"},
        # A raw NeMo archive is the same shape of mistake in the audio column,
        # and unloadable outright since D406 withdrew `parakeet-mlx` — no
        # runner here reads a `.nemo` archive or an MLX conversion of one.
        {"id": "nvidia/parakeet-tdt-0.6b-v3",
         "pipeline_tag": "automatic-speech-recognition", "library_name": "nemo"},
        _hit("org/known"),
    ]))
    assert [m["id"] for m in _search(client).json()["models"]] == ["org/known"]


def test_a_result_with_no_library_name_is_kept(client, hub_cache, monkeypatch):
    """An ABSENT library says nothing about the format, so it cannot be read as
    "unsupported" — only a value naming a framework we have no runner for is."""
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/unsaid"), _hit("org/null", library_name=None),
        # Not a string, so not a value either: this must be as harmless as a
        # missing key rather than a 500 in a `.lower()`.
        _hit("org/weird", library_name=17),
    ]))
    assert [m["id"] for m in _search(client).json()["models"]] == [
        "org/unsaid", "org/null", "org/weird"]


@pytest.mark.parametrize("library", [
    # Every one of these is a value a repo something here loads TODAY reports,
    # read off the Hub rather than guessed — which is why this filter is a
    # denylist. An allowlist of the libraries our runners are built on
    # (diffusers, transformers, mlx) would have hidden five of these eight.
    "diffusers",            # black-forest-labs/FLUX.2-klein-4B
    "transformers",         # most text models
    "mlx",                  # mlx-community/whisper-large-v3-turbo, …/Qwen3-8B-4bit
    "mflux",                # Runpod/FLUX.2-klein-4B-mflux-4bit
    "ggml",                 # unsloth/FLUX.2-klein-4B-GGUF, the recipe's transformer
    "gguf",                 # the same repos' other spelling of it
    "ctranslate2",          # Systran/faster-whisper-large-v3
    "diffusion-single-file",  # mlx-community/FLUX.2-Klein-4B-4bit
])
def test_a_library_something_here_loads_is_not_dropped(client, hub_cache, monkeypatch, library):
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/fine", library_name=library)]))
    models = _search(client).json()["models"]
    assert [m["id"] for m in models] == ["org/fine"]
    assert models[0]["library"] == library


def test_asking_for_a_task_nothing_here_runs_is_refused(client, hub_cache, monkeypatch):
    # Not an empty grid: that reads as "the Hub has no summarization models"
    # rather than "this app does not run them", and the Hub is not the one being
    # unhelpful.
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    bad = _search(client, {"task": "fill-mask"})
    assert bad.status_code == 400 and "fill-mask" in bad.json()["error"]
    assert not fake.calls, "an unrunnable task still cost an outbound request"


def test_an_unfiltered_search_asks_for_more_than_it_shows(client, hub_cache, monkeypatch):
    """The filter runs HERE for an unfiltered query, so the request has to
    over-fetch or a search for a common word comes back nearly empty.

    With a task filter AND `includeUnfit`, the Hub has already done the
    constraining and nothing here drops anything more, so asking for more
    would be spending someone's rate limit on rows that are thrown away.
    """
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"q": "small", "limit": 10})
    assert "limit=40" in fake.calls[0][0]

    _search(client, {"q": "small", "task": "text-generation", "limit": 10,
                      "includeUnfit": True})
    assert "limit=10" in fake.calls[1][0]


def test_a_task_filtered_search_no_longer_overfetches_now_unfit_is_never_dropped(
        client, hub_cache, monkeypatch):
    """Item 7 (fix round 5): a `verdict: "no"` row used to be dropped by
    default, which a task filter alone gave the Hub no way to see coming —
    that drop is gone now (every row is always shown), so a single-tag task
    filter with no other Part 3 filter active asks the Hub for exactly
    `limit`, the same small request an explicit `includeUnfit: True` used to
    require opting into."""
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"q": "small", "task": "text-generation", "limit": 10})
    assert "limit=10" in fake.calls[0][0]


def test_limit_still_counts_rows_actually_returned_with_a_task_filter(
        client, hub_cache, monkeypatch):
    rows = [_fitted(f"org/fits{i}", score=100, safetensors_gb=1,
                     pipeline_tag="text-generation") for i in range(3)]
    rows += [_fitted("org/toobig", score=0, safetensors_gb=4000,
                      pipeline_tag="text-generation")]
    monkeypatch.setattr(httpx, "get", _reply(rows))
    body = _search(client, {"task": "text-generation", "limit": 2}).json()
    assert len(body["models"]) == 2


def test_the_page_is_truncated_after_filtering_not_before(client, hub_cache, monkeypatch):
    # `limit` means "rows you will be shown". Truncating the Hub's answer first
    # would make a page of embedding models come back as two results.
    rows = [{"id": f"org/junk{i}", "pipeline_tag": "fill-mask"} for i in range(20)]
    rows += [_hit(f"org/good{i}") for i in range(5)]
    monkeypatch.setattr(httpx, "get", _reply(rows))
    models = _search(client, {"q": "x", "limit": 3}).json()["models"]
    assert [m["id"] for m in models] == ["org/good0", "org/good1", "org/good2"]


def test_the_hub_token_does_not_survive_a_cross_host_redirect():
    """A canary on httpx, because the token's containment depends on it.

    `_fetch` sends the user's Hub token as an `Authorization` header AND follows
    redirects. Those two are only safe together because httpx drops the header
    when a redirect leaves the origin — a behaviour of the library, not of this
    module, and `httpx` is an unpinned dependency here. If a resolver ever picks
    a version without it, the user's credential rides a 302 to whatever host the
    Hub (or an `HF_ENDPOINT` mirror) names, and nothing else in this repo would
    notice. So the behaviour is asserted rather than assumed.

    Driven through a mock transport with the same client settings `_fetch` uses.
    """
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/api/models"})
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        client.get("https://huggingface.co/api/models",
                   headers={"Authorization": "Bearer hf_secret"})

    assert len(seen) == 2, "the redirect was not followed; rewrite this canary"
    assert seen[0].headers.get("Authorization") == "Bearer hf_secret"
    assert "authorization" not in {k.lower() for k in seen[1].headers}, (
        "httpx forwarded the Hub token to another host across a redirect — "
        "pin httpx, or stop following redirects on the authenticated call"
    )


def test_search_is_a_guarded_post_not_a_read(client, hub_cache, monkeypatch):
    """The one read in this app that leaves the machine.

    Every other read is an unguarded GET (WF-5), because D36's protection is the
    browser's: a foreign page can fire the request but cannot read the reply.
    That covers the RESPONSE. Search's cost is in the REQUEST — it calls the Hub
    with the user's token attached — so a blind cross-origin GET could spend
    someone's credential and their rate limit while learning nothing. The route
    therefore takes the shape its effect deserves rather than the rule acquiring
    an exception.
    """
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)

    # No header: refused, and the Hub is never called.
    blind = client.post("/api/ai-models/hub/search", json={})
    assert blind.status_code == 403
    assert not fake.calls, "a guarded search still reached the Hub"

    # The old shape is gone, not merely discouraged.
    assert client.get("/api/ai-models/hub/search").status_code == 405

    # And with the header it works.
    assert _search(client).status_code == 200
    assert fake.calls


def test_the_task_glossary_stays_an_ordinary_read(client):
    """`hub/tasks` is a static list — no network, no token, nothing to spend —
    so it keeps the unguarded GET every other read has (WF-5). The asymmetry is
    the point: what earns the guard is the outbound call, not the router."""
    assert client.get("/api/ai-models/hub/tasks").status_code == 200


# -- the total-size fallback (hub/size) --------------------------------------
#
# A repo with no safetensors metadata — GGUF, mflux, a LoRA — has no size to
# recover from a dtype map, and the search endpoint cannot ask for one: the
# Hub's LIST endpoint refuses `expand[]=usedStorage` outright. The real total
# comes from the per-repo DETAIL endpoint, one round trip each, which is why it
# is its own route the page calls only for the cards it is actually showing.


def _size(client, body=None):
    """One size lookup. Guarded POST for the same reason search is: it leaves
    the machine carrying the user's token."""
    return client.post("/api/ai-models/hub/size", json=body or {},
                       headers={"X-Fused": "1"})


def _detail(payload, status=200, body=None):
    """A stand-in `httpx.get` returning one canned per-repo detail answer — an
    OBJECT, not the list the search endpoint gets."""
    def fake(url, **kwargs):
        fake.calls.append((url, kwargs))
        content = json.dumps(payload).encode() if body is None else body
        return httpx.Response(status, content=content,
                              request=httpx.Request("GET", url))
    fake.calls = []
    return fake


def test_the_total_size_comes_from_the_detail_endpoint(client, monkeypatch):
    # The number the Hub's own model page shows for a repo with no safetensors:
    # everything in it, not just the weights.
    fake = _detail({"id": "Runpod/FLUX.2-klein-4B-mflux-4bit", "usedStorage": 4_619_599_193})
    monkeypatch.setattr(httpx, "get", fake)
    body = _size(client, {"id": "Runpod/FLUX.2-klein-4B-mflux-4bit"}).json()
    assert body == {"id": "Runpod/FLUX.2-klein-4B-mflux-4bit",
                    "usedStorage": 4_619_599_193, "fileSize": None,
                    "fit": None, "speedEstimate": None, "error": None}
    url = fake.calls[0][0]
    assert url == ("https://huggingface.co/api/models/"
                   "Runpod/FLUX.2-klein-4B-mflux-4bit?expand%5B%5D=usedStorage")


def test_a_repo_the_hub_has_no_total_for_reports_none(client, monkeypatch):
    # No guess and no fallback to the dtype map: this route's only job is the
    # total, and a repo the Hub does not measure has none.
    monkeypatch.setattr(httpx, "get", _detail({"id": "org/m"}))
    assert _size(client, {"id": "org/m"}).json() == {
        "id": "org/m", "usedStorage": None, "fileSize": None,
        "fit": None, "speedEstimate": None, "error": None}


@pytest.mark.parametrize("value", ["4619599193", -1, 1.5, True, {}, None])
def test_a_total_that_is_not_a_count_of_bytes_is_no_total(client, monkeypatch, value):
    # A string of digits is not an int, and a negative is not a size. Either
    # would reach the card as a number someone plans a download around.
    monkeypatch.setattr(httpx, "get", _detail({"id": "org/m", "usedStorage": value}))
    assert _size(client, {"id": "org/m"}).json()["usedStorage"] is None


def test_the_id_is_quoted_into_the_path_not_concatenated(client, monkeypatch):
    # `org/name` keeps its slash — it is the path — but nothing else does, so an
    # id carrying a `?` cannot become a second query parameter.
    fake = _detail({})
    monkeypatch.setattr(httpx, "get", fake)
    _size(client, {"id": "org/a b?expand[]=evil"})
    url = fake.calls[0][0]
    assert "/api/models/org/a%20b%3Fexpand%5B%5D%3Devil?" in url


@pytest.mark.parametrize("bad", [
    None, "", "   ", 7, ["org/m"], "nameonly", "org/name/extra", "/name", "org/",
    "org/" + "n" * 300,
])
def test_a_malformed_id_is_refused_before_the_hub_is_asked(client, monkeypatch, bad):
    fake = _detail({})
    monkeypatch.setattr(httpx, "get", fake)
    reply = _size(client, {"id": bad})
    assert reply.status_code == 400 and reply.json()["error"]
    assert not fake.calls, "a malformed id still cost an outbound request"


def test_an_unreachable_hub_is_a_sentence_not_a_500_for_sizes(client, monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "get", boom)
    body = _size(client, {"id": "org/m"}).json()
    assert body["usedStorage"] is None and "huggingface.co" in body["error"]


@pytest.mark.parametrize("status,needle", [
    (403, "token"), (429, "rate-limiting"), (500, "500")])
def test_an_unhappy_hub_explains_itself_for_sizes(client, monkeypatch, status, needle):
    monkeypatch.setattr(httpx, "get", _detail(None, status=status))
    body = _size(client, {"id": "org/m"}).json()
    assert body["usedStorage"] is None and needle in body["error"]


@pytest.mark.parametrize("raw", [b"<html>nope</html>", b'[{"not": "an object"}]'])
def test_an_unexpected_detail_reply_does_not_reach_the_page(client, monkeypatch, raw):
    # The detail endpoint answers with an object. A list is the LIST endpoint's
    # shape, and reading one as a repo would be indexing blindly.
    monkeypatch.setattr(httpx, "get", _detail(None, body=raw))
    body = _size(client, {"id": "org/m"}).json()
    assert body["usedStorage"] is None and body["error"]


def test_the_same_repo_inside_the_window_is_asked_once(client, monkeypatch):
    # One round trip per repo is the cost this route exists to bound; a card
    # that scrolls back into view must not pay it again.
    fake = _detail({"id": "org/m", "usedStorage": 123})
    monkeypatch.setattr(httpx, "get", fake)
    for _ in range(3):
        assert _size(client, {"id": "org/m"}).json()["usedStorage"] == 123
    assert len(fake.calls) == 1
    _size(client, {"id": "org/other"})
    assert len(fake.calls) == 2  # …a different repo is a different question


def test_a_size_error_is_not_cached(client, monkeypatch):
    monkeypatch.setattr(httpx, "get", _detail(None, status=500))
    assert _size(client, {"id": "org/m"}).json()["error"]
    monkeypatch.setattr(httpx, "get", _detail({"id": "org/m", "usedStorage": 9}))
    assert _size(client, {"id": "org/m"}).json()["usedStorage"] == 9


def test_the_size_lookup_does_not_collide_with_a_search_answer(client, hub_cache, monkeypatch):
    # Both caches are the same dict, so the keys have to be told apart or a
    # search would be answered with a size.
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/m")]))
    assert _search(client).json()["models"][0]["id"] == "org/m"
    monkeypatch.setattr(httpx, "get", _detail({"id": "org/m", "usedStorage": 5}))
    assert _size(client, {"id": "org/m"}).json()["usedStorage"] == 5


def test_the_size_lookup_sends_the_token_but_never_returns_it(client, monkeypatch):
    fake = _detail({"id": "org/m", "usedStorage": 5})
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setattr(httpx, "get", fake)
    body = _size(client, {"id": "org/m"}).json()
    assert fake.calls[0][1]["headers"]["Authorization"] == "Bearer hf_secret"
    assert "hf_secret" not in json.dumps(body)


def test_the_size_lookup_is_a_guarded_post(client, monkeypatch):
    # Same reasoning as search: the cost is in the REQUEST, which spends
    # someone's credential and their rate limit on a third party.
    fake = _detail({"id": "org/m", "usedStorage": 5})
    monkeypatch.setattr(httpx, "get", fake)
    blind = client.post("/api/ai-models/hub/size", json={"id": "org/m"})
    assert blind.status_code == 403
    assert not fake.calls, "a guarded size lookup still reached the Hub"
    assert client.get("/api/ai-models/hub/size").status_code == 405
    assert _size(client, {"id": "org/m"}).status_code == 200


# -- Bug chain fix: the GGUF total was the whole repo, not the resolved file -

# A GGUF repo's `usedStorage` (the detail endpoint's own total) counts EVERY
# quantization the author published, not the one file `_model_row` resolved
# for this row. `blobs=true` on the SAME detail endpoint — verified against
# `huggingface_hub`'s own `HfApi.model_info(files_metadata=True)`
# (`hf_api.py`: `if files_metadata: params["blobs"] = True`) — expands
# `siblings` into filename+size, so the one file's own bytes come from the
# same one-round-trip endpoint rather than a repo-wide sum.


def test_a_file_specific_size_comes_from_the_blobs_expansion(client, monkeypatch):
    fake = _detail({"id": "unsloth/x-GGUF", "siblings": [
        {"rfilename": "x-Q4_K_M.gguf", "size": 4_200_000_000},
        {"rfilename": "x-Q8_0.gguf", "size": 8_100_000_000},
    ]})
    monkeypatch.setattr(httpx, "get", fake)
    body = _size(client, {"id": "unsloth/x-GGUF", "file": "x-Q4_K_M.gguf"}).json()
    assert body["fileSize"] == 4_200_000_000
    assert body["usedStorage"] is None
    url = fake.calls[0][0]
    assert url == "https://huggingface.co/api/models/unsloth/x-GGUF?blobs=true"


def test_a_file_not_among_the_siblings_reports_no_size(client, monkeypatch):
    monkeypatch.setattr(httpx, "get", _detail({"id": "org/x", "siblings": [
        {"rfilename": "other.gguf", "size": 10},
    ]}))
    body = _size(client, {"id": "org/x", "file": "missing.gguf"}).json()
    assert body["fileSize"] is None and body["error"] is None


@pytest.mark.parametrize("value", ["10", -1, 1.5, True])
def test_a_file_size_that_is_not_a_count_of_bytes_is_no_size(client, monkeypatch, value):
    monkeypatch.setattr(httpx, "get", _detail({"id": "org/x", "siblings": [
        {"rfilename": "m.gguf", "size": value},
    ]}))
    body = _size(client, {"id": "org/x", "file": "m.gguf"}).json()
    assert body["fileSize"] is None


def test_without_a_file_the_size_lookup_is_the_repo_total_as_before(client, monkeypatch):
    fake = _detail({"id": "org/m", "usedStorage": 123})
    monkeypatch.setattr(httpx, "get", fake)
    body = _size(client, {"id": "org/m"}).json()
    assert body["usedStorage"] == 123 and body["fileSize"] is None


def test_a_file_lookup_does_not_collide_with_the_repo_total_cache(client, monkeypatch):
    # Two different questions about the same repo, and the cache key has to
    # tell them apart or one would silently answer the other.
    monkeypatch.setattr(httpx, "get", _detail({"id": "org/m", "usedStorage": 999}))
    assert _size(client, {"id": "org/m"}).json()["usedStorage"] == 999
    fake = _detail({"id": "org/m", "siblings": [{"rfilename": "m.gguf", "size": 7}]})
    monkeypatch.setattr(httpx, "get", fake)
    body = _size(client, {"id": "org/m", "file": "m.gguf"}).json()
    assert body["fileSize"] == 7 and body["usedStorage"] is None
    assert len(fake.calls) == 1


# -- Bug chain fix: fit/speed ride along the file-specific size lookup ------
#
# `_model_row` cannot judge fit for a GGUF row during SEARCH — there is no
# safetensors dtype map to size it from, and resolving the one Hub call that
# WOULD size it (this same `blobs=true` lookup) per row inside a search reply
# would be exactly the per-row Hub round trip the module's own docstring
# forbids. But the lazy per-repo size lookup already exists and already
# costs one round trip once a card scrolls into view — so the verdict rides
# that same answer rather than asking a second time.


def test_the_size_lookup_computes_fit_and_speed_for_the_resolved_file(
        client, monkeypatch):
    _pin_hardware(monkeypatch)
    fake = _detail({"id": "unsloth/x-GGUF", "siblings": [
        {"rfilename": "x-Q4_K_M.gguf", "size": 4_000_000_000},
    ]})
    monkeypatch.setattr(httpx, "get", fake)
    body = _size(client, {
        "id": "unsloth/x-GGUF", "file": "x-Q4_K_M.gguf",
        "capability": registry.TEXT_GENERATION,
    }).json()
    assert body["fit"]["verdict"] in ("easy", "tight")
    assert body["speedEstimate"]["tokensPerSecond"] > 0


def test_no_fit_or_speed_without_a_capability(client, monkeypatch):
    # `capability` is the caller's own row telling this route what ladder to
    # judge against — a size with no stated capability is not enough to judge.
    fake = _detail({"id": "unsloth/x-GGUF", "siblings": [
        {"rfilename": "x-Q4_K_M.gguf", "size": 4_000_000_000},
    ]})
    monkeypatch.setattr(httpx, "get", fake)
    body = _size(client, {"id": "unsloth/x-GGUF", "file": "x-Q4_K_M.gguf"}).json()
    assert body["fit"] is None and body["speedEstimate"] is None


def test_no_speed_estimate_for_a_non_text_capability(client, monkeypatch):
    _pin_hardware(monkeypatch)
    fake = _detail({"id": "org/x", "siblings": [
        {"rfilename": "x.gguf", "size": 4_000_000_000},
    ]})
    monkeypatch.setattr(httpx, "get", fake)
    body = _size(client, {
        "id": "org/x", "file": "x.gguf", "capability": "text-to-image",
    }).json()
    assert body["fit"] is not None
    assert body["speedEstimate"] is None


def test_no_fit_without_a_resolved_file(client, monkeypatch):
    # The repo-wide total is not a basis for judging fit: it counts every
    # quantization the author published, not the weights a load would read,
    # so it would be MORE likely to be wrong than showing nothing.
    fake = _detail({"id": "org/x", "usedStorage": 900_000_000_000})
    monkeypatch.setattr(httpx, "get", fake)
    body = _size(client, {
        "id": "org/x", "capability": registry.TEXT_GENERATION,
    }).json()
    assert body["fit"] is None and body["speedEstimate"] is None


# -- Part 2: Quant, derived from measured metadata, never guessed from a name


def test_quant_is_the_dominant_measured_dtype(client, hub_cache, monkeypatch):
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/m", safetensors={"parameters": {"BF16": 8_000_000_000}, "total": 8_000_000_000},
    )]))
    row = _search(client).json()["models"][0]
    assert row["quant"] == "BF16"


def test_quant_picks_the_dtype_with_the_most_bytes(client, hub_cache, monkeypatch):
    # A tiny embedding table at a different width must not decide the label.
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/m", safetensors={"parameters": {"F32": 1_000, "F16": 8_000_000_000}},
    )]))
    row = _search(client).json()["models"][0]
    assert row["quant"] == "F16"


def test_quant_is_the_gguf_files_own_token(client, hub_cache, monkeypatch):
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([_hit("unsloth/x-GGUF", siblings=[
        {"rfilename": "x-Q4_K_M.gguf"},
    ])]))
    row = _search(client).json()["models"][0]
    assert row["quant"] == "Q4_K_M"


def test_quant_is_null_when_nothing_measured_it(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/nothing", safetensors=None)]))
    row = _search(client).json()["models"][0]
    assert row["quant"] is None


# -- Code review F2: a packed integer dtype is a storage container, not a ---
# -- quantization; `config`'s own declared bit width is real evidence too ---


def test_quant_does_not_report_a_packed_dtype_as_the_quantization(client, hub_cache, monkeypatch):
    # An MLX/GPTQ 4-bit checkpoint bit-packs weights into U32 words with no
    # `quantization`/`quantization_config` block at all (an ad-hoc format this
    # server does not recognise) — "U32" is a storage container, not evidence
    # of any particular precision, so this must render nothing rather than a
    # wrong label.
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/packed-no-config", safetensors={"parameters": {"U32": 1_000_000}},
    )]))
    row = _search(client).json()["models"][0]
    assert row["quant"] is None


def test_quant_prefers_configs_declared_bit_width_over_the_packed_dtype(client, hub_cache, monkeypatch):
    # `mlx-community/Qwen3.8-27B-4bit`'s own shape, live-verified: BF16 scales
    # beside a U32-packed 4-bit weight matrix, with `config.quantization_config
    # = {"bits": 4}` declaring the real precision.
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/mlx-4bit",
        safetensors={"parameters": {"BF16": 1_303_792_880, "U32": 3_361_669_120}},
        config={"quantization_config": {"bits": 4}},
    )]))
    row = _search(client).json()["models"][0]
    assert row["quant"] == "4-bit"


def test_quant_still_reports_a_float_dtype_with_no_config(client, hub_cache, monkeypatch):
    # Unchanged from before this fix: a plain BF16 checkpoint has no packed
    # container to misreport, so the dtype itself is still real evidence.
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/bf16", safetensors={"parameters": {"BF16": 8_000_000_000}},
    )]))
    row = _search(client).json()["models"][0]
    assert row["quant"] == "BF16"


def test_params_unpacks_a_packed_dtype_using_configs_declared_bit_width(client, hub_cache, monkeypatch):
    # Same live-verified fixture as the quant test above: the Hub's own raw
    # element count (BF16 + U32 summed with neither unpacked) is ~4.7B, which
    # is the bug (a declared-27B model reporting "Under 8B"). Unpacking the
    # U32 count by the declared 4-bit width recovers ~28B, matching the
    # model's own name.
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/mlx-4bit",
        safetensors={
            "parameters": {"BF16": 1_303_792_880, "U32": 3_361_669_120},
            "total": 4_665_462_000,
        },
        config={"quantization_config": {"bits": 4}},
    )]))
    row = _search(client).json()["models"][0]
    assert row["params"] == 1_303_792_880 + 3_361_669_120 * 8
    assert row["params"] > 27_000_000_000


def test_params_band_reclassifies_the_unpacked_model_correctly(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/mlx-27b",
        safetensors={"parameters": {"BF16": 1_303_792_880, "U32": 3_361_669_120}},
        config={"quantization_config": {"bits": 4}},
    )]))
    body = _search(client, {"paramsBand": "over15b", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/mlx-27b"]


def test_params_without_a_declared_bit_width_stays_the_raw_undercount(client, hub_cache, monkeypatch):
    # No `config` at all: there is no honest way to know the packing ratio, so
    # this is unchanged from before the fix — an undercount, not a guess.
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/packed-no-config", safetensors={"parameters": {"U32": 3_361_669_120}},
    )]))
    row = _search(client).json()["models"][0]
    assert row["params"] == 3_361_669_120


# -- Code review F3: a `file`-resolved row's safetensors upload must not ----
# -- describe what the Download button actually fetches ---------------------


def test_a_file_resolved_row_ignores_its_own_safetensors_upload(client, hub_cache, monkeypatch):
    # A repo publishing BOTH GGUF and safetensors, with llama.cpp active (so
    # `file` resolves): quant, size and params must all describe the GGUF file
    # the Download button fetches, never the full-precision safetensors this
    # row is not going to download.
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner())
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/both-formats",
        safetensors={"parameters": {"BF16": 8_000_000_000}, "total": 8_000_000_000},
        siblings=[{"rfilename": "both-formats-Q4_K_M.gguf"}],
    )]))
    row = _search(client).json()["models"][0]
    assert row["quant"] == "Q4_K_M"
    assert row["estimatedSize"] is None
    assert row["params"] is None


# -- Part 3: three filters, all server-side (see hub-search-notes.md) -------


def _stub_verdicts(monkeypatch, verdicts: dict):
    """Bypass real fit maths for the FILTER tests below — the scoring itself
    is covered elsewhere (`test_sort_fit_...`, `test_a_verdict_no_row_...`);
    these tests are only about what `api_hub_search` DOES with a verdict once
    it has one."""
    def fake_verdict(capability, model_id, size_gb=None, resident_gb=None, **kw):
        return verdicts.get(model_id)
    monkeypatch.setattr(hub.fit, "verdict", fake_verdict)


def test_fit_level_easy_shows_only_easy_verdicts(client, hub_cache, monkeypatch):
    _stub_verdicts(monkeypatch, {
        "org/easy": {"verdict": "easy", "basis": "declared", "footprintBytes": 1, "score": 100.0},
        "org/tight": {"verdict": "tight", "basis": "declared", "footprintBytes": 1, "score": 50.0},
    })
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/easy"), _hit("org/tight")]))
    body = _search(client, {"fitLevel": "easy"}).json()
    assert [m["id"] for m in body["models"]] == ["org/easy"]


def test_fit_level_tight_allows_easy_and_tight_but_not_no(client, hub_cache, monkeypatch):
    _stub_verdicts(monkeypatch, {
        "org/easy": {"verdict": "easy", "basis": "declared", "footprintBytes": 1, "score": 100.0},
        "org/tight": {"verdict": "tight", "basis": "declared", "footprintBytes": 1, "score": 50.0},
        "org/no": {"verdict": "no", "basis": "declared", "footprintBytes": 1, "score": 0.0},
    })
    monkeypatch.setattr(httpx, "get", _reply(
        [_hit("org/easy"), _hit("org/tight"), _hit("org/no")]))
    body = _search(client, {"fitLevel": "tight", "includeUnfit": True}).json()
    assert {m["id"] for m in body["models"]} == {"org/easy", "org/tight"}


def test_fit_level_any_leaves_the_unfit_default_untouched(client, hub_cache, monkeypatch):
    _stub_verdicts(monkeypatch, {
        "org/no": {"verdict": "no", "basis": "declared", "footprintBytes": 1, "score": 0.0},
    })
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/no")]))
    body = _search(client, {"fitLevel": "any", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/no"]


def test_fit_level_easy_still_shows_an_unknown_verdict_row(client, hub_cache, monkeypatch):
    # D806: an unmeasured row (no verdict at all) is not the same claim as
    # "will not fit" and must pass the Fit filter — dropped before, alongside
    # the `no` rows it does not resemble.
    _stub_verdicts(monkeypatch, {
        "org/easy": {"verdict": "easy", "basis": "declared", "footprintBytes": 1, "score": 100.0},
        "org/no": {"verdict": "no", "basis": "declared", "footprintBytes": 1, "score": 0.0},
    })
    monkeypatch.setattr(httpx, "get", _reply(
        [_hit("org/easy"), _hit("org/no"), _hit("org/unmeasured")]))
    body = _search(client, {"fitLevel": "easy", "includeUnfit": True}).json()
    assert {m["id"] for m in body["models"]} == {"org/easy", "org/unmeasured"}


def test_fit_level_tight_still_shows_an_unknown_verdict_row(client, hub_cache, monkeypatch):
    _stub_verdicts(monkeypatch, {
        "org/no": {"verdict": "no", "basis": "declared", "footprintBytes": 1, "score": 0.0},
    })
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/no"), _hit("org/unmeasured")]))
    body = _search(client, {"fitLevel": "tight", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/unmeasured"]


def test_an_unknown_fit_level_is_refused(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([]))
    assert _search(client, {"fitLevel": "bogus"}).status_code == 400


def test_quant_filter_narrows_to_the_matching_dtype(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/bf16", safetensors={"parameters": {"BF16": 1_000_000}}),
        _hit("org/f16", safetensors={"parameters": {"F16": 1_000_000}}),
    ]))
    body = _search(client, {"quant": "F16", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/f16"]


def test_quant_filter_is_case_insensitive(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/bf16", safetensors={"parameters": {"BF16": 1_000_000}}),
    ]))
    body = _search(client, {"quant": "bf16", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/bf16"]


def test_quant_filter_drops_rows_with_no_measured_quant(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/bf16", safetensors={"parameters": {"BF16": 1_000_000}}),
        _hit("org/none", safetensors=None),
    ]))
    body = _search(client, {"quant": "BF16", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/bf16"]


def test_params_band_under_4b(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/small", safetensors={"parameters": {"BF16": 1_000_000_000}}),
        _hit("org/big", safetensors={"parameters": {"BF16": 8_000_000_000}}),
    ]))
    body = _search(client, {"paramsBand": "under4b", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/small"]


def test_params_band_4_to_15b_is_inclusive_at_the_edges(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/edge-low", safetensors={"parameters": {"BF16": 4_000_000_000}}),
        _hit("org/edge-high", safetensors={"parameters": {"BF16": 15_000_000_000}}),
        _hit("org/over", safetensors={"parameters": {"BF16": 16_000_000_000}}),
    ]))
    body = _search(client, {"paramsBand": "4to15b", "includeUnfit": True}).json()
    assert {m["id"] for m in body["models"]} == {"org/edge-low", "org/edge-high"}


def test_params_band_over_15b(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/small", safetensors={"parameters": {"BF16": 1_000_000_000}}),
        _hit("org/huge", safetensors={"parameters": {"BF16": 70_000_000_000}}),
    ]))
    body = _search(client, {"paramsBand": "over15b", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/huge"]


def test_params_band_drops_rows_with_no_params(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/known", safetensors={"parameters": {"BF16": 1_000_000_000}}),
        _hit("org/unknown", safetensors=None),
    ]))
    body = _search(client, {"paramsBand": "under4b", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/known"]


def test_an_unknown_params_band_is_refused(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([]))
    assert _search(client, {"paramsBand": "bogus"}).status_code == 400


# -- Code review F6: `publisher` and `quant` are capped like their neighbours


def test_publisher_is_capped_before_it_reaches_the_outbound_hub_url(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"publisher": "x" * 10_000})
    url = fake.calls[0][0]
    author = parse_qs(urlsplit(url).query)["author"][0]
    assert len(author) == hub._MAX_ID_LEN


def test_quant_filter_is_capped(client, hub_cache, monkeypatch):
    # A real quant token is short (`Q4_K_M`, `4-bit`, a dtype name); an
    # uncapped value here was never functional, only unbounded. Confirmed via
    # `_params_band`-style behavioural check: a capped filter that no longer
    # matches any real quant string drops every row rather than erroring.
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/bf16", safetensors={"parameters": {"BF16": 1_000_000}}),
    ]))
    body = _search(client, {"quant": "BF16" + "x" * 10_000, "includeUnfit": True}).json()
    assert body["models"] == []


def test_publisher_is_sent_to_the_hub_as_author(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"publisher": "mlx-community"})
    url = fake.calls[0][0]
    assert "author=mlx-community" in url


def test_publisher_joins_the_cache_key(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"publisher": "mlx-community"})
    _search(client, {"publisher": "unsloth"})
    # D853 (fix round 6, item 5): a `publisher`-scoped search now makes a
    # SECOND Hub request with `author` dropped, to compute the publisher/quant
    # facets over the unscoped candidate set (so the dropdown does not
    # collapse to the one publisher already picked) — cached under a
    # `publisher=None` key, so the second search's own facet fetch is a cache
    # hit off the first's. Two main (author-scoped) fetches + one shared
    # facet fetch = 3, not the 2 a publisher-only cache key would predict.
    assert len(fake.calls) == 3


def test_search_reports_publisher_and_quant_facets(client, hub_cache, monkeypatch):
    """D853 (fix round 6, item 5): `facets.publishers`/`facets.quants` — repo
    id before the `/`, and the row's own measured `quant` — sorted by count
    desc then name, so the frontend's new dropdown menus have something to
    list.

    Fix round 11, item 2: text-generation searches (the default, no
    `capability` sent) now pin this machine's go-to runner publishers to the
    front of `facets.publishers` (`_pin_publisher_facets`) — `is_apple_
    silicon` pinned False here (matches `_pin_hardware`'s CPU-only fixture)
    puts `bartowski`/`unsloth`/`lmstudio-community` first, `unsloth` folded
    into that pinned block at its real count (1) rather than listed twice."""
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(hub.fit, "is_apple_silicon", lambda: False)
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("Qwen/a", safetensors={"parameters": {"BF16": 1_000_000}}),
        _hit("Qwen/b", safetensors={"parameters": {"BF16": 1_000_000}}),
        _hit("Qwen/c", safetensors={"parameters": {"F16": 1_000_000}}),
        _hit("unsloth/d", safetensors={"parameters": {"F16": 1_000_000}}),
    ]))
    body = _search(client, {"includeUnfit": True}).json()
    assert body["facets"]["publishers"] == [
        {"id": "bartowski", "count": 0},
        {"id": "unsloth", "count": 1},
        {"id": "lmstudio-community", "count": 0},
        {"id": "Qwen", "count": 3},
    ]
    assert body["facets"]["quants"] == [
        {"id": "BF16", "count": 2}, {"id": "F16", "count": 2}]


def test_quant_facets_do_not_collapse_once_a_quant_is_picked(client, hub_cache, monkeypatch):
    """The quant filter narrows `models` AFTER facets are computed — picking
    `BF16` must not make the dropdown's own option list shrink to just
    `BF16`, or a reader could never get back to `F16`."""
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/a", safetensors={"parameters": {"BF16": 1_000_000}}),
        _hit("org/b", safetensors={"parameters": {"F16": 1_000_000}}),
    ]))
    body = _search(client, {"quant": "BF16", "includeUnfit": True}).json()
    ids = {m["id"] for m in body["models"]}
    assert ids == {"org/a"}
    assert {q["id"] for q in body["facets"]["quants"]} == {"BF16", "F16"}


def test_publisher_facets_do_not_collapse_once_a_publisher_is_picked(
        client, hub_cache, monkeypatch):
    """Publisher narrows the WIRE request itself (`author`), so without the
    unscoped second fetch the facet list would only ever see the one
    publisher already picked — this pins that the full candidate set's
    publishers still show up.

    `is_apple_silicon` pinned True (fix round 11, item 2): on a Metal
    machine `lmstudio-community` is also pinned into the front of the list
    alongside the real `mlx-community`/`unsloth` rows, so the expected set
    below includes it."""
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(hub.fit, "is_apple_silicon", lambda: True)

    def fake(url, **kwargs):
        fake.calls.append(url)
        q = parse_qs(urlsplit(url).query)
        if "author" in q:
            rows = [_hit("mlx-community/a", safetensors={"parameters": {"BF16": 1_000_000}})]
        else:
            rows = [
                _hit("mlx-community/a", safetensors={"parameters": {"BF16": 1_000_000}}),
                _hit("unsloth/b", safetensors={"parameters": {"F16": 1_000_000}}),
            ]
        return httpx.Response(200, content=json.dumps(rows).encode(),
                              request=httpx.Request("GET", url))
    fake.calls = []
    monkeypatch.setattr(httpx, "get", fake)
    body = _search(client, {"publisher": "mlx-community", "includeUnfit": True}).json()
    assert {m["id"] for m in body["models"]} == {"mlx-community/a"}
    assert {p["id"] for p in body["facets"]["publishers"]} == {
        "mlx-community", "unsloth", "lmstudio-community"}


def test_metal_machines_get_mlx_community_pinned_into_publisher_facets(
        client, hub_cache, monkeypatch):
    """Fix round 11, item 2: `mlx-community` has exactly one row in the
    ~200 most-downloaded window `_facets` counts over in real life, so it
    sorts alphabetically behind the top-40 cutoff and never appears in the
    dropdown — even though a search for it by name returns a full page.
    On a Metal-bucket machine it is pinned to the FRONT of
    `facets.publishers` with `count: 0` when this fetch's rows have none."""
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(hub.fit, "is_apple_silicon", lambda: True)
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/a", safetensors={"parameters": {"BF16": 1_000_000}}),
    ]))
    body = _search(client, {"includeUnfit": True}).json()
    publishers = body["facets"]["publishers"]
    assert publishers[0] == {"id": "mlx-community", "count": 0}
    assert publishers[1]["id"] == "lmstudio-community"


def test_non_metal_machines_do_not_pin_mlx_community(client, hub_cache, monkeypatch):
    """Off Metal, the pinned set is the bartowski/unsloth/lmstudio-community
    trio — `mlx-community` is absent from the facet list unless one of this
    fetch's own rows actually named it."""
    _pin_hardware(monkeypatch)
    monkeypatch.setattr(hub.fit, "is_apple_silicon", lambda: False)
    monkeypatch.setattr(httpx, "get", _reply([
        _hit("org/a", safetensors={"parameters": {"BF16": 1_000_000}}),
    ]))
    body = _search(client, {"includeUnfit": True}).json()
    ids = [p["id"] for p in body["facets"]["publishers"]]
    assert "mlx-community" not in ids
    assert ids[0] == "bartowski"
    assert ids[1] == "unsloth"
    assert ids[2] == "lmstudio-community"


def test_no_publisher_means_no_author_param(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client)
    url = fake.calls[0][0]
    assert "author=" not in url


def test_a_narrow_quant_filter_still_overfetches_so_limit_is_not_underfilled(
        client, hub_cache, monkeypatch):
    # Mirrors the identical fix already made for `includeUnfit`/task filters
    # (see `test_unchecking_include_unfit_inside_the_window_does_not_reuse_the_smaller_fetch`):
    # a filter that only removes rows AFTER the Hub's own answer must not be
    # satisfied from the same small `count`-sized fetch a plain query would
    # use, or the page comes back under-filled with headroom left unused on
    # the Hub's own answer.
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"quant": "BF16", "limit": 24})
    url = fake.calls[0][0]
    assert "limit=96" in url  # count(24) * _OVERFETCH(4)


def test_toggling_a_narrow_filter_inside_the_window_does_not_reuse_the_smaller_fetch(
        client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"task": "text-generation", "includeUnfit": True, "limit": 24})
    first_limit = parse_qs(urlsplit(fake.calls[0][0]).query)["limit"][0]
    assert first_limit == "24"
    _search(client, {"task": "text-generation", "includeUnfit": True, "limit": 24,
                     "paramsBand": "under4b"})
    second_limit = parse_qs(urlsplit(fake.calls[1][0]).query)["limit"][0]
    assert second_limit == "96"


# -- D780: the composite "Best match" ranking --------------------------------


def test_matchscore_is_present_on_every_row_regardless_of_sort(client, hub_cache, monkeypatch):
    _pin_hardware(monkeypatch)
    fake = _reply([_fitted("org/m", score=100, safetensors_gb=1)])
    monkeypatch.setattr(httpx, "get", fake)
    body = _search(client, {"sort": "downloads"}).json()
    score = body["models"][0]["matchScore"]
    assert isinstance(score, (int, float))
    assert 0.0 <= score <= 100.0


def test_default_sort_is_best_not_downloads(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([]))
    body = _search(client).json()
    assert body["query"]["sort"] == "best"


def test_sort_best_still_asks_the_hub_for_downloads(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"sort": "best"})
    assert "sort=downloads" in fake.calls[0][0]


def test_an_unknown_sort_best_typo_is_still_refused(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([]))
    bad = _search(client, {"sort": "bset"})
    assert bad.status_code == 400


def test_sort_best_ranks_by_the_composite_not_by_downloads_order(client, hub_cache, monkeypatch):
    # A tiny, 5-year-old, hugely-downloaded stub vs. a capable, brand-new
    # model with far fewer downloads — the exact inversion D780 exists to
    # produce (a live repro on this project's own screenshot: a 2M-param
    # test stub outranking real, current models by raw downloads alone).
    _pin_hardware(monkeypatch)
    tiny = _hit("org/tiny-stub",
               safetensors={"parameters": {"F32": 500_000}, "total": 500_000},
               downloads=20_000_000, createdAt="2021-01-01T00:00:00.000Z")
    capable = _fitted("org/capable", score=100, safetensors_gb=8,
                      downloads=1_000_000, createdAt="2026-08-01T00:00:00.000Z")
    fake = _reply([tiny, capable])
    monkeypatch.setattr(httpx, "get", fake)
    body = _search(client, {"sort": "best", "includeUnfit": True}).json()
    assert [m["id"] for m in body["models"]] == ["org/capable", "org/tiny-stub"]


def test_capability_axis_rewards_more_params_with_diminishing_returns():
    lo = hub._capability_score(100_000_000, ram_gb=32.0)
    hi = hub._capability_score(8_000_000_000, ram_gb=32.0)
    huge = hub._capability_score(64_000_000_000, ram_gb=32.0)
    assert lo < hi < huge <= 100.0


def test_capability_axis_defaults_honestly_for_unknown_params():
    assert hub._capability_score(None, ram_gb=32.0) == hub._CAPABILITY_DEFAULT
    assert hub._capability_score(0, ram_gb=32.0) == hub._CAPABILITY_DEFAULT


def test_speed_axis_saturates_so_an_anchor_less_estimate_cannot_win_outright():
    # `speed.py:283`'s own documented gap: a sub-billion-parameter model's
    # tok/s is not modelled at all, and the axis must not let that show up
    # as a ranking advantage over a genuinely fast, correctly-modelled one.
    # `params` is above `_SPEED_ANCHOR_PARAMS` for both rows here — this
    # test is about the SATURATING CURVE, not the anchor gate (see the two
    # tests below for that).
    already_fast = hub._speed_score({"tokensPerSecond": 40}, 7_000_000_000)
    inflated = hub._speed_score({"tokensPerSecond": 17_324.6}, 7_000_000_000)
    assert inflated <= 100.0
    assert inflated - already_fast < 5


def test_speed_axis_defaults_honestly_for_no_estimate():
    assert hub._speed_score(None, 7_000_000_000) == hub._SPEED_DEFAULT
    assert hub._speed_score({}, 7_000_000_000) == hub._SPEED_DEFAULT


def test_speed_axis_defaults_below_the_anchor_even_with_a_real_estimate():
    # Code review finding 6: the display (`speedLabel`/`speedTitle` in
    # `hubTableView.ts`) already refuses to print a tok/s figure below
    # `_SPEED_ANCHOR_PARAMS` ("a number here would not be a real estimate")
    # — the ranking axis must refuse to SCORE on it too, one source of
    # truth for "this estimate isn't real". Before this fix,
    # `_saturating(17_324.6, 12)` scored the axis's own CEILING (100.0) for
    # a sub-billion-parameter stub, ABOVE a genuinely fast, correctly-
    # modelled model's real score.
    tiny_params = 2_000_000  # a 2M-parameter CI stub, same shape as the
    # `tiny-Qwen2ForCausalLM-2.5` example D780 itself cites.
    inflated_but_tiny = hub._speed_score({"tokensPerSecond": 17_324.6}, tiny_params)
    assert inflated_but_tiny == hub._SPEED_DEFAULT
    real_fast_model = hub._speed_score({"tokensPerSecond": 40}, 7_000_000_000)
    assert inflated_but_tiny < real_fast_model


def test_speed_default_is_at_or_below_the_conversational_anchor():
    # Code review finding 7: `_SPEED_DEFAULT` used to correspond to ~14.4
    # tok/s, ABOVE `_SPEED_CONVERSATIONAL_TOK_S` (12) itself, so an
    # unmeasured row outranked a real, measured, plainly-usable slow model
    # (a real 8 tok/s model scored 48 on this axis, well under the old
    # default of 70). The default must never beat what a genuinely-measured
    # row AT the anchor itself scores.
    at_anchor = hub._saturating(hub._SPEED_CONVERSATIONAL_TOK_S, hub._SPEED_CONVERSATIONAL_TOK_S)
    assert hub._SPEED_DEFAULT <= at_anchor + 0.5


def test_recency_axis_prefers_newer_and_floors_a_future_date_at_zero_age():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    # A second in the past, not exactly "now" — two separate `datetime.now()`
    # reads (this test's and `_recency_score`'s own) a few microseconds apart
    # would otherwise make "brand new" score a hair under the ceiling by pure
    # timing, an irrelevant flake this margin avoids.
    recent = hub._recency_score((now - timedelta(seconds=1)).isoformat())
    four_years_old = hub._recency_score((now - timedelta(days=365 * 4)).isoformat())
    a_future_date = hub._recency_score((now + timedelta(days=30)).isoformat())
    assert four_years_old < recent < 100.0
    # A "future" timestamp (clock skew) is floored at age zero, i.e. AT the
    # ceiling — never scored ABOVE it, which is the property under test.
    assert a_future_date == pytest.approx(100.0)


def test_recency_axis_defaults_honestly_for_no_createdat():
    assert hub._recency_score(None) == hub._RECENCY_DEFAULT
    assert hub._recency_score("not a date") == hub._RECENCY_DEFAULT


def test_popularity_axis_is_weak_log_scaled_and_capped():
    low = hub._popularity_score(100_000)
    high = hub._popularity_score(23_000_000)
    assert 0.0 < low < high <= 100.0


def test_popularity_axis_default_is_zero_not_a_middle_value():
    # The one axis whose "nothing known" default is 0, not a mid-range
    # value like every other axis (D780): popularity measures nothing but
    # downloads, so no count at all is genuinely the worst case for it.
    assert hub._popularity_score(None) == 0.0
    assert hub._popularity_score(0) == 0.0


def test_composite_score_gives_a_small_bump_for_a_row_already_on_disk():
    absent = {"fit": {"score": 100}, "params": None, "speedEstimate": None,
             "created": None, "downloads": None, "local": {"state": "none"}}
    have = dict(absent, local={"state": "downloaded"})
    assert hub._composite_score(have, 32.0) - hub._composite_score(absent, 32.0) == \
        pytest.approx(hub._ON_DISK_BONUS)


def test_composite_score_penalizes_cpu_offload_and_cpu_only():
    base = {"params": None, "speedEstimate": None, "created": None, "downloads": None,
            "local": {"state": "none"}}
    gpu = dict(base, fit={"score": 80, "runMode": "gpu"})
    offload = dict(base, fit={"score": 80, "runMode": "cpu-offload"})
    cpu_only = dict(base, fit={"score": 80, "runMode": "cpu-only"})
    s_gpu = hub._composite_score(gpu, 32.0)
    s_offload = hub._composite_score(offload, 32.0)
    s_cpu_only = hub._composite_score(cpu_only, 32.0)
    assert s_offload == pytest.approx(s_gpu - hub._CPU_OFFLOAD_PENALTY)
    assert s_cpu_only == pytest.approx(s_gpu - hub._CPU_ONLY_PENALTY)
    assert s_cpu_only < s_offload < s_gpu


def test_composite_score_stays_within_0_100_even_at_the_ceiling():
    row = {"fit": {"score": 100, "runMode": "gpu"}, "params": 8_000_000_000,
           "speedEstimate": {"tokensPerSecond": 200},
           "created": "2026-09-01T00:00:00.000Z",
           "downloads": 50_000_000, "local": {"state": "downloaded"}}
    assert hub._composite_score(row, 32.0) <= 100.0


def test_composite_score_degrades_honestly_with_nothing_known_at_all():
    # Never 0 (reads as "definitely bad") and never 100 (reads as
    # "definitely good") for a row with no evidence on any axis.
    row = {"fit": None, "params": None, "speedEstimate": None, "created": None,
           "downloads": None, "local": {"state": "none"}}
    score = hub._composite_score(row, 32.0)
    assert 0.0 < score < 100.0


def test_raw_score_is_unclamped_where_the_displayed_score_is_clamped():
    # Code review finding 8: `_composite_score` (displayed) clamps to
    # [0, 100]; `_composite_raw_score` (the SORT key) must not, or a
    # GPU-less machine's whole tail (identical `_CPU_ONLY_PENALTY` moves no
    # row relative to another, but the clamp afterward flattens every one
    # whose blend was already under 20 to exactly 0.0) loses its ordering.
    low = {"fit": {"score": 5, "runMode": "cpu-only"}, "params": 1_000,
           "speedEstimate": None, "created": "2015-01-01T00:00:00.000Z",
           "downloads": 0, "local": {"state": "none"}}
    from datetime import datetime, timezone
    high_ceiling = {"fit": {"score": 100, "runMode": "gpu"}, "params": 800_000_000_000,
                    "speedEstimate": {"tokensPerSecond": 5000},
                    "created": datetime.now(timezone.utc).isoformat(),
                    "downloads": 50_000_000, "local": {"state": "downloaded"}}
    assert hub._composite_raw_score(low, 32.0) < 0.0
    assert hub._composite_raw_score(high_ceiling, 32.0) > 100.0
    # The displayed number stays clamped either way.
    assert hub._composite_score(low, 32.0) == 0.0
    assert hub._composite_score(high_ceiling, 32.0) == 100.0


def test_a_gpu_less_machines_tail_keeps_a_strict_ordering():
    """A GPU-less machine gives every row the identical `_CPU_ONLY_PENALTY`
    (`runMode` is a property of the MACHINE, not the row) — it must not
    change one row's rank relative to another's. Three rows here differ
    only in fit score; capability, speed, recency and popularity are all
    real-but-weak evidence (a tiny, old, unpopular repo), so every blend
    lands well under `_CPU_ONLY_PENALTY` — before the fix, all three
    clamped to the SAME displayed 0.0, and sorting on that clamped number
    would have tied them, falling back to whatever order the Hub happened
    to send them in. `_composite_raw_score` must keep them apart."""
    def row(fit_score):
        return {"fit": {"score": fit_score, "runMode": "cpu-only"},
                "params": 1_000,  # real, but far below the capability anchor
                "speedEstimate": None, "created": "2015-01-01T00:00:00.000Z",
                "downloads": 0, "local": {"state": "none"}}

    high, mid, low = row(20), row(10), row(0)
    raw = [hub._composite_raw_score(r, 32.0) for r in (high, mid, low)]
    assert raw[0] > raw[1] > raw[2]
    assert raw[2] < 0.0  # negative — a fact the clamp then hides
    # The reported symptom: the DISPLAYED number genuinely ties at the
    # clamp floor even though the raw blends plainly do not.
    displayed = [hub._composite_score(r, 32.0) for r in (high, mid, low)]
    assert displayed[0] == displayed[1] == displayed[2] == 0.0


# ---- D1245/D1246: per-axis breakdown for the row-level tooltip -----------


def test_score_breakdown_gained_plus_lost_accounts_for_every_axis_weight():
    # Each of the five weighted axes must reconcile: `gained` (blended points
    # earned) plus `lost` (blended points short of a perfect 100 on that
    # axis) always equals that axis's own full weight in blended points —
    # nothing invented, nothing dropped.
    row = {"fit": {"score": 62, "runMode": "gpu"}, "params": 3_000_000_000,
           "speedEstimate": {"tokensPerSecond": 9}, "created": "2024-01-01T00:00:00Z",
           "downloads": 1200, "local": {"state": "none"}}
    entries = hub._score_breakdown(row, 32.0)
    by_axis = {e["axis"]: e for e in entries}
    weights = {"fit": hub._WEIGHT_FIT, "capability": hub._WEIGHT_CAPABILITY,
               "speed": hub._WEIGHT_SPEED, "recency": hub._WEIGHT_RECENCY,
               "popularity": hub._WEIGHT_POPULARITY}
    for axis, weight in weights.items():
        entry = by_axis[axis]
        assert entry["gained"] + entry["lost"] == pytest.approx(weight * 100.0, abs=0.15)


def test_score_breakdown_axis_gains_sum_to_the_raw_score_with_bonus_and_penalty():
    have_offload = {"fit": {"score": 90, "runMode": "cpu-offload"}, "params": 7_000_000_000,
                     "speedEstimate": {"tokensPerSecond": 20}, "created": "2026-01-01T00:00:00Z",
                     "downloads": 900_000, "local": {"state": "downloaded"}}
    entries = hub._score_breakdown(have_offload, 32.0)
    total = sum(e["gained"] for e in entries) - sum(
        e["lost"] for e in entries if e["axis"] == "runMode")
    assert total == pytest.approx(hub._composite_raw_score(have_offload, 32.0), abs=0.15)


def test_score_breakdown_perfect_axis_loses_nothing():
    row = {"fit": {"score": 100, "runMode": "gpu"}, "params": None,
           "speedEstimate": None, "created": None, "downloads": None,
           "local": {"state": "none"}}
    entries = hub._score_breakdown(row, 32.0)
    fit_entry = next(e for e in entries if e["axis"] == "fit")
    assert fit_entry["lost"] == 0.0
    assert fit_entry["gained"] == pytest.approx(hub._WEIGHT_FIT * 100.0)


def test_score_breakdown_reports_the_raw_downloads_behind_the_popularity_axis():
    row = {"fit": None, "params": None, "speedEstimate": None, "created": None,
           "downloads": 42, "local": {"state": "none"}}
    entries = hub._score_breakdown(row, 32.0)
    popularity = next(e for e in entries if e["axis"] == "popularity")
    assert popularity["downloads"] == 42


def test_score_breakdown_reports_age_days_behind_the_recency_axis():
    from datetime import datetime, timedelta, timezone
    created = (datetime.now(timezone.utc) - timedelta(days=730)).isoformat()
    row = {"fit": None, "params": None, "speedEstimate": None, "created": created,
           "downloads": None, "local": {"state": "none"}}
    entries = hub._score_breakdown(row, 32.0)
    recency = next(e for e in entries if e["axis"] == "recency")
    assert recency["ageDays"] == pytest.approx(730.0, abs=1.0)


def test_score_breakdown_only_includes_on_disk_bonus_when_actually_on_disk():
    absent = {"fit": None, "params": None, "speedEstimate": None, "created": None,
              "downloads": None, "local": {"state": "none"}}
    have = dict(absent, local={"state": "downloaded"})
    assert not any(e["axis"] == "onDisk" for e in hub._score_breakdown(absent, 32.0))
    bonus = next(e for e in hub._score_breakdown(have, 32.0) if e["axis"] == "onDisk")
    assert bonus["gained"] == hub._ON_DISK_BONUS
    assert bonus["lost"] == 0.0


def test_score_breakdown_reports_run_mode_penalty_matching_the_composite():
    base = {"params": None, "speedEstimate": None, "created": None, "downloads": None,
            "local": {"state": "none"}}
    offload = dict(base, fit={"score": 80, "runMode": "cpu-offload"})
    cpu_only = dict(base, fit={"score": 80, "runMode": "cpu-only"})
    gpu = dict(base, fit={"score": 80, "runMode": "gpu"})
    offload_entry = next(e for e in hub._score_breakdown(offload, 32.0) if e["axis"] == "runMode")
    cpu_only_entry = next(e for e in hub._score_breakdown(cpu_only, 32.0) if e["axis"] == "runMode")
    assert offload_entry["lost"] == hub._CPU_OFFLOAD_PENALTY
    assert offload_entry["runMode"] == "cpu-offload"
    assert cpu_only_entry["lost"] == hub._CPU_ONLY_PENALTY
    assert not any(e["axis"] == "runMode" for e in hub._score_breakdown(gpu, 32.0))


def test_score_breakdown_reports_footprint_and_pool_gb_for_the_fit_axis():
    row = {"fit": {"score": 40, "runMode": "gpu", "footprintBytes": 17 * hub.fit.GB_BYTES},
           "params": None, "speedEstimate": None, "created": None, "downloads": None,
           "local": {"state": "none"}}
    entries = hub._score_breakdown(row, 32.0, pool_gb=22.4)
    fit_entry = next(e for e in entries if e["axis"] == "fit")
    assert fit_entry["footprintGb"] == pytest.approx(17.0, abs=0.1)
    assert fit_entry["poolGb"] == pytest.approx(22.4)


def test_score_breakdown_omits_pool_gb_when_not_supplied():
    row = {"fit": {"score": 40, "runMode": "gpu", "footprintBytes": 17 * hub.fit.GB_BYTES},
           "params": None, "speedEstimate": None, "created": None, "downloads": None,
           "local": {"state": "none"}}
    entries = hub._score_breakdown(row, 32.0)
    fit_entry = next(e for e in entries if e["axis"] == "fit")
    assert fit_entry["poolGb"] is None


def test_score_breakdown_pool_gb_prefers_the_rows_own_selected_pool(monkeypatch):
    """C4: a row selected onto VRAM (a smaller pool than the machine's
    combined VRAM+RAM offload budget) must report ITS OWN pool in the
    tooltip, not the caller's `pool_gb` reading of the bigger combined
    figure — the two disagree whenever a discrete GPU is present."""
    row = {"fit": {"score": 40, "runMode": "gpu", "footprintBytes": 17 * hub.fit.GB_BYTES,
                   "poolBytes": 24 * hub.fit.GB_BYTES, "poolName": "gpu"},
           "params": None, "speedEstimate": None, "created": None, "downloads": None,
           "local": {"state": "none"}}
    # The caller's own combined-budget reading (e.g. VRAM + system RAM) —
    # deliberately a different, larger figure than the row's own VRAM pool.
    entries = hub._score_breakdown(row, 32.0, pool_gb=48.0)
    fit_entry = next(e for e in entries if e["axis"] == "fit")
    assert fit_entry["poolGb"] == pytest.approx(24.0)


def test_score_breakdown_pool_gb_falls_back_when_row_has_no_verdict():
    row = {"fit": None, "params": None, "speedEstimate": None, "created": None,
           "downloads": None, "local": {"state": "none"}}
    entries = hub._score_breakdown(row, 32.0, pool_gb=22.4)
    fit_entry = next(e for e in entries if e["axis"] == "fit")
    assert fit_entry["poolGb"] == pytest.approx(22.4)
