"""The model mirror's manifest client (SPEC AI-5l).

`mirror.py` answers one question — "what does our own mirror hold for this repo,
and at which URLs" — and it is the only part of the feature that reads bytes we
did not write ourselves. So every test here is about a manifest that is WRONG in
one specific way, because the client's contract is that a wrong manifest reads
as NO MIRROR rather than as an error: a mirror that is down, misconfigured or
serving junk must cost a slower download and never a failed one.

The server is the same local `http.server` harness `test_ai_hub_fetch` drives —
imported rather than copied, because a mirror is served over the same HTTP the
Hub is and a second harness would be a second set of CDN misbehaviours to keep
in step. Nothing here reaches huggingface.co, and nothing reaches CloudFront.
"""
import hashlib
import importlib.util
import json
import os

import pytest
from test_ai_hub_fetch import _start_server

MIRROR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "mirror.py",
)


def _fresh_mirror():
    """A fresh import of `mirror`, by path.

    By path for the same reason `worker_base` is: it lives in `runners/`, which
    is on a WORKER's `sys.path` (the worker script puts it there) and is not a
    package, so there is no import statement that reaches it from here.
    """
    spec = importlib.util.spec_from_file_location("mirror_under_test", MIRROR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mirror():
    return _fresh_mirror()


BLOB = b"the weights" * 100
ETAG = hashlib.sha256(BLOB).hexdigest()
COMMIT = "a1b2c3d4" * 5


def _manifest(**overrides):
    """A manifest the client accepts, before a test breaks one field of it."""
    payload = {
        "schema": 1,
        "repo": "org/m",
        "commit": COMMIT,
        "complete": True,
        "files": [{"name": "model.safetensors", "etag": ETAG,
                   "size": len(BLOB), "sha256": ETAG}],
    }
    payload.update(overrides)
    return payload


def _serve(payload, model_id="org/m", **flags):
    """A mirror that answers a manifest for `model_id` and nothing else.

    `payload` may be a dict (served as JSON), raw bytes, or an int status.
    `flags` reach the harness, so a test can make the RESPONSE misbehave rather
    than only its body — see `test_ai_hub_fetch._start_server`.
    """
    body = payload
    if isinstance(payload, dict):
        body = json.dumps(payload).encode()
    org, _, name = model_id.partition("/")
    routes = {f"/models/{org}/{name}/manifest.json": body}
    if not isinstance(payload, int):
        routes[f"/models/{org}/{name}/{COMMIT}/{ETAG}"] = BLOB
    _url, state = _start_server(b"", routes=routes, **flags)
    return state


def _point_at(monkeypatch, state, model_id="org/m"):
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"])
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", model_id)


# -- the permission is per-model, and it is what gates the probe ------------------


def test_no_mirror_is_configured_so_nothing_is_probed(mirror, monkeypatch):
    """The default for every user: no base URL, no request, no mirror.

    The env var being unset is not a degraded state — it is the shipped one, and
    every download stays on today's Hub path.
    """
    state = _serve(_manifest())
    monkeypatch.delenv("FUSED_MODEL_MIRROR", raising=False)
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "org/m")

    assert mirror.allowed("org/m") is False
    assert mirror.manifest("org/m") is None
    assert state["requests"] == [], "a mirror that is not configured was probed"


def test_permission_withheld_means_the_mirror_is_never_asked(mirror, monkeypatch):
    """The privacy rule, and the reason the flag is per-model rather than global.

    The supervisor sets `FUSED_MODEL_MIRROR_OK` only for a SUGGESTED model, so
    the mirror never sees a request naming a model the user chose from Discover.
    Withheld, a configured base URL still buys nothing — the probe itself is
    what would leak the name.
    """
    state = _serve(_manifest())
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"])
    monkeypatch.delenv("FUSED_MODEL_MIRROR_OK", raising=False)

    assert mirror.allowed("org/m") is False
    assert mirror.manifest("org/m") is None
    assert state["requests"] == [], "an unmirrored model was named to the mirror"


def test_the_permission_is_for_one_model_not_for_any_model(mirror, monkeypatch):
    """A flag set for one repo must not licence a probe for another.

    The worker is only ever sent to fetch one model, so the flag carries that
    model's id rather than a bare "1": a stale or inherited flag then cannot
    hand permission to whatever the next download happens to be.
    """
    state = _serve(_manifest())
    _point_at(monkeypatch, state, "org/m")

    assert mirror.allowed("org/m") is True
    assert mirror.allowed("other/n") is False
    assert mirror.manifest("other/n") is None
    assert state["requests"] == []


