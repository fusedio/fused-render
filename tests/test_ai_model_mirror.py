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
# `no_egress` is imported for its SIDE EFFECT: it is an autouse fixture, so
# binding the name in this module installs it for every test here. See its
# docstring — Windows CI proved that "every test stubs the Hub" is not the same
# claim as "no test reaches the network".
from test_ai_hub_fetch import _start_server, no_egress  # noqa: F401

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


def test_unset_now_means_the_shipped_default_not_no_mirror(mirror, monkeypatch):
    """The reversal this module used to encode the opposite of.

    Before the default flipped on, deleting `FUSED_MODEL_MIRROR` was this
    file's spelling of "no mirror is configured". It is not anymore: unset now
    resolves to `DEFAULT_BASE`, exactly like an operator having typed it. This
    checks `base_url()` alone (never `manifest()`) so the test itself cannot
    become the thing `no_egress` exists to catch — probing the *real* default
    host is not this test's job.
    """
    monkeypatch.delenv("FUSED_MODEL_MIRROR", raising=False)

    assert mirror.base_url() == mirror.DEFAULT_BASE


def test_the_documented_opt_out_yields_no_mirror(mirror, monkeypatch):
    """Setting the var to `""` is how a user (or an air-gapped install, or a
    test) says "no mirror at all" now that unset no longer means that.

    `allowed()` must be False even though `FUSED_MODEL_MIRROR_OK` matches the
    model asked about — the opt-out has to win over a base URL that happens to
    still be configured for the permission half.
    """
    state = _serve(_manifest())
    monkeypatch.setenv("FUSED_MODEL_MIRROR", "")
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "org/m")

    assert mirror.base_url() == ""
    assert mirror.allowed("org/m") is False
    assert mirror.manifest("org/m") is None
    assert state["requests"] == [], "a mirror that opted out was probed"


@pytest.mark.parametrize("spelling", ["off", "0", "none", "None", "OFF"])
def test_other_falsy_spellings_also_opt_out_for_free(mirror, monkeypatch, spelling):
    """`off`/`0`/`none` need no special-case code: none of them is a valid
    `http(s)://host` URL, so `_valid_base` already reads every one of them as
    "no mirror" — the same path the empty-string opt-out takes."""
    monkeypatch.setenv("FUSED_MODEL_MIRROR", spelling)

    assert mirror.base_url() == ""


def test_an_env_value_overrides_the_default(mirror, monkeypatch):
    """The operator escape hatch: pointing it at staging (or a test server)
    is not shadowed by the shipped default."""
    state = _serve(_manifest())
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"])

    assert mirror.base_url() == state["origin"]


def test_a_corrupted_default_constant_still_fails_validation(mirror, monkeypatch):
    """`DEFAULT_BASE` is not exempt from `_valid_base` — a future edit that
    breaks its scheme or netloc must read as no mirror, not crash and not
    silently serve a bad URL to every worker."""
    monkeypatch.delenv("FUSED_MODEL_MIRROR", raising=False)
    monkeypatch.setattr(mirror, "DEFAULT_BASE", "not-a-url")

    assert mirror.base_url() == ""


def test_the_default_does_not_widen_who_may_be_named_to_the_mirror(mirror,
                                                                    monkeypatch):
    """The privacy invariant, with the default in play. An id outside
    `catalog.all_suggested_ids()` never reaches `FUSED_MODEL_MIRROR_OK` (that
    is `supervisor._mirror_ok`'s job, tested elsewhere) — this pins the other
    half, in the client itself: even with the shipped default base active,
    `allowed()` is false for any id the permission env var does not name."""
    monkeypatch.delenv("FUSED_MODEL_MIRROR", raising=False)
    monkeypatch.delenv("FUSED_MODEL_MIRROR_OK", raising=False)

    assert mirror.base_url() == mirror.DEFAULT_BASE
    assert mirror.allowed("somebody/a-model-we-never-suggested") is False

    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "org/m")
    assert mirror.allowed("somebody/a-model-we-never-suggested") is False
    assert mirror.allowed("org/m") is True


