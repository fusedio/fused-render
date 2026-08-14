"""GET /api/ai-models/hub/* — searching the Hub, joined to the local cache
(SPEC §39).

The Hub itself is never called: `httpx.get` is replaced per test, because the
point under test is what this module DOES with an answer — how it joins, what it
leaves out, and how it behaves when the far side is unreachable, rate-limiting,
or sending something unexpected. A test that reached huggingface.co would be
testing huggingface.co.
"""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.server.routers import ai_models as ai_models_mod
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
    # A developer's real token must not decide what these tests assert.
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-home"))


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
    monkeypatch.setattr(httpx, "get", _reply([{"id": "org/partial"}]))
    models = _search(client).json()["models"]
    assert models[0]["local"]["state"] == "partial"


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
    monkeypatch.setattr(httpx, "get", _reply([{"id": "org/m2"}, {"id": "org/absent"}]))

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
    fake = _reply([{"id": "org/m"}])
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
    assert row["taskHelp"] == ai_models_mod._TASK_HELP["image + text to text"]


def test_size_is_recovered_from_the_dtype_map(client, hub_cache, monkeypatch):
    # 8B parameters at BF16 is 16GB, and saying so before the click is the
    # number that matters on a page whose sibling feature exists because disks
    # fill up.
    monkeypatch.setattr(httpx, "get", _reply([{
        "id": "org/big",
        "safetensors": {"parameters": {"BF16": 8_000_000_000}, "total": 8_000_000_000},
    }]))
    row = _search(client).json()["models"][0]
    assert row["params"] == 8_000_000_000
    assert row["estimatedSize"] == 16_000_000_000


def test_a_repo_with_no_safetensors_metadata_reports_no_size(client, hub_cache, monkeypatch):
    # A size we cannot compute is left out. A guessed one would be a number
    # someone plans a download around.
    monkeypatch.setattr(httpx, "get", _reply([{"id": "org/gguf", "safetensors": None}]))
    row = _search(client).json()["models"][0]
    assert row["estimatedSize"] is None and row["params"] is None


def test_gated_is_reported_before_someone_tries(client, hub_cache, monkeypatch):
    # The Hub sends "auto"/"manual"/false. Any of the truthy ones means the
    # licence has to be accepted first, which is worth knowing in advance.
    monkeypatch.setattr(httpx, "get", _reply([
        {"id": "org/gated", "gated": "manual"}, {"id": "org/open", "gated": False}]))
    rows = {m["id"]: m for m in _search(client).json()["models"]}
    assert rows["org/gated"]["gated"] is True
    assert rows["org/open"]["gated"] is False


def test_missing_fields_are_absent_not_fatal(client, hub_cache, monkeypatch):
    # The Hub returns what it returns, and an older deployment may refuse an
    # expand[] field entirely. Nothing here indexes blindly.
    monkeypatch.setattr(httpx, "get", _reply([{"id": "org/bare"}]))
    row = _search(client).json()["models"][0]
    assert row["id"] == "org/bare"
    assert row["task"] is None and row["downloads"] is None and row["tags"] == []


def test_a_row_with_no_id_is_dropped(client, hub_cache, monkeypatch):
    # A row the page could not act on is a row it should not be given.
    monkeypatch.setattr(httpx, "get", _reply([{"likes": 3}, {"id": "org/real"}]))
    models = _search(client).json()["models"]
    assert [m["id"] for m in models] == ["org/real"]


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
    fake = _reply([{"id": "org/m"}])
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
    fake = _reply([{"id": "org/m"}])
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
