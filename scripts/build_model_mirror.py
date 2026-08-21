#!/usr/bin/env python3
"""Build the model mirror's objects from a real hf cache directory (SPEC AI-5l).

The mirror serves two shapes per model — a manifest, and one blob per distinct
etag under the commit — and this is what produces both:

    <prefix>/models/<org>/<name>/manifest.json
    <prefix>/models/<org>/<name>/<commit>/<etag>

Plus a third for ONE named file (AI-5m), which is how a GGUF gets mirrored at
all:

    <prefix>/models/<org>/<name>/files/<filename>/manifest.json

`llama_text.download` fetches one quantization out of a repo publishing dozens,
and the repo manifest cannot describe that: it asserts it lists the repo WHOLE at
the commit, so publishing one 2.6GB file the repo-wide way would mean holding and
uploading all 147.81GB of `unsloth/Qwen3.5-9B-GGUF`. The per-file manifest lists
exactly one file, carries no `complete` flag (there is no repo claim to prove, so
`--fetch-missing` and the Hub listing are both skipped), and points at a blob in
the SHARED key space above — so a repo published both ways stores one copy of
each blob.

**Everything is READ OUT OF A CACHE DIRECTORY hf itself produced**, never
transcribed. The commit comes from `refs/main`, the etags are the blob
FILENAMES, the sizes and the sha256 digests come from the blobs. That is the
whole reason this script exists rather than a hand-written JSON file: a
transcribed etag, commit or layout is the one part of this feature that would
fail silently and permanently — the client would download bytes, file them under
a name hf does not use, and every later load would miss the cache while the
download reported success. Generated from the cache, all three are correct by
construction, and `tests/test_build_model_mirror.py` round-trips the result
through the client that reads it.

**Dry run by default.** Nothing is uploaded without `--upload`, because the
normal use of this script is to see what a release WOULD publish.

**Idempotent, and the key is why.** A blob's key contains both the commit and
the etag, so a key that exists already holds exactly the bytes it would be
given — existence is the whole check, and an S3 `head-object` is cheaper than
re-reading a 4.6GB shard. The manifest is mutable and is always re-uploaded.

Usage:

    python scripts/build_model_mirror.py --cache ~/.cache/huggingface/hub
    python scripts/build_model_mirror.py --model org/name --json manifest.json
    python scripts/build_model_mirror.py --model org/name --file model-Q4_K_M.gguf
    python scripts/build_model_mirror.py --fetch-missing        # download first
    python scripts/build_model_mirror.py --upload s3://bucket/prefix

Reading a cache needs nothing but the stdlib. `--fetch-missing` needs
`huggingface_hub`, and `--upload` needs the `aws` CLI on PATH; both are imported
or invoked only when asked for, so the common case runs anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

#: Kept in step with `fused_render/ai/runners/mirror.py`. A manifest this script
#: writes must be one that client accepts, and `test_build_model_mirror.py`
#: asserts the round trip rather than trusting the two constants to agree.
SCHEMA = 1

HASH_BLOCK_BYTES = 1024 * 1024
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX = re.compile(r"\A[0-9a-f]+\Z")
#: Kept in step with `mirror._FILENAME`, and checked HERE for the reason this
#: whole script exists: the name becomes one URL path segment of the per-file
#: manifest's key, so publishing a name the client refuses produces an object
#: nothing can ever read — silently, since a client that finds no mirror just
#: downloads from the Hub.
_FILENAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def default_cache() -> str:
    """Where hf keeps its hub cache, by hf's own rules."""
    if os.environ.get("HF_HUB_CACHE"):
        return os.environ["HF_HUB_CACHE"]
    home = os.environ.get("HF_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface")
    return os.path.join(home, "hub")


def repo_folder(cache: str, repo_id: str) -> str:
    """hf's folder name for a repo. Its own rule: `/` becomes `--`."""
    return os.path.join(cache, "models--" + repo_id.replace("/", "--"))