def test_no_mirror_is_configured_so_nothing_is_probed(mirror, monkeypatch):
    """The documented opt-out: no base URL, no request, no mirror.

    This used to be reached by deleting `FUSED_MODEL_MIRROR`; now that unset
    means the shipped default, the way to reach "no mirror" is the explicit
    opt-out above. This test now exercises it end to end through `manifest()`.
    """
    state = _serve(_manifest())
    monkeypatch.setenv("FUSED_MODEL_MIRROR", "")
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


def test_these_tests_cannot_reach_the_network():
    """The imported guard, asserted here too — see the import comment."""
    import socket

    with pytest.raises(AssertionError, match="tried to resolve"):
        socket.getaddrinfo("huggingface.co", 443)


# -- a manifest for ONE FILE, which claims nothing about the repo (AI-5m) ---------
#
# A GGUF repo publishes dozens of quantizations of the same model
# (`unsloth/Qwen3.5-9B-GGUF` is 147.81GB whole) and `llama_text.download` wants
# exactly one of them, so the per-repo manifest above cannot serve it: that
# document has to assert `complete: true` for the WHOLE repo, and earning that
# assertion would mean mirroring all of it to serve 2.6GB. Hence a second
# object and a second reader — a manifest that lists one named file and makes
# no completeness claim at all, which is safe precisely because nothing on this
# path writes an AI-5k fetch record (`download_file` never has).

FILE_NAME = "Model-Q4_K_M.gguf"


def _file_manifest(**overrides):
    """A per-file manifest the client accepts. Note: NO `complete` field."""
    payload = {
        "schema": 1,
        "repo": "org/m",
        "commit": COMMIT,
        "files": [{"name": FILE_NAME, "etag": ETAG,
                   "size": len(BLOB), "sha256": ETAG}],
    }
    payload.update(overrides)
    return payload


def _serve_file(payload, model_id="org/m", filename=FILE_NAME, **flags):
    """A mirror answering the per-file manifest for one (repo, filename)."""
    body = payload
    if isinstance(payload, dict):
        body = json.dumps(payload).encode()
    org, _, name = model_id.partition("/")
    routes = {f"/models/{org}/{name}/files/{filename}/manifest.json": body}
    if not isinstance(payload, int):
        routes[f"/models/{org}/{name}/{COMMIT}/{ETAG}"] = BLOB
    _url, state = _start_server(b"", routes=routes, **flags)
    return state


def test_a_per_file_manifest_needs_no_completeness_claim(mirror, monkeypatch):
    """The crux of the second reader: one named file asserts nothing about the
    repo, so there is no completeness proof to demand.

    The per-repo reader refuses a manifest without `complete: true` because it
    is about to write a fetch record saying "this scope is whole on disk"
    (AI-5k), and a list taken from the same document would make that record
    self-certifying. `download_file` writes no such record — it never has — so
    the worst a wrong per-file manifest can do is fetch the wrong bytes, which
    the sha256 check catches, and there is nothing left on disk to poison a
    later bring-up.
    """
    state = _serve_file(_file_manifest())
    _point_at(monkeypatch, state)

    manifest = mirror.file_manifest("org/m", FILE_NAME)

    assert manifest is not None
    assert manifest["commit"] == COMMIT
    assert manifest["files"] == [{"name": FILE_NAME, "etag": ETAG,
                                 "size": len(BLOB), "sha256": ETAG}]
    # One request, at the per-file URL, and nothing else — the same "the
    # manifest request IS the download signal" rule the per-repo path has.
    assert [r["path"] for r in state["requests"]] == [
        f"/models/org/m/files/{FILE_NAME}/manifest.json"]


def test_the_per_repo_reader_still_demands_completeness(mirror, monkeypatch):
    """Two readers, two claims. Adding the relaxed one must not relax the other:
    a repo manifest without `complete: true` is still no mirror."""
    payload = _manifest()
    payload.pop("complete")
    state = _serve(payload)
    _point_at(monkeypatch, state)

    assert mirror.manifest("org/m") is None