# -- a manifest the client accepts ------------------------------------------------


def test_a_good_manifest_yields_the_file_list_and_hub_shaped_metadata(mirror,
                                                                      monkeypatch):
    """The whole contract: one request, then per-file metadata shaped exactly
    like `_hub_file_meta`'s return value.

    Shaped exactly, because the fetcher takes it as a drop-in for that call —
    the same five keys, plus the `sha256` the Hub cannot give us and which is
    what lets the mirror path verify what it wrote.
    """
    state = _serve(_manifest())
    _point_at(monkeypatch, state)

    manifest = mirror.manifest("org/m")

    assert manifest is not None
    assert manifest["commit"] == COMMIT
    assert [entry["name"] for entry in manifest["files"]] == ["model.safetensors"]
    assert [entry["size"] for entry in manifest["files"]] == [len(BLOB)]
    # Exactly one request, before any bytes: the counting key (see the plan).
    assert [r["path"] for r in state["requests"]] == [
        "/models/org/m/manifest.json"]

    meta = mirror.file_meta("org/m", manifest)("org/m", "model.safetensors", COMMIT)
    assert meta == {
        "url": f"{state['origin']}/models/org/m/{COMMIT}/{ETAG}",
        "location": f"{state['origin']}/models/org/m/{COMMIT}/{ETAG}",
        "etag": ETAG, "commit": COMMIT, "size": len(BLOB), "sha256": ETAG,
    }
    # The blob URL is commit-pinned and therefore immutable, which is what lets
    # it be cached forever while the manifest above stays short-TTL.
    assert COMMIT in meta["url"]


def test_a_base_url_with_a_trailing_slash_or_a_prefix_still_resolves(mirror,
                                                                     monkeypatch):
    """An operator points this at staging, a bucket subpath, or types a trailing
    slash. None of the three may produce a double slash or a lost prefix."""
    state = _serve(_manifest())
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"] + "/")
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "org/m")

    assert mirror.manifest_url("org/m") == (
        f"{state['origin']}/models/org/m/manifest.json")
    assert mirror.manifest("org/m") is not None


# -- validation is a trust boundary ----------------------------------------------


@pytest.mark.parametrize("broken, why", [
    ({"schema": 2}, "a schema version this build does not know"),
    ({"schema": "1"}, "a schema version that is not even a number"),
    ({"commit": "not-a-sha"}, "a commit that is not 40 hex characters"),
    ({"commit": "A1B2C3D4" * 5}, "a commit in upper case, which names no folder"),
    ({"repo": "other/n"}, "a manifest for a different repo"),
    ({"files": []}, "no files at all"),
    ({"files": "model.safetensors"}, "a file list that is not a list"),
    ({"files": [{"name": "m", "etag": "zz", "size": 1, "sha256": ETAG}]},
     "an etag that is not hex"),
    ({"files": [{"name": "m", "etag": ETAG, "size": -5, "sha256": ETAG}]},
     "a negative size"),
    ({"files": [{"name": "m", "etag": ETAG, "size": 1.5, "sha256": ETAG}]},
     "a size that is not an integer"),
    ({"files": [{"name": "m", "etag": ETAG, "size": 1, "sha256": "nope"}]},
     "a sha256 that is not 64 hex characters"),
    ({"files": [{"name": "m", "etag": ETAG, "size": 1}]}, "no sha256 at all"),
    ({"files": [{"name": "../escape", "etag": ETAG, "size": 1, "sha256": ETAG}]},
     "a name that climbs out of the snapshot directory"),
    ({"files": [{"name": "/etc/passwd", "etag": ETAG, "size": 1, "sha256": ETAG}]},
     "an absolute name"),
    ({"files": [{"name": "", "etag": ETAG, "size": 1, "sha256": ETAG}]},
     "an empty name"),
    ({"files": [{"name": "a", "etag": ETAG, "size": 1, "sha256": ETAG},
                {"name": "a", "etag": ETAG, "size": 1, "sha256": ETAG}]},
     "the same name twice"),
    ({"files": [{"name": "a", "etag": "../../blob", "size": 1, "sha256": ETAG}]},
     "an etag that is a path rather than a name"),
])
def test_a_manifest_that_is_wrong_in_one_field_reads_as_no_mirror(mirror, monkeypatch,
                                                                  broken, why):
    """Every one of these is a rejection, and NONE of them is an exception.

    The client is parsing bytes from a CDN, so the interesting failures are the
    ones that would otherwise be plausible: a name that climbs out of the
    snapshot directory writes a file wherever it likes, and an etag that is a
    path does the same inside `blobs/`. A size or a hash that is not what it
    claims to be would corrupt the cache under a real etag, where hf itself then
    serves it forever.

    Rejection means `None`, not `raise`: the caller's contract is "no mirror",
    and a raise here would have to be caught by every call site to mean the
    same thing.
    """
    state = _serve(_manifest(**broken))
    _point_at(monkeypatch, state)

    assert mirror.manifest("org/m") is None, why