def read_commit(folder: str, ref: str = "main") -> str:
    """The commit `refs/<ref>` points at.

    From the ref file rather than from whatever single directory happens to be
    under `snapshots/` — a cache that holds two revisions of a repo has two, and
    picking one by listing order is how a manifest comes to describe a commit
    nobody asked for.
    """
    with open(os.path.join(folder, "refs", ref), encoding="utf-8") as handle:
        commit = handle.read().strip()
    if not _COMMIT.match(commit):
        raise ValueError(f"{folder}: refs/{ref} is not a 40-hex commit: {commit!r}")
    return commit


#: hf's own bookkeeping, written INSIDE a snapshot directory by newer
#: `snapshot_download` versions (`.cache/huggingface/download/*.metadata`). Not
#: repo content, and its files do not resolve to blobs — a walk that treated
#: them as repo files would abort every real model on a modern cache.
_HF_PRIVATE = ".cache"


def wire_name(path: str, root: str) -> str:
    r"""`path`, relative to `root`, as a MANIFEST name.

    **The one boundary where a filesystem path becomes a name on the wire**, and
    the only place in this script that converts separators. A manifest name is
    `/`-separated on every platform, because it is consumed as both a URL path
    segment and a filesystem path, and the client validates it as `/`-only —
    a `\` there is a traversal character and gets the whole manifest rejected.

    So on Windows this conversion is load-bearing: `os.path.relpath` returns
    `tokenizer\vocab.json`, and publishing that would produce a manifest the
    generator's own client refuses. Nothing downstream converts back,
    deliberately: both POSIX and Windows accept `/` in every filesystem call the
    client makes, so one conversion here is the whole story rather than a
    per-call `normpath` sprinkled through the fetcher.
    """
    return os.path.relpath(path, root).replace(os.sep, "/")


def hub_listing(repo_id: str, commit: str) -> set:
    """Every filename the Hub says this repo holds at `commit`.

    The one authority on what a repo CONTAINS, and the reason `read_manifest`
    can promise completeness at all. Asking it here is free of the concern that
    governs the client: this runs on a build machine, not in a user's runner, so
    a Hub round trip tells huggingface.co nothing about any user.
    """
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo_id, revision=commit)
    return {sibling.rfilename for sibling in getattr(info, "siblings", None) or []}


def read_manifest(cache: str, repo_id: str, ref: str = "main", listing=None) -> dict:
    """The manifest for one repo, read entirely out of its cache directory.

    Every snapshot entry is a link (or a copy, on a filesystem without symlinks)
    of a blob named by its etag, so the entry's REAL path gives both: the etag is
    the basename, and the bytes to hash are right there. A repo publishing the
    same bytes under two names yields two entries sharing one etag, which is what
    the client turns back into one download and two links.

    **A snapshot that is missing a file the repo HAS is refused**, checked
    against `listing` — because a cache directory existing says nothing about it
    being whole. `torch_image._download` fetches its image models with
    `allow_patterns=recipe["keep"]`, so any machine that ever loaded one holds a
    deliberately partial cache for it, and a manifest of that subset is a
    permanently broken model on every client that installs it: the client selects
    everything the manifest lists, gets the subset, and records it as complete.
    Which is exactly the "fails silently and permanently" class this script
    exists to prevent, so the check belongs here and the resulting manifest says
    `complete: true` — the assertion the client requires before it will trust the
    file list enough to write a fetch record from it.
    """
    folder = repo_folder(cache, repo_id)
    commit = read_commit(folder, ref)
    root = os.path.join(folder, "snapshots", commit)
    if not os.path.isdir(root):
        raise ValueError(f"{repo_id}: no snapshot for {commit} in {folder}")
    files, digests = [], {}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name != _HF_PRIVATE]
        for name in sorted(names):
            path = os.path.join(dirpath, name)
            relative = wire_name(path, root)
            blob = os.path.realpath(path)
            etag = os.path.basename(blob)
            if not _HEX.match(etag):
                raise ValueError(
                    f"{repo_id}: {relative} resolves to {etag!r}, which is not an "
                    f"etag — is this snapshot a `local_dir` copy rather than a "
                    f"cache entry?")
            # No lower bound: an empty file is legal on the Hub, hf caches it
            # like any other, and refusing the repo over one would take a whole
            # model off the mirror because of a file with nothing in it.
            size = os.path.getsize(blob)
            # Hashed once per BLOB, not once per name: two names sharing an etag
            # are the same bytes by definition.
            if etag not in digests:
                digests[etag] = (sha256_of(blob), blob)
            files.append({"name": relative, "etag": etag, "size": size,
                          "sha256": digests[etag][0]})
    if not files:
        raise ValueError(f"{repo_id}: the snapshot for {commit} is empty")
    files.sort(key=lambda entry: entry["name"])
    # Refused, never trimmed to what is there: a manifest of "whatever this
    # machine happens to hold" is the bug, not the fix.
    expected = (listing or hub_listing)(repo_id, commit)
    missing = sorted(expected - {entry["name"] for entry in files})
    if missing:
        raise ValueError(
            f"{repo_id}: the cached snapshot for {commit[:12]} is missing "
            f"{len(missing)} of the {len(expected)} files the Hub lists "
            f"({', '.join(missing[:4])}{', …' if len(missing) > 4 else ''}). "
            f"Re-run with --fetch-missing; a partial snapshot must never be "
            f"published, because a client cannot tell one from a whole repo.")
    return {"schema": SCHEMA, "repo": repo_id, "commit": commit,
            "complete": True, "files": files}


