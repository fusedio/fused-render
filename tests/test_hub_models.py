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

import httpx
import pytest
from fastapi.testclient import TestClient

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
    and the handful that care about the format filter override it.

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
    """
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner(tags=()))


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
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/big",
        safetensors={"parameters": {"BF16": 8_000_000_000}, "total": 8_000_000_000},
    )]))
    row = _search(client).json()["models"][0]
    assert row["params"] == 8_000_000_000
    assert row["estimatedSize"] == 16_000_000_000


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


def _gguf_runner(tags=("gguf",)):
    """A stand-in for whatever runner `registry.for_capability` resolves to,
    carrying only the one field `_model_row` reads. Not a real `Runner` —
    this module's own resolution is under test, not the registry's."""
    import types as _types

    return _types.SimpleNamespace(hub_filter_tags=tags)


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


def test_a_gguf_row_carries_no_file_when_the_active_engine_is_not_llamacpp(
        client, hub_cache, monkeypatch):
    """When the capability's active runner declares no format tag at all —
    the `mlx-text` case — a repo is not resolved or dropped
    by the picker, whatever its `siblings` look like: `file` is simply
    absent from the answer, the same as it always was before D412."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: _gguf_runner(tags=()))
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/whatever", siblings=[
        {"rfilename": "m-mmproj-F16.gguf"},
    ])]))
    row = _search(client).json()["models"][0]
    assert row["file"] is None


def test_a_gguf_row_carries_no_file_when_nothing_serves_the_capability_here(
        client, hub_cache, monkeypatch):
    """`for_capability` returning None (nothing registered could run here)
    must not crash the join — the row still surfaces on capability
    existence alone, per D313, with `file` simply unset."""
    monkeypatch.setattr(hub, "for_capability", lambda capability: None)
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/whatever")]))
    row = _search(client).json()["models"][0]
    assert row["file"] is None


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
    assert "filter=text-generation" in url and "gguf" not in url


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


# -- fit, speed and age (task 1) --------------------------------------------


def test_a_row_with_params_carries_fit_speed_and_created(client, hub_cache, monkeypatch):
    # 8B params at BF16 is a real footprint fit.verdict can judge, and a
    # text-generation row is exactly the capability speed.estimate_tok_s covers.
    monkeypatch.setattr(httpx, "get", _reply([_hit(
        "org/big",
        createdAt="2026-08-01T00:00:00.000Z",
        safetensors={"parameters": {"BF16": 8_000_000_000}, "total": 8_000_000_000},
    )]))
    row = _search(client).json()["models"][0]
    assert row["created"] == "2026-08-01T00:00:00.000Z"
    assert row["fit"] is not None
    assert set(row["fit"]) == {"verdict", "basis", "footprintBytes", "score", "runMode"}
    assert row["speedEstimate"] is not None
    assert "tokensPerSecond" in row["speedEstimate"]


def test_a_row_with_no_params_and_no_size_carries_nulls_not_a_guess(client, hub_cache, monkeypatch):
    monkeypatch.setattr(httpx, "get", _reply([_hit("org/gguf", safetensors=None)]))
    row = _search(client).json()["models"][0]
    assert row["fit"] is None
    assert row["speedEstimate"] is None
    assert row["created"] is None


def test_speed_estimate_is_absent_for_a_non_text_capability(client, hub_cache, monkeypatch):
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


# -- ranking by fit, trending, and hiding what cannot run (task 2) ----------


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


def test_sort_trending_reaches_the_wire_as_trendingscore(client, hub_cache, monkeypatch):
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"sort": "trending"})
    url = fake.calls[0][0]
    assert "sort=trendingScore" in url


def test_a_verdict_no_row_is_absent_by_default_and_present_with_the_opt_in_flag(
        client, hub_cache, monkeypatch):
    fake = _reply([
        _fitted("org/fits", score=100, safetensors_gb=1),
        _fitted("org/toobig", score=0, safetensors_gb=4000),
    ])
    monkeypatch.setattr(httpx, "get", fake)
    default = _search(client).json()
    assert [m["id"] for m in default["models"]] == ["org/fits"]
    assert default["hiddenUnfit"] == 1

    opted_in = _search(client, {"includeUnfit": True}).json()
    assert {m["id"] for m in opted_in["models"]} == {"org/fits", "org/toobig"}
    assert opted_in["hiddenUnfit"] == 0


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

    With a task filter the Hub has already done the constraining, so asking for
    more would be spending someone's rate limit on rows that are thrown away.
    """
    fake = _reply([])
    monkeypatch.setattr(httpx, "get", fake)
    _search(client, {"q": "small", "limit": 10})
    assert "limit=40" in fake.calls[0][0]

    _search(client, {"q": "small", "task": "text-generation", "limit": 10})
    assert "limit=10" in fake.calls[1][0]


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
                    "usedStorage": 4_619_599_193, "error": None}
    url = fake.calls[0][0]
    assert url == ("https://huggingface.co/api/models/"
                   "Runpod/FLUX.2-klein-4B-mflux-4bit?expand%5B%5D=usedStorage")


def test_a_repo_the_hub_has_no_total_for_reports_none(client, monkeypatch):
    # No guess and no fallback to the dtype map: this route's only job is the
    # total, and a repo the Hub does not measure has none.
    monkeypatch.setattr(httpx, "get", _detail({"id": "org/m"}))
    assert _size(client, {"id": "org/m"}).json() == {
        "id": "org/m", "usedStorage": None, "error": None}


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
