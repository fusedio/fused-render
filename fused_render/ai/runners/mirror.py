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

_ALLOWED_SCHEMES = ("http", "https")
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX = re.compile(r"\A[0-9a-f]+\Z")
#: `org/name`, with nothing in either half that could address a different
#: object once it is pasted into a URL path.
_REPO_ID = re.compile(r"\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")


def base_url():
    """The mirror's base URL, or `""` for "there is no mirror".

    The scheme is checked HERE rather than left to `urlopen`, which happily
    opens `file://` and would then read a local path in answer to what looks
    like a network request.
    """
    base = (_env(BASE_ENV) or "").strip().rstrip("/")
    if not base:
        return ""
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in _ALLOWED_SCHEMES or not parts.netloc:
        return ""
    return base


def allowed(model_id):
    """Whether this process may name `model_id` to the mirror.

    Both halves are required: a base URL to talk to, and the permission for THIS
    repo id — not a global "mirror is on" switch, because the probe itself is
    what would leak which models a user downloads (see the module docstring).
    """
    if not _REPO_ID.match(model_id or ""):
        return False
    return bool(base_url()) and _env(OK_ENV) == model_id


def manifest_url(model_id):
    """Where this repo's manifest lives, or `""` if there is no mirror."""
    base = base_url()
    if not base or not _REPO_ID.match(model_id or ""):
        return ""
    return f"{base}/models/{model_id}/manifest.json"


def blob_url(model_id, commit, etag):
    """Where one blob lives. Commit-pinned, and therefore immutable.

    That is what lets a blob be cached forever while the manifest above stays
    short-TTL: a re-upload lands under a new commit, so no old URL can ever be
    made to serve different bytes than it served before.
    """
    base = base_url()
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
    url = manifest_url(model_id)
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
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if len(raw) > MAX_MANIFEST_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return _validated(payload, model_id)


def files(man):
    """The manifest's file entries, in manifest order."""
    return list(man["files"])


def names(man):
    """Every filename the mirror holds for this repo."""
    return [entry["name"] for entry in man["files"]]


def total_bytes(man):
    """What a full fetch of this manifest adds up to, for the progress row."""
    return sum(entry["size"] for entry in man["files"]) or None


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


def _safe_etag(etag):
    """Whether `etag` can be a blob FILENAME.

    hf's cache names a blob by its etag, which is a git blob sha1 for a small
    file and a sha256 for an LFS one — hex either way, and hex is also what
    makes it unable to name a directory or climb out of `blobs/`.
    """
    return (isinstance(etag, str) and 8 <= len(etag) <= 128
            and bool(_HEX.match(etag)))


def _validated(payload, model_id):
    """The manifest as this build uses it, or None.

    Normalised on the way through — the caller gets `{"commit", "files"}` with
    every entry already checked — so no reader downstream has to ask whether a
    field it is about to use was validated.
    """
    if not isinstance(payload, dict):
        return None
    schema = payload.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != SCHEMA:
        # `isinstance(True, int)` is True and `True == 1`, so a boolean schema
        # would otherwise pass as version 1. A manifest this build does not
        # understand reads as no mirror, which is what lets the shape change.
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
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        return None
    validated, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        name, etag = entry.get("name"), entry.get("etag")
        size, digest = entry.get("size"), entry.get("sha256")
        if not _safe_name(name) or name in seen:
            return None
        if not _safe_etag(etag):
            return None
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            return None
        if not isinstance(digest, str) or not _SHA256.match(digest):
            return None
        seen.add(name)
        validated.append({"name": name, "etag": etag, "size": size,
                          "sha256": digest})
    return {"commit": commit, "files": validated}