def test_a_body_that_is_not_json_at_all_reads_as_no_mirror(mirror, monkeypatch):
    """An HTML error page from a misconfigured distribution, served with a 200."""
    state = _serve(b"<html>NoSuchKey</html>")
    _point_at(monkeypatch, state)

    assert mirror.manifest("org/m") is None


def test_a_json_body_that_is_not_an_object_reads_as_no_mirror(mirror, monkeypatch):
    """`json.loads` is happy with `[]` and `2`; neither has a `.get`."""
    for body in (b"[]", b"2", b"null", b'"manifest"'):
        state = _serve(body)
        _point_at(monkeypatch, state)
        assert mirror.manifest("org/m") is None, body


def test_a_manifest_larger_than_the_cap_is_refused(mirror, monkeypatch):
    """A manifest is a few KB of names. A response that is not is not one, and
    reading it into memory unbounded is the one thing this client must not do
    on the strength of a URL an operator typed."""
    huge = {"schema": 1, "repo": "org/m", "commit": COMMIT,
            "files": [{"name": f"f{n}", "etag": ETAG, "size": 1,
                       "sha256": ETAG} for n in range(200_000)]}
    state = _serve(huge)
    _point_at(monkeypatch, state)

    assert mirror.manifest("org/m") is None


# -- a mirror that is down --------------------------------------------------------


def test_a_404_reads_as_no_mirror(mirror, monkeypatch):
    """The ordinary answer for a model nobody has mirrored yet, and the reason
    the suggested list and the mirror's contents are allowed to disagree."""
    state = _serve(404)
    _point_at(monkeypatch, state)

    assert mirror.manifest("org/m") is None
    assert len(state["requests"]) == 1, "the manifest was asked for exactly once"


def test_a_5xx_reads_as_no_mirror(mirror, monkeypatch):
    """A distribution having a bad day costs a slower download, never a failed
    one."""
    state = _serve(503)
    _point_at(monkeypatch, state)

    assert mirror.manifest("org/m") is None


def test_a_host_that_does_not_answer_reads_as_no_mirror(mirror, monkeypatch):
    """A base URL pointing at nothing — a typo, or a distribution being
    replaced. Connection refused is not an error the caller should see."""
    state = _serve(_manifest())
    state["server"].shutdown()
    state["server"].server_close()
    _point_at(monkeypatch, state)

    assert mirror.manifest("org/m") is None


def test_a_base_url_that_is_not_http_is_refused_without_a_request(mirror,
                                                                  monkeypatch):
    """`file://` and friends are not a mirror. Refused on the SCHEME, before
    `urlopen` is handed a URL that could read a local path instead."""
    monkeypatch.setenv("FUSED_MODEL_MIRROR", "file:///etc")
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "org/m")

    assert mirror.allowed("org/m") is False
    assert mirror.manifest("org/m") is None


def test_a_repo_id_that_is_not_org_slash_name_is_refused(mirror, monkeypatch):
    """The URL layout is `/models/<org>/<name>/…`, so an id that is not that
    shape has no place in it. Refused rather than fitted into the path, where a
    stray slash would address a different object entirely."""
    state = _serve(_manifest())
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"])
    for bad in ("m", "a/b/c", "/m", "org/", "", "org/../../x"):
        monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", bad)
        assert mirror.allowed(bad) is False, bad
        assert mirror.manifest(bad) is None, bad
    assert state["requests"] == []


# -- the rule that keeps this importable by every runner venv ---------------------