def read_file_manifest(cache: str, repo_id: str, filename: str,
                       ref: str = "main") -> dict:
    """The manifest for ONE file of a repo, read out of its cache directory.

    **A partial cache is the normal input here**, which is the whole difference
    from `read_manifest` above. That function refuses a snapshot missing anything
    the Hub lists, because the manifest it writes claims to describe the repo
    whole and a client turns that claim into an AI-5k fetch record. This one
    claims one named file, so there is nothing to prove: no `complete` flag, no
    Hub listing, and `--fetch-missing` fetches the one file rather than the repo.
    That is not a loosening for convenience — see `mirror.file_manifest` — the
    client that reads this writes no fetch record, so no partial answer can be
    recorded as whole.

    Everything else is read exactly as the repo mode reads it: the commit from
    `refs/<ref>`, the etag from the blob FILENAME the snapshot entry resolves to,
    the size and digest from the blob itself.
    """
    if not _FILENAME.match(filename or ""):
        raise ValueError(
            f"{repo_id}: {filename!r} is not a publishable file name — the "
            f"per-file manifest's key is `files/<name>/manifest.json`, one path "
            f"segment, and the client refuses anything else")
    folder = repo_folder(cache, repo_id)
    commit = read_commit(folder, ref)
    root = os.path.join(folder, "snapshots", commit)
    path = os.path.join(root, filename)
    if not os.path.isfile(path):
        raise ValueError(
            f"{repo_id}: {filename} is not in the cached snapshot for "
            f"{commit[:12]}. Re-run with --fetch-missing.")
    blob = os.path.realpath(path)
    etag = os.path.basename(blob)
    if not _HEX.match(etag):
        raise ValueError(
            f"{repo_id}: {filename} resolves to {etag!r}, which is not an etag "
            f"— is this snapshot a `local_dir` copy rather than a cache entry?")
    return {"schema": SCHEMA, "repo": repo_id, "commit": commit,
            "files": [{"name": wire_name(path, root), "etag": etag,
                       "size": os.path.getsize(blob), "sha256": sha256_of(blob)}]}


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(HASH_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def plan(cache: str, manifest: dict, filename: str | None = None) -> list[dict]:
    """One upload per distinct etag, plus the manifest itself, last.

    Last deliberately: the manifest is what makes a model DOWNLOADABLE, so
    publishing it before its blobs would advertise objects that are not there
    yet — a manifest promising bytes the mirror does not hold, which the client
    can only discover mid-download.

    `filename` publishes the PER-FILE manifest key instead of the repo one — an
    explicit argument rather than something inferred from the manifest's shape,
    because which claim is being published is the caller's decision and a wrong
    guess would file one document under the other's key. The BLOBS are keyed
    identically either way, which is what makes a repo published both ways store
    one copy of each blob.
    """
    folder = repo_folder(cache, manifest["repo"])
    uploads, seen = [], set()
    for entry in manifest["files"]:
        if entry["etag"] in seen:
            continue
        seen.add(entry["etag"])
        uploads.append({
            "key": f"models/{manifest['repo']}/{manifest['commit']}/{entry['etag']}",
            "path": os.path.join(folder, "blobs", entry["etag"]),
            "size": entry["size"],
            "immutable": True,
        })
    key = f"models/{manifest['repo']}/manifest.json"
    if filename:
        key = f"models/{manifest['repo']}/files/{filename}/manifest.json"
    uploads.append({"key": key, "path": None, "size": None, "immutable": False})
    return uploads


# ------------------------------------------------------------------- the actions


def fetch_missing(repo_id: str, cache: str) -> None:
    """Download the repo into `cache` with hf's own downloader.

    hf's, not ours: the point of reading a cache directory is that hf produced
    it, and a manifest generated from a cache OUR fetcher wrote would be
    checking our own work.
    """
    from huggingface_hub import snapshot_download

    snapshot_download(repo_id, cache_dir=cache)


def fetch_missing_file(repo_id: str, filename: str, cache: str) -> None:
    """Download ONE file into `cache` with hf's own downloader.

    The repo-wide `fetch_missing` is not an option here: for a GGUF repo it would
    fetch every quantization, which is the 147.81GB this mode exists to avoid.
    """
    from huggingface_hub import hf_hub_download

    hf_hub_download(repo_id, filename, cache_dir=cache)


def upload(uploads: list[dict], destination: str, manifest: dict,
           run=subprocess.run) -> list[str]:
    """Put the objects there with the `aws` CLI. Returns the keys it wrote.

    A blob whose key already exists is skipped: the key names the commit and the
    etag, so it cannot hold anything but these bytes (see the module docstring).
    """
    if not shutil.which("aws"):
        raise SystemExit("--upload needs the `aws` CLI on PATH")
    written = []
    for item in uploads:
        target = destination.rstrip("/") + "/" + item["key"]
        if item["immutable"] and _exists(target, run):
            print(f"  = {item['key']} (already uploaded)")
            continue
        if item["path"] is None:
            body = json.dumps(manifest, indent=2).encode() + b"\n"
            run(["aws", "s3", "cp", "-", target, "--content-type",
                 "application/json", "--cache-control", "max-age=60"],
                input=body, check=True)
        else:
            run(["aws", "s3", "cp", item["path"], target, "--cache-control",
                 "public, max-age=31536000, immutable"], check=True)
        print(f"  + {item['key']}")
        written.append(item["key"])
    return written


def _exists(target: str, run) -> bool:
    done = run(["aws", "s3", "ls", target], capture_output=True, check=False)
    return done.returncode == 0 and bool((done.stdout or b"").strip())


def suggested_ids() -> list[str]:
    """The curated list, from the app rather than from a copy of it."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fused_render.ai import catalog

    return sorted(catalog.all_suggested_ids())


def suggested_targets() -> list[tuple]:
    """Every suggested model as a `(repo_id, filename or None)` publish target.

    A curated llama.cpp id is a `.gguf` FILENAME rather than a repo id — one repo
    publishes many quantizations, so the app keys that curation by file — and
    `models--<filename>` is not a cache folder, so the whole-repo mode could only
    ever print SKIPPED for it. A default run therefore could not succeed and
    always exited 1, once for every llama.cpp row. Those ids become per-file
    targets against the recipe's repo, which is also the only shape publishable
    at all: the repo is 147.81GB and the file is 2.6GB.

    Everything else is already a repo id and stays a whole-repo target.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fused_render.ai import catalog
    from fused_render.ai.runners import formats

    targets = []
    for model_id in sorted(catalog.all_suggested_ids()):
        recipe = formats.GGUF_RECIPES.get(model_id)
        targets.append((recipe["repo"], recipe["file"]) if recipe
                       else (model_id, None))
    return targets


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache", default=None,
                        help="hf hub cache to read (default: hf's own location)")
    parser.add_argument("--model", action="append", dest="models", default=None,
                        help="repo id; repeatable. Default: every suggested model")
    parser.add_argument("--file", action="append", dest="files", default=None,
                        help="publish ONE file of --model rather than the whole "
                             "repo (AI-5m); repeatable, needs exactly one --model")
    parser.add_argument("--ref", default="main", help="the ref to publish")
    parser.add_argument("--fetch-missing", action="store_true",
                        help="ask hf to complete each model's cache first")
    parser.add_argument("--json", default=None,
                        help="write each manifest into this directory as well")
    parser.add_argument("--upload", default=None, metavar="S3URI",
                        help="upload instead of only printing the plan")
    args = parser.parse_args(argv)

    cache = args.cache or default_cache()
    if args.files:
        # `--file` names a file OF a repo, and of ONE repo: spread over the
        # suggested list it would ask every model for somebody else's GGUF.
        if not args.models or len(args.models) != 1:
            parser.error("--file needs exactly one --model")
        targets = [(args.models[0], filename) for filename in args.files]
    elif args.models:
        targets = [(repo_id, None) for repo_id in args.models]
    else:
        targets = suggested_targets()
    failed = []
    for repo_id, filename in targets:
        label = repo_id if filename is None else f"{repo_id} {filename}"
        if args.fetch_missing:
            # UNCONDITIONALLY, not only when the folder is absent. "The folder
            # exists" was the old condition and it is not the same question: a
            # scoped download (`allow_patterns`) leaves a folder that exists and
            # holds a tenth of the repo, and the manifest built from it would be
            # a permanently broken model. For a cache that IS complete this costs
            # one etag revalidation, which is nothing on a release script.
            #
            # A per-file target fetches the one FILE, because the repo behind it
            # is the 147.81GB this mode exists to avoid.
            print(f"{label}: completing the cache in {cache}")
            if filename is None:
                fetch_missing(repo_id, cache)
            else:
                fetch_missing_file(repo_id, filename, cache)
        try:
            manifest = (read_manifest(cache, repo_id, args.ref) if filename is None
                        else read_file_manifest(cache, repo_id, filename, args.ref))
        except (OSError, ValueError) as error:
            # Reported and skipped, not fatal: one model missing from a machine's
            # cache must not stop the other twenty from being published.
            print(f"{label}: SKIPPED — {error}")
            failed.append(label)
            continue
        uploads = plan(cache, manifest, filename)
        total = sum(item["size"] or 0 for item in uploads)
        print(f"{label} @ {manifest['commit'][:12]} — {len(manifest['files'])} "
              f"files, {len(uploads) - 1} blobs, {total / 1e9:.2f} GB")
        if args.json:
            os.makedirs(args.json, exist_ok=True)
            # The filename is in the OUTPUT name too: two targets in one repo
            # write two manifests, and a shared name would leave whichever ran
            # last on disk under a name describing the other.
            stem = repo_id.replace("/", "--")
            if filename is not None:
                stem = f"{stem}--{filename}"
            out = os.path.join(args.json, stem + ".manifest.json")
            with open(out, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2)
            print(f"  wrote {out}")
        if args.upload:
            upload(uploads, args.upload, manifest)
        else:
            for item in uploads:
                print(f"  would upload {item['key']}")
    # ANY skip is a non-zero exit, not only a total wipeout. Publishing 19 of 20
    # with a green exit is how a suggested model goes missing from the mirror
    # unnoticed — its download quietly stays on the Hub, which is invisible by
    # design, so the exit code is the only place it can be caught. The loop
    # itself stays tolerant: one absent model must not stop the other nineteen.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