def test_both_shapes_address_the_same_blob(mirror, monkeypatch):
    """The dedupe claim, checked rather than asserted in a comment.

    The per-file manifest lives at its own key but its blobs stay at
    `<base>/models/<org>/<name>/<commit>/<etag>` — the per-repo path's blob
    space — so a repo mirrored BOTH ways stores one copy of each blob and a
    client that has one shape's URL has the other's.
    """
    state = _serve_file(_file_manifest())
    _point_at(monkeypatch, state)
    per_file = mirror.file_manifest("org/m", FILE_NAME)

    meta = mirror.file_meta("org/m", per_file)("org/m", FILE_NAME, COMMIT)

    assert meta["url"] == f"{state['origin']}/models/org/m/{COMMIT}/{ETAG}"
    assert meta["url"] == mirror.blob_url("org/m", COMMIT, ETAG)
    assert meta == {"url": meta["url"], "location": meta["url"], "etag": ETAG,
                    "commit": COMMIT, "size": len(BLOB), "sha256": ETAG}


def test_a_manifest_naming_a_different_file_is_refused(mirror, monkeypatch):
    """The requested name is the whole identity of this object. A manifest that
    answers with some other file would install those bytes under the name the
    caller asked for."""
    state = _serve_file(_file_manifest(
        files=[{"name": "something-else.gguf", "etag": ETAG,
                "size": len(BLOB), "sha256": ETAG}]))
    _point_at(monkeypatch, state)

    assert mirror.file_manifest("org/m", FILE_NAME) is None


def test_a_manifest_listing_more_than_one_file_is_refused(mirror, monkeypatch):
    """EXACTLY one entry. A second one is either a repo manifest served at a
    per-file key or a generator that does not mean what this reader reads, and
    fetching the extra file is not what the caller asked for."""
    entry = {"name": FILE_NAME, "etag": ETAG, "size": len(BLOB), "sha256": ETAG}
    other = dict(entry, name="also.gguf")
    state = _serve_file(_file_manifest(files=[entry, other]))
    _point_at(monkeypatch, state)

    assert mirror.file_manifest("org/m", FILE_NAME) is None


def test_a_completeness_claim_on_a_per_file_manifest_buys_nothing(mirror,
                                                                 monkeypatch):
    """`complete` is not read here, in either direction.

    Present or absent, this document lists one file and the reader treats it as
    one file — the field cannot promote a per-file manifest into a repo one,
    because the only thing completeness gates is the fetch record and this path
    writes none.
    """
    state = _serve_file(_file_manifest(complete=True))
    _point_at(monkeypatch, state)

    manifest = mirror.file_manifest("org/m", FILE_NAME)

    assert manifest is not None
    assert "complete" not in manifest
    assert [entry["name"] for entry in manifest["files"]] == [FILE_NAME]


@pytest.mark.parametrize("broken, why", [
    ({"schema": 2}, "a schema version this build does not know"),
    ({"schema": True}, "a boolean that would pass an `== 1` test"),
    ({"repo": "other/m"}, "a manifest that names a different repo"),
    ({"commit": "not-a-sha"}, "a commit that is not 40 hex characters"),
    ({"commit": "A1B2C3D4" * 5}, "a commit in upper case, which names no folder"),
    ({"files": []}, "no file at all where exactly one was promised"),
    ({"files": "a-string"}, "a file list that is not a list"),
    ({"files": [{"name": FILE_NAME, "etag": "../../etc/passwd",
                 "size": 1, "sha256": ETAG}]},
     "an etag that would climb out of `blobs/`"),
    ({"files": [{"name": FILE_NAME, "etag": ETAG, "size": -1, "sha256": ETAG}]},
     "a negative size"),
    ({"files": [{"name": FILE_NAME, "etag": ETAG, "size": True,
                 "sha256": ETAG}]},
     "a boolean size, which is an int in Python and not one on the wire"),
    ({"files": [{"name": FILE_NAME, "etag": ETAG, "size": 1,
                 "sha256": "short"}]},
     "a digest that is not 64 hex characters"),
    ({"files": [{"name": FILE_NAME, "etag": ETAG, "size": 1}]},
     "no digest at all — the mirror path's only proof of what it wrote"),
    ({"files": [[FILE_NAME, ETAG]]}, "an entry that is not an object"),
])
def test_a_per_file_manifest_wrong_in_one_field_reads_as_no_mirror(mirror,
                                                                  monkeypatch,
                                                                  broken, why):
    """Same trust boundary as the per-repo reader, same rejection vocabulary.
    Relaxing the completeness claim relaxes nothing else."""
    state = _serve_file(_file_manifest(**broken))
    _point_at(monkeypatch, state)

    assert mirror.file_manifest("org/m", FILE_NAME) is None, why


