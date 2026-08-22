"""Our own download path for the suggested models (SPEC AI-5l).

A suggested model is one WE chose to put in front of the user, so the bytes can
come from a distribution we run instead of from huggingface.co. This module is
the whole client half of that: it answers "what does the mirror hold for this
repo, and at which URLs", and nothing else. The bytes themselves are fetched by
`worker_base._segmented_fetch`, which already knows how to range-fetch a file
into hf's cache layout, and what lands on disk is byte-for-byte the cache
directory hf itself would have produced — which is why the loaders, the Local
tab's inventory, disk usage and deletion all keep working untouched.

**Two objects, not a protocol.** A manifest per repo, mutable and short-TTL, and
one immutable blob per distinct etag under the commit:

    <base>/models/<org>/<name>/manifest.json
    <base>/models/<org>/<name>/<commit>/<etag>

**Plus a third shape for ONE named file, which is not a relaxed manifest but a
different claim** (AI-5m):

    <base>/models/<org>/<name>/files/<filename>/manifest.json

`llama_text.download` fetches a single GGUF out of a repo that publishes dozens
of quantizations — `unsloth/Qwen3.5-9B-GGUF` is 147.81GB whole for a 2.6GB
file — and the manifest above cannot serve that, because it has to ASSERT it
lists the whole repo at the commit (`complete: true`, see `validate_manifest`) and
earning that assertion would mean mirroring all of it. So there is a second
reader, `file_manifest`, with its own document and its own claim: exactly one
named file, and no completeness claim at all. What makes dropping the claim safe
rather than convenient is what the CALLER does next — `worker_base.download_file`
writes no AI-5k fetch record and never has, so nothing on this path can record a
partial answer as whole, which is the only harm the assertion exists to prevent.
The blobs stay in the per-repo blob space above, so a repo mirrored both ways
stores one copy of each blob.

That is deliberately NOT `HF_ENDPOINT`, which is a protocol switch: the mirror
would then have to answer `/api/models/…`, produce `x-linked-etag`,
`x-linked-size` and `x-repo-commit` on a resolve, and hold up its end of Xet's
`xet-read-token`. Two objects on any static host with `Range` has none of that
surface. The manifest request is also the one signal that a download STARTED,
and it is made exactly once per attempt before a single byte moves.

**Two environment variables, and the second one is a privacy rule.**
`FUSED_MODEL_MIRROR` is the base URL — unset on every shipped build today, which
leaves every download on the Hub path. `FUSED_MODEL_MIRROR_OK` carries the ONE
repo id this process is permitted to name to the mirror, set by
`supervisor._child_env` only when `catalog.all_suggested_ids()` contains it. The
worker cannot make that decision itself: `catalog` is unreachable from a runner's
interpreter, which imports this file as a bare module with no `fused_render`
package on `sys.path`. But more to the point it MUST not — probing the mirror for
an arbitrary repo id would tell us which models a user downloads, and the whole
point of gating it to the curated list is that we never learn that.

**Stdlib only**, like `worker_base`, and for the same reason: this file is
imported by every runner's interpreter, so anything imported here becomes a
dependency of every backend forever.

**Validation is a trust boundary.** Everything below comes off a CDN, and the
failure that matters is not a 500 — it is a manifest that is plausible and wrong.
A size that lies puts a short blob under a real etag; a name that climbs out of
the snapshot writes a file wherever it likes; an etag with a slash in it does the
same inside `blobs/`. So every field is checked, and a rejection reads as NO
MIRROR rather than as an error: the caller's contract is that a mirror which is
down, misconfigured or serving junk costs a slower download and never a failed
one.
"""

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

#: The manifest shape this build understands. An unknown version reads as no
#: mirror, so a future manifest can change shape freely without an old build
#: reading it as the shape it expects.
SCHEMA = 1

BASE_ENV = "FUSED_MODEL_MIRROR"
OK_ENV = "FUSED_MODEL_MIRROR_OK"