def test_mirror_imports_nothing_but_the_stdlib():
    """Same rule as `worker_base`, enforced the same way and for the same reason.

    This file is imported by every runner's interpreter, so anything imported
    here becomes a dependency of every backend forever — and absence does not
    enforce it, since `huggingface_hub` and `requests` both resolve in this
    environment (D402) and an accidental module-scope import of either would
    pass unnoticed.
    """
    import ast
    import sys

    tree = ast.parse(open(MIRROR_PATH, encoding="utf-8").read())
    imported = set()
    for node in tree.body:  # module scope ONLY
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported, "the file surely imports something — did MIRROR_PATH move?"
    outside = sorted(name for name in imported
                     if name not in sys.stdlib_module_names)
    assert outside == [], f"mirror gained a non-stdlib module-scope import: {outside}"


# -- a manifest has to declare that it is the WHOLE repo (review finding 3) ------


def test_a_manifest_that_does_not_claim_to_be_complete_is_refused(mirror,
                                                                  monkeypatch):
    """`complete` is the manifest asserting that it lists EVERY file in the repo
    at that commit, and the client requires it.

    Without it the client would be inferring completeness from the very document
    it is reading — and the consequence is not a slow download but a permanent
    one: a manifest missing `config.json` downloads a subset, the fetch record
    calls that subset complete at this scope, and every later bring-up is served
    a snapshot that cannot load, with nothing to make it refetch. The client
    cannot check completeness itself without asking the Hub, which is the one
    thing this feature exists to avoid — so the proof is the build script's job
    (it verifies the snapshot against the Hub's own listing at that commit) and
    this field is where that proof is recorded. A manifest that cannot say it is
    complete is not one this client will file a record for, so it is not one it
    will use at all.
    """
    for value in (False, None, 1, "yes"):
        state = _serve(_manifest(complete=value))
        _point_at(monkeypatch, state)
        assert mirror.manifest("org/m") is None, value


def test_a_manifest_with_no_complete_field_at_all_is_refused(mirror, monkeypatch):
    """A hand-written manifest, or one from an older generator that could not
    prove completeness. Absence is not consent."""
    payload = _manifest()
    del payload["complete"]
    state = _serve(payload)
    _point_at(monkeypatch, state)

    assert mirror.manifest("org/m") is None


# -- an empty file is a real file (review finding 6) ------------------------------


def test_a_zero_byte_file_does_not_reject_the_whole_repo(mirror, monkeypatch):
    """An empty file is legal on the Hub and hf caches it like any other.

    Rejecting the MANIFEST for one of them would take a whole model off the
    mirror over a file with nothing in it — and the fetcher handles a
    zero-length segment already (`_chunks(0)` yields one piece that is complete
    on arrival).
    """
    empty = hashlib.sha256(b"").hexdigest()
    state = _serve(_manifest(files=[
        {"name": "model.safetensors", "etag": ETAG, "size": len(BLOB),
         "sha256": ETAG},
        {"name": "empty.txt", "etag": empty, "size": 0, "sha256": empty},
    ]))
    _point_at(monkeypatch, state)

    manifest = mirror.manifest("org/m")

    assert manifest is not None
    assert [entry["size"] for entry in manifest["files"]] == [len(BLOB), 0]


def test_a_negative_or_non_integer_size_is_still_refused(mirror, monkeypatch):
    """Allowing zero is not allowing nonsense: a size is the length of a file
    this client is about to pre-size on disk."""
    for size in (-1, 1.5, "12", True, None):
        state = _serve(_manifest(files=[{"name": "m", "etag": ETAG,
                                         "size": size, "sha256": ETAG}]))
        _point_at(monkeypatch, state)
        assert mirror.manifest("org/m") is None, size


# -- a response that falls apart, not merely a body that is wrong (finding 2) -----


def test_a_manifest_response_that_falls_apart_reads_as_no_mirror(mirror,
                                                                 monkeypatch):
    """A truncated chunked body raises `http.client.IncompleteRead`, which is an
    `HTTPException` — NOT an `OSError`, and not a `ValueError`.

    So it escaped the guard that was written to mean "any way of not getting a
    manifest", and a mirror host misbehaving at the HTTP level failed the whole
    download instead of degrading to the Hub. `worker_base._TRANSIENT` has
    always named this family for the same reason.
    """
    state = _serve(_manifest(), break_first=1, break_bytes=4)
    _point_at(monkeypatch, state)

    assert mirror.manifest("org/m") is None
    assert len(state["requests"]) == 1