@pytest.mark.parametrize("bad", [
    "", "..", ".", "a/b.gguf", "/abs.gguf", "sub\\file.gguf", "C:file.gguf",
    "%2e%2e/x.gguf", "a?b.gguf", "a#b.gguf", "a b.gguf", ".hidden.gguf",
    "x" * 300,
])
def test_a_filename_that_could_address_another_object_has_no_url(mirror,
                                                                monkeypatch,
                                                                bad):
    """The filename is pasted into a URL PATH SEGMENT and then used as a
    filesystem name, so it is validated before either.

    A `/` addresses a different object, `..` climbs the key space, and `?`/`#`
    truncate the path into a query or a fragment — `files/a?b.gguf/manifest.json`
    requests `files/a` with the rest thrown away, which is a different object
    answering for this one. Refused, with no request made at all.
    """
    state = _serve_file(_file_manifest())
    _point_at(monkeypatch, state)

    assert mirror.file_manifest_url("org/m", bad) == "", bad
    assert mirror.file_manifest("org/m", bad) is None, bad
    assert state["requests"] == [], bad


def test_the_per_file_url_carries_the_base_prefix_and_no_double_slash(mirror,
                                                                     monkeypatch):
    """An operator points this at a bucket subpath or types a trailing slash."""
    state = _serve_file(_file_manifest())
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"] + "/")
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "org/m")

    assert mirror.file_manifest_url("org/m", FILE_NAME) == (
        f"{state['origin']}/models/org/m/files/{FILE_NAME}/manifest.json")
    assert mirror.file_manifest("org/m", FILE_NAME) is not None


def test_a_per_file_probe_needs_the_same_per_model_permission(mirror,
                                                             monkeypatch):
    """The privacy rule is the reader-independent half of this feature: the
    probe itself is what would leak which models a user downloads, so a second
    reader must not be a second way around the permission."""
    state = _serve_file(_file_manifest())
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"])
    monkeypatch.delenv("FUSED_MODEL_MIRROR_OK", raising=False)

    assert mirror.file_manifest("org/m", FILE_NAME) is None
    assert mirror.file_manifest_url("org/m", FILE_NAME) != ""

    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "other/m")
    assert mirror.file_manifest("org/m", FILE_NAME) is None
    assert state["requests"] == [], "an unpermitted repo was named to the mirror"


@pytest.mark.parametrize("payload, why", [
    (404, "a file nobody mirrored"),
    (503, "a distribution having a bad day"),
    (b"<html>not json</html>", "a body that is not JSON"),
    (b"[]", "a JSON body that is not an object"),
])
def test_a_mirror_that_cannot_answer_a_per_file_probe_reads_as_no_mirror(
        mirror, monkeypatch, payload, why):
    state = _serve_file(payload)
    _point_at(monkeypatch, state)

    assert mirror.file_manifest("org/m", FILE_NAME) is None, why


def test_a_per_file_manifest_larger_than_the_cap_is_refused(mirror, monkeypatch):
    """One file's worth of names cannot be a megabyte, and reading a response
    into memory unbounded on the strength of a base URL is the one thing this
    client must not do."""
    padded = _file_manifest(pad="x" * (mirror.MAX_MANIFEST_BYTES + 10))
    state = _serve_file(padded)
    _point_at(monkeypatch, state)

    assert mirror.file_manifest("org/m", FILE_NAME) is None


def test_a_per_file_response_that_falls_apart_reads_as_no_mirror(mirror,
                                                                monkeypatch):
    """`http.client.HTTPException` is neither an `OSError` nor a `ValueError`,
    and a truncated chunked body raising out of here would FAIL a download the
    Hub could have served."""
    state = _serve_file(_file_manifest(), chunked=True, budget=40)
    _point_at(monkeypatch, state)

    assert mirror.file_manifest("org/m", FILE_NAME) is None