#: A manifest is a few KB of names. Anything wildly larger is not one, and
#: reading a response into memory unbounded on the strength of an operator's
#: base URL is the one thing this client must not do.
MAX_MANIFEST_BYTES = 1 << 20

MANIFEST_TIMEOUT_S = 15.0

#: Every way of not getting a manifest, and `http.client.HTTPException` is in it
#: for a reason that is easy to miss: it is NOT an `OSError` and not a
#: `ValueError`, so `BadStatusLine` and `LineTooLong` out of `getresponse()` and
#: `IncompleteRead` off a truncated chunked body all escaped a guard written to
#: mean "the mirror did not answer". Escaping here is not a slower download, it
#: is a FAILED one — a mirror host misbehaving at the HTTP level took down a
#: download the Hub could have served. `worker_base._TRANSIENT` names the same
#: family for the same reason.
_UNREACHABLE = (urllib.error.URLError, OSError, ValueError,
                http.client.HTTPException)

_ALLOWED_SCHEMES = ("http", "https")
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX = re.compile(r"\A[0-9a-f]+\Z")
#: `org/name`, with nothing in either half that could address a different
#: object once it is pasted into a URL path.
_REPO_ID = re.compile(r"\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
#: ONE path segment, for the per-file manifest key. Deliberately narrower than
#: `_safe_name`, which validates a repo-RELATIVE path and therefore allows `/`:
#: here the name is a single URL path segment, so a `/` would address a
#: different object outright. Anchored on an alphanumeric first character so
#: `.`, `..` and a dotfile cannot be spelled at all, and the charset excludes
#: `?`, `#`, `%` and a space — each of which would let the segment truncate or
#: rewrite the rest of the path (`files/a?b.gguf/manifest.json` requests
#: `files/a`). Every curated GGUF filename is inside it.
_FILENAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
MAX_FILENAME_CHARS = 256


def _valid_base(raw):
    """`raw`, normalised and scheme-checked, or `""` if it cannot be a mirror.

    The scheme is checked HERE rather than left to `urlopen`, which happily
    opens `file://` and would then read a local path in answer to what looks
    like a network request. Pulled out of `base_url` so a caller that already
    HAS a base URL — `scripts/build_model_mirror.py --check` is given one on
    the command line, not through `FUSED_MODEL_MIRROR` — gets the identical
    check rather than a second copy of it.
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        return ""
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.netloc:
        return ""
    return base


def base_url():
    """The mirror's base URL, or `""` for "there is no mirror"."""
    return _valid_base(_env(BASE_ENV))


def allowed(model_id):
    """Whether this process may name `model_id` to the mirror.

    Both halves are required: a base URL to talk to, and the permission for THIS
    repo id — not a global "mirror is on" switch, because the probe itself is
    what would leak which models a user downloads (see the module docstring).
    """
    if not _REPO_ID.match(model_id or ""):
        return False
    return bool(base_url()) and _env(OK_ENV) == model_id


def manifest_url(model_id, base=None):
    """Where this repo's manifest lives, or `""` if there is no mirror.

    `base`, when given, is used INSTEAD of `FUSED_MODEL_MIRROR` — for a caller
    that is not a runner and has its own base URL to check against (a release
    gate given one on the command line), rather than the env-gated default
    every in-app caller uses.
    """
    base = base_url() if base is None else _valid_base(base)
    if not base or not _REPO_ID.match(model_id or ""):
        return ""
    return f"{base}/models/{model_id}/manifest.json"


def file_manifest_url(model_id, filename, base=None):
    """Where ONE file's manifest lives, or `""` if it cannot be addressed.

    `filename` becomes a URL path SEGMENT here and a filesystem name later, so
    it is validated before either — a `/` addresses a different object, `..`
    climbs the key space, and `?` or `#` truncate the rest of the path so that
    some other object answers for this one. `""` rather than an exception,
    because every way of not having a mirror reads the same to the caller.

    `base` overrides `FUSED_MODEL_MIRROR`, as in `manifest_url` above.
    """
    base = base_url() if base is None else _valid_base(base)
    if not base or not _REPO_ID.match(model_id or ""):
        return ""
    if not _safe_filename(filename):
        return ""
    return f"{base}/models/{model_id}/files/{filename}/manifest.json"


def blob_url(model_id, commit, etag, base=None):
    """Where one blob lives. Commit-pinned, and therefore immutable.

    That is what lets a blob be cached forever while the manifest above stays
    short-TTL: a re-upload lands under a new commit, so no old URL can ever be
    made to serve different bytes than it served before.

    `base` overrides `FUSED_MODEL_MIRROR`, as in `manifest_url` above.
    """
    base = base_url() if base is None else _valid_base(base)
    if not base:
        return ""
    return f"{base}/models/{model_id}/{commit}/{etag}"


def manifest(model_id):
    """The validated manifest for `model_id`, or None for "no mirror".

    None for every way of not having one — permission withheld, no base URL, a
    404, a 5xx, a host that does not answer, a body that is not JSON, a manifest
    this build does not understand, or one whose fields do not hold up. They all
    mean the same thing to the caller, and none of them is grounds for failing a
    download that the Hub can serve.
    """
    if not allowed(model_id):
        return None
    payload = fetch_json(manifest_url(model_id))
    if payload is None:
        return None
    return validate_manifest(payload, model_id)


def file_manifest(model_id, filename):
    """The validated manifest for ONE file of `model_id`, or None.

    A separate reader from `manifest` above rather than a flag on it, because it
    reads a document that makes a DIFFERENT claim: this one lists exactly one
    named file and asserts nothing about the repo, where that one asserts it
    lists the repo whole. Two claims, two readers, so relaxing this one cannot
    relax that one.

    **And this one has no completeness assertion to make.** `validate_manifest`
    requires `complete: true` not for tidiness but because of what its caller
    does next: `_mirror_snapshot` writes an AI-5k fetch record from the
    manifest's own file list, so an incomplete manifest would record a subset as
    whole and every later bring-up would be served a snapshot that cannot load,
    with nothing left that would refetch it. `download_file` — the only caller
    of this reader — writes NO fetch record, and never has. So there is no
    record to be self-certifying, and the worst a wrong manifest here can do is
    serve the wrong bytes for one file, which the sha256 in `file_meta` catches
    before anything is published into the cache. Requiring completeness anyway
    would not buy safety; it would only mean mirroring 147.81GB to serve 2.6GB.

    None for every way of not having one, exactly as above.
    """
    if not allowed(model_id) or not _safe_filename(filename):
        return None
    payload = fetch_json(file_manifest_url(model_id, filename))
    if payload is None:
        return None
    return validate_file_manifest(payload, model_id, filename)


def file_meta(model_id, man):
    """A drop-in for `worker_base._hub_file_meta`, backed by this manifest.

    Same signature and the same five keys, so `_segmented_fetch` cannot tell the
    difference — plus `sha256`, which the Hub does not give us and which is what
    lets the mirror path verify what it wrote (see `_FileFetch.finish`). Here we
    ARE the origin, so hf's "trust TLS and Content-Length" trade no longer
    holds: nobody else would notice if we shipped a bad byte.

    `url` and `location` are the same URL because there is no redirect to a
    presigned host — which also means `_FileFetch._cdn_token` would offer the
    Hub token to our own mirror, and the mirror path passes no token at all for
    exactly that reason.
    """
    by_name = {entry["name"]: entry for entry in man["files"]}
    commit = man["commit"]

    def meta(_repo_id, filename, _revision):
        entry = by_name[filename]
        url = blob_url(model_id, commit, entry["etag"])
        return {"url": url, "location": url, "etag": entry["etag"],
                "commit": commit, "size": entry["size"],
                "sha256": entry["sha256"]}

    return meta


# ------------------------------------------------------------------ validation


def fetch_json(url):
    """One manifest request, or None. The whole network half of this module.

    Shared by both readers so that the cap, the timeout, the identity encoding
    and the "every failure is None" rule are stated once — a second copy is how
    one reader comes to be missing a guard the other has. Public (no leading
    underscore) because it consults no env var and gates on nothing — a caller
    that already has a URL of its own (`scripts/build_model_mirror.py --check`)
    can drive the exact same request the client makes, rather than a copy of
    this function that could drift from it.
    """
    if not url:
        return None
    try:
        request = urllib.request.Request(
            url, headers={"Accept": "application/json",
                          # A gzipped body's length is not the body's length,
                          # and the cap below is a cap on what we read.
                          "Accept-Encoding": "identity"})
        with urllib.request.urlopen(request, timeout=MANIFEST_TIMEOUT_S) as response:
            # One byte past the cap, so a body that is exactly at the limit is
            # still distinguishable from one that ran over it.
            raw = response.read(MAX_MANIFEST_BYTES + 1)
    except _UNREACHABLE:
        return None
    if len(raw) > MAX_MANIFEST_BYTES:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _env(name):
    # Read at CALL time rather than captured at import: a worker's permission
    # arrives in its environment, and one process serves one download.
    return os.environ.get(name)


def _safe_name(name):
    """Whether `name` is a repo-relative path that stays inside the snapshot.

    A snapshot entry is created at `snapshots/<commit>/<name>`, so a name is a
    filesystem path we are about to write. `..`, an absolute path and a Windows
    separator are all ways of leaving that directory, and a manifest is not a
    place to accept any of them from.
    """
    if not isinstance(name, str) or not name or len(name) > 512:
        return False
    if name.startswith("/") or "\\" in name or ":" in name:
        return False
    parts = name.split("/")
    return all(part and part not in (".", "..") for part in parts)


def _safe_filename(filename):
    """Whether `filename` can be ONE segment of the per-file manifest's key.

    Stricter than `_safe_name` on purpose (see `_FILENAME`): that one validates
    a repo-relative path, which may contain `/`, and this one is a single URL
    path segment where a `/` addresses a different object entirely.
    """
    return (isinstance(filename, str) and 0 < len(filename) <= MAX_FILENAME_CHARS
            and bool(_FILENAME.match(filename)))


def _safe_etag(etag):
    """Whether `etag` can be a blob FILENAME.

    hf's cache names a blob by its etag, which is a git blob sha1 for a small
    file and a sha256 for an LFS one — hex either way, and hex is also what
    makes it unable to name a directory or climb out of `blobs/`.
    """
    return (isinstance(etag, str) and 8 <= len(etag) <= 128
            and bool(_HEX.match(etag)))


def validate_manifest(payload, model_id):
    """The manifest as this build uses it, or None.

    Public alias for what `manifest()` above calls internally, so a caller that
    already has a payload of its own — fetched with its own base URL rather
    than through `FUSED_MODEL_MIRROR` — gets the SAME schema check the runtime
    client applies, not a second implementation of it. That matters more than
    convenience: a drift check
    (`scripts/build_model_mirror.py --check`) that accepted a manifest this
    validator refuses would report a target published when nothing that ever
    runs this code can actually read it.

    Normalised on the way through — the caller gets `{"commit", "files"}` with
    every entry already checked — so no reader downstream has to ask whether a
    field it is about to use was validated.
    """
    if not isinstance(payload, dict):
        return None
    if not _known_schema(payload):
        return None
    if payload.get("repo") != model_id:
        # A manifest that names a different repo is either a misconfigured
        # distribution or a rewritten URL, and installing it under this id would
        # put one model's weights in another model's cache folder.
        return None
    commit = payload.get("commit")
    if not isinstance(commit, str) or not _COMMIT.match(commit):
        # Lower-case 40 hex, because that string IS the snapshot directory name
        # hf resolves and `_commit_of` reads back.
        return None
    if payload.get("complete") is not True:
        # **The manifest has to SAY it lists the whole repo at this commit**, and
        # `is not True` rather than a truthiness test, so a `1` or a `"yes"` from
        # some other generator is not taken as the assertion this is.
        #
        # The client cannot check completeness for itself: the only independent
        # authority on what a repo contains is the Hub, and asking it is the one
        # thing this feature exists to avoid. So the proof lives on the
        # GENERATOR side — `scripts/build_model_mirror.py` verifies the snapshot
        # against the Hub's own listing at this commit, on a build machine where
        # talking to the Hub costs nothing — and this field is where that proof
        # is recorded.
        #
        # It matters because of what the client does NEXT: it writes a fetch
        # record (AI-5k) saying this scope is complete on disk. An incomplete
        # manifest would make that record self-certifying — a manifest missing
        # `config.json` downloads a subset, records the subset as whole, and
        # every later bring-up is then served a snapshot that cannot load, with
        # nothing left that would ever refetch it. A slow download is a cost; a
        # permanently broken model is not.
        return None
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        return None
    validated, seen = [], set()
    for entry in entries:
        checked = _validated_entry(entry)
        if checked is None or checked["name"] in seen:
            return None
        seen.add(checked["name"])
        validated.append(checked)
    return {"commit": commit, "files": validated}


def validate_file_manifest(payload, model_id, filename):
    """The per-file manifest as this build uses it, or None (AI-5m).

    Public alias for what `file_manifest()` above calls internally — see
    `validate_manifest`'s docstring for why a caller with its own payload gets
    this function rather than a copy of it.

    Same shape out as `validate_manifest` — `{"commit", "files"}` — so `file_meta` and
    `_segmented_fetch` cannot tell the two documents apart, and the same field
    vocabulary going in. Two differences, both deliberate:

    * EXACTLY one entry, and its `name` must be the file that was asked for. The
      requested name is this object's whole identity: a manifest answering with
      some other file would install those bytes under the name the caller wants,
      and a second entry means either a repo manifest served at a per-file key
      or a generator this reader does not understand.
    * `complete` is not read, in either direction. See `file_manifest` for why
      the assertion is unnecessary here rather than merely inconvenient: nothing
      on this path writes a fetch record, so there is no record for a partial
      answer to certify.
    """
    if not isinstance(payload, dict):
        return None
    if not _known_schema(payload) or payload.get("repo") != model_id:
        return None
    commit = payload.get("commit")
    if not isinstance(commit, str) or not _COMMIT.match(commit):
        return None
    entries = payload.get("files")
    if not isinstance(entries, list) or len(entries) != 1:
        return None
    entry = _validated_entry(entries[0])
    if entry is None or entry["name"] != filename:
        return None
    return {"commit": commit, "files": [entry]}


def _known_schema(payload):
    """Whether this build understands the manifest's shape.

    `isinstance(True, int)` is True and `True == 1`, so a boolean schema would
    otherwise pass as version 1. A manifest this build does not understand reads
    as no mirror, which is what lets the shape change.
    """
    schema = payload.get("schema")
    return (isinstance(schema, int) and not isinstance(schema, bool)
            and schema == SCHEMA)


def _validated_entry(entry):
    """One file entry, normalised, or None. Shared by both readers.

    Shared so the two documents cannot come to disagree about what a valid entry
    IS — the etag names a blob, the name is written into a snapshot, and the
    digest is the mirror path's only proof of what it wrote.
    """
    if not isinstance(entry, dict):
        return None
    name, etag = entry.get("name"), entry.get("etag")
    size, digest = entry.get("size"), entry.get("sha256")
    if not _safe_name(name) or not _safe_etag(etag):
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        # Zero is allowed: an empty file is legal on the Hub, hf caches it like
        # any other, and the fetcher already handles a zero-length segment
        # (`_chunks(0)` yields one piece that is complete on arrival). Rejecting
        # the MANIFEST over it would take a whole model off the mirror because
        # of a file with nothing in it.
        return None
    if not isinstance(digest, str) or not _SHA256.match(digest):
        return None
    return {"name": name, "etag": etag, "size": size, "sha256": digest}
