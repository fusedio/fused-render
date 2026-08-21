"""The build script and the client, checked against each other (SPEC AI-5l).

The mirror has two halves written months apart in different languages of thought:
a script that reads an hf cache directory and emits a manifest, and a stdlib
client in a runner venv that reads that manifest and reproduces the cache
directory. Nothing else keeps them honest — a transcribed etag, a commit read
from the wrong place, a size measured on the symlink rather than the blob would
all produce a manifest that looks right, downloads bytes, and files them under a
name hf never uses, so every later load misses the cache while the download
reports success.

So the test that matters here is a ROUND TRIP: build a manifest from a fixture
cache directory, serve it and its blobs over the same local HTTP harness the
other mirror tests use, download it through `worker_base.download_snapshot`, and
compare what lands against what we started from. Nothing reaches huggingface.co,
S3 or CloudFront.
"""
import hashlib
import importlib.util
import json
import os

import pytest
# `no_egress` is imported for its SIDE EFFECT — it is an autouse fixture, and
# binding the name here installs it for every test in this file. THIS is the
# file that needed it: its round-trip test drove `download_snapshot`, whose Hub
# fallback is real `huggingface_hub`, and on Windows (where the mirror cannot
# run at all) that made a real request to huggingface.co and failed on its 401.
from test_ai_hub_fetch import _fresh_base, _start_server, no_egress  # noqa: F401
from test_ai_model_mirror import _fresh_mirror

SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "build_model_mirror.py",
)

COMMIT = "a1b2c3d4" * 5


def _load_script():
    spec = importlib.util.spec_from_file_location("build_model_mirror", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_script()


def _cache(tmp_path, contents, repo_id="org/m", commit=COMMIT, ref="main"):
    """An hf cache directory, built the way hf builds one.

    Blobs named by their etag, snapshot entries as RELATIVE symlinks into
    `blobs/`, and `refs/<ref>` naming the commit — because "the layout hf
    produced" is the whole input to the script, and a fixture that took a
    shortcut would test a layout nothing produces.
    """
    cache = tmp_path / "hub"
    folder = cache / ("models--" + repo_id.replace("/", "--"))
    (folder / "blobs").mkdir(parents=True)
    (folder / "refs").mkdir(parents=True)
    (folder / "refs" / ref).write_text(commit)
    snapshot = folder / "snapshots" / commit
    for name, body in contents.items():
        etag = hashlib.sha256(body).hexdigest()
        (folder / "blobs" / etag).write_bytes(body)
        entry = snapshot / name
        entry.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(folder / "blobs" / etag, entry.parent), entry)
    return str(cache)


def _listing(*names):
    """A stand-in for the Hub's file listing at a commit.

    Injected in every test, so nothing here reaches huggingface.co — but
    `read_manifest` requires one, because "this manifest lists the whole repo"
    is a claim only the Hub can settle and the build machine is the right place
    to settle it.
    """
    def listing(repo_id, commit):
        return set(names)

    return listing


CONTENTS = {
    "config.json": b'{"model_type": "test"}',
    "model.safetensors": hashlib.sha256(b"weights").digest() * 6250,  # 200_000 B
    "tokenizer/vocab.json": b'{"a": 1}',
}


# -- what the script reads out of a cache ----------------------------------------


def test_the_manifest_is_read_out_of_the_cache_not_transcribed(script, tmp_path):
    """Commit from `refs/main`, etags from the blob FILENAMES, sizes and digests
    from the blobs. All three correct by construction, which is the point."""
    cache = _cache(tmp_path, CONTENTS)

    manifest = script.read_manifest(cache, "org/m",
                                    listing=_listing(*CONTENTS))

    assert manifest["schema"] == 1
    assert manifest["complete"] is True
    assert manifest["repo"] == "org/m"
    assert manifest["commit"] == COMMIT
    assert [entry["name"] for entry in manifest["files"]] == [
        "config.json", "model.safetensors", "tokenizer/vocab.json"]
    for entry in manifest["files"]:
        body = CONTENTS[entry["name"]]
        assert entry["size"] == len(body)
        assert entry["sha256"] == hashlib.sha256(body).hexdigest()
        # The etag is the blob's own filename, which is what makes the layout the
        # client writes the layout hf reads.
        assert entry["etag"] == hashlib.sha256(body).hexdigest()


def test_the_commit_comes_from_the_ref_not_from_listing_snapshots(script, tmp_path):
    """A cache holding two revisions has two snapshot directories, and picking
    one by listing order publishes a commit nobody asked for."""
    cache = _cache(tmp_path, CONTENTS)
    stale = os.path.join(cache, "models--org--m", "snapshots", "b" * 40)
    os.makedirs(stale)

    assert script.read_manifest(
        cache, "org/m", listing=_listing(*CONTENTS))["commit"] == COMMIT


def test_two_names_for_one_blob_are_one_upload_and_two_entries(script, tmp_path):
    """A repo really does publish the same bytes twice. One etag is one blob, so
    it is uploaded once and the manifest names it twice — which is exactly what
    the client turns back into one download and two links."""
    body = b"shared bytes" * 100
    cache = _cache(tmp_path, {"a.bin": body, "b.bin": body})

    manifest = script.read_manifest(cache, "org/m",
                                    listing=_listing("a.bin", "b.bin"))
    uploads = script.plan(cache, manifest)

    assert len(manifest["files"]) == 2
    assert len({entry["etag"] for entry in manifest["files"]}) == 1
    blobs = [item for item in uploads if item["immutable"]]
    assert len(blobs) == 1, [item["key"] for item in uploads]


def test_the_manifest_is_uploaded_last(script, tmp_path):
    """Published first, it would advertise blobs that are not there yet — a
    manifest promising bytes the mirror does not hold, which a client can only
    discover mid-download."""
    cache = _cache(tmp_path, CONTENTS)
    uploads = script.plan(cache, script.read_manifest(
        cache, "org/m", listing=_listing(*CONTENTS)))

    assert uploads[-1]["key"] == "models/org/m/manifest.json"
    assert not any(item["key"].endswith("manifest.json") for item in uploads[:-1])


def test_a_snapshot_that_is_not_a_cache_entry_is_refused(script, tmp_path):
    """A `local_dir` download is real files, not links into `blobs/`, so there
    are no etags to read. Refused loudly rather than published with a filename
    where an etag belongs."""
    cache = _cache(tmp_path, CONTENTS)
    entry = os.path.join(cache, "models--org--m", "snapshots", COMMIT, "config.json")
    os.unlink(entry)
    with open(entry, "wb") as handle:
        handle.write(b"{}")

    with pytest.raises(ValueError, match="not an"):
        script.read_manifest(cache, "org/m", listing=_listing(*CONTENTS))


def test_a_repo_the_cache_does_not_hold_is_skipped_but_the_run_fails(script,
                                                                     tmp_path,
                                                                     monkeypatch,
                                                                     capsys):
    """Skipping one model must not stop the other twenty — and must not exit 0.

    Publishing 19 of 20 with a green exit is how a suggested model goes missing
    from the mirror unnoticed: its download quietly stays on the Hub, which is
    invisible BY DESIGN, so the exit code is the only place this can be caught.
    """
    cache = _cache(tmp_path, CONTENTS)
    monkeypatch.setattr(script, "hub_listing", _listing(*CONTENTS))

    code = script.main(["--cache", cache, "--model", "org/m",
                        "--model", "org/never-downloaded"])

    out = capsys.readouterr().out
    assert code == 1, "an incomplete publish exited green"
    assert "org/never-downloaded: SKIPPED" in out
    # …and the loop carried on: the model that WAS there is still planned.
    assert "would upload models/org/m/manifest.json" in out


def test_nothing_is_uploaded_without_being_asked(script, tmp_path, monkeypatch,
                                                 capsys):
    """Dry run is the default, because the normal use of this script is to see
    what a release WOULD publish."""
    cache = _cache(tmp_path, CONTENTS)

    def no(*args, **kwargs):
        raise AssertionError("a dry run shelled out to something")

    monkeypatch.setattr(script.subprocess, "run", no)
    monkeypatch.setattr(script.shutil, "which", no)
    monkeypatch.setattr(script, "hub_listing", _listing(*CONTENTS))

    assert script.main(["--cache", cache, "--model", "org/m"]) == 0
    assert "would upload" in capsys.readouterr().out


def test_an_upload_skips_a_blob_whose_key_is_already_there(script, tmp_path,
                                                           monkeypatch):
    """A blob's key names the commit AND the etag, so a key that exists holds
    exactly the bytes it would be given. Existence is the whole check — a
    re-read of a 4.6GB shard to prove it is not."""
    cache = _cache(tmp_path, CONTENTS)
    manifest = script.read_manifest(cache, "org/m",
                                    listing=_listing(*CONTENTS))
    uploads = script.plan(cache, manifest)
    monkeypatch.setattr(script.shutil, "which", lambda name: "/usr/bin/aws")
    calls = []

    class _Done:
        returncode = 0
        stdout = b"2026-08-21 12:00:00  12 object\n"

    def run(args, **kwargs):
        calls.append(args)
        return _Done()

    written = script.upload(uploads, "s3://bucket/prefix", manifest, run=run)

    assert written == ["models/org/m/manifest.json"], written
    copies = [args for args in calls if args[:3] == ["aws", "s3", "cp"]]
    assert len(copies) == 1, copies
    assert copies[0][4].endswith("/models/org/m/manifest.json")


# -- the round trip, which is what keeps the two halves honest -------------------


def _served(script, mirror, tmp_path, monkeypatch):
    """A generated manifest, served over HTTP, with the client pointed at it.

    Returns `(source cache, manifest, state)`.
    """
    source = _cache(tmp_path, CONTENTS)
    manifest = script.read_manifest(source, "org/m", listing=_listing(*CONTENTS))
    routes = {"/models/org/m/manifest.json": json.dumps(manifest).encode()}
    for entry in manifest["files"]:
        routes[f"/models/org/m/{COMMIT}/{entry['etag']}"] = CONTENTS[entry["name"]]
    _url, state = _start_server(b"", routes=routes)
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"])
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "org/m")
    return source, manifest, state


def test_the_client_accepts_what_the_script_wrote_field_for_field(
        script, mirror_and_base, tmp_path, monkeypatch):
    """Half one of the round trip: the agreement between generator and parser.

    Split out from the download below because the two had different platform
    requirements — this one is true on every OS, while fetching needed
    `os.pwrite`, and keeping them in one test meant Windows either skipped the
    agreement check too or ran a fetch half on a platform that could not fetch.
    Both halves run everywhere now that the fetch has an append-only route, and
    the split is kept because a manifest that parses and a manifest that
    downloads are still two different claims: one can fail while the other
    holds, and a single test would not say which.
    """
    mirror, _base = mirror_and_base
    _source, manifest, state = _served(script, mirror, tmp_path, monkeypatch)

    read = mirror.manifest("org/m")

    assert read is not None, "the client rejected a manifest the script wrote"
    assert read["commit"] == manifest["commit"]
    assert read["files"] == manifest["files"]
    # Nested names survive the trip as `/`-separated wire names, which is the
    # boundary `script.wire_name` owns — on Windows the generator's own
    # `os.path.relpath` produces `\` and the client rejects that as traversal.
    assert "tokenizer/vocab.json" in [entry["name"] for entry in read["files"]]
    assert [r["path"] for r in state["requests"]] == [
        "/models/org/m/manifest.json"]


def test_a_manifest_built_from_a_cache_round_trips_through_the_client(
        script, mirror_and_base, tmp_path, monkeypatch):
    """Half two: download it and compare the two cache directories.

    This is the only test in the feature that exercises BOTH halves against real
    bytes. Each on its own can be self-consistently wrong: a script that reports
    the symlink's size and a client that trusts it would agree perfectly and
    produce a truncated blob under a real etag.

    **No longer gated on `os.pwrite`.** It was, because the mirror's only
    transport refused without it and this half really did fetch; the transport
    now takes a single append-only stream instead (`worker_base._appends_only`),
    so the round trip runs on win32 too — and it is the one test in this feature
    that proves a real Windows download end to end.
    """
    mirror, base = mirror_and_base
    source, _manifest, _state = _served(script, mirror, tmp_path, monkeypatch)

    landed = str(tmp_path / "downloaded" / "models--org--m")
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": landed)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    monkeypatch.setattr(base, "SEGMENT_MIN_BYTES", 20_000)
    monkeypatch.setattr(base, "CHUNK_BYTES", 50_000)

    snapshot = base.download_snapshot("org/m")

    assert snapshot == os.path.join(landed, "snapshots", COMMIT)
    for name, body in CONTENTS.items():
        assert open(os.path.join(snapshot, name), "rb").read() == body
    # The blob NAMES are the assertion that matters: hf's loaders find a file by
    # its etag, so a download that got the bytes right and the names wrong is
    # cache-invisible and re-downloads forever.
    origin = os.path.join(source, "models--org--m", "blobs")
    assert sorted(os.listdir(os.path.join(landed, "blobs"))) == sorted(
        os.listdir(origin))
    assert open(os.path.join(landed, "refs", "main")).read() == COMMIT


def test_the_scripts_schema_is_the_one_the_client_understands(script,
                                                             mirror_and_base):
    """Two constants, two files, one meaning. A bump on one side alone is a
    manifest every client reads as "no mirror", which is a silent
    un-deployment."""
    mirror, _base = mirror_and_base
    assert script.SCHEMA == mirror.SCHEMA


@pytest.fixture()
def mirror_and_base():
    return _fresh_mirror(), _fresh_base()


# -- completeness is proven here, not assumed (review findings 3 and 4) ----------


def test_a_snapshot_missing_a_file_the_repo_HAS_is_refused(script, tmp_path):
    """The bug this whole check exists for, and it is not hypothetical.

    `torch_image._download` fetches a GGUF-recipe image model with
    `allow_patterns=recipe["keep"]`, so any build machine that ever LOADED one
    holds a deliberately partial cache for it. The folder exists, so nothing
    re-downloads, and a manifest describing only the allow-list subset gets
    published. A client with no recipe then selects everything in that manifest,
    fetches the subset, records it complete, and the model is permanently broken
    — the exact class of failure this script's docstring says it exists to
    prevent.

    The Hub is the only authority on what a repo contains, and asking it HERE
    costs nothing: this runs on a build machine, not in a user's runner.
    """
    cache = _cache(tmp_path, {"model.safetensors": b"weights" * 100})

    with pytest.raises(ValueError, match="config.json"):
        script.read_manifest(cache, "org/m",
                             listing=_listing("model.safetensors", "config.json"))


def test_a_manifest_is_only_marked_complete_when_it_was_checked(script, tmp_path):
    """`complete` is an assertion about the Hub listing, so it is written only
    where that comparison happened. The client refuses a manifest without it."""
    cache = _cache(tmp_path, CONTENTS)

    manifest = script.read_manifest(cache, "org/m", listing=_listing(*CONTENTS))

    assert manifest["complete"] is True


def test_hfs_own_bookkeeping_inside_a_snapshot_is_not_a_repo_file(script, tmp_path):
    """Newer `snapshot_download` writes `.cache/huggingface/download/*.metadata`
    INSIDE the snapshot directory.

    A walk that treats those as repo files publishes entries whose realpath is
    not a blob, which would abort every real model on a modern cache. Skipped by
    name, because that directory is hf's private state and not repo content.
    """
    cache = _cache(tmp_path, CONTENTS)
    junk = os.path.join(cache, "models--org--m", "snapshots", COMMIT,
                        ".cache", "huggingface", "download")
    os.makedirs(junk)
    with open(os.path.join(junk, "config.json.metadata"), "w") as handle:
        handle.write("not a blob")

    manifest = script.read_manifest(cache, "org/m", listing=_listing(*CONTENTS))

    assert [entry["name"] for entry in manifest["files"]] == sorted(CONTENTS)


def test_fetch_missing_completes_a_cache_that_is_merely_PRESENT(script, tmp_path,
                                                                monkeypatch,
                                                                capsys):
    """"The folder exists" is not "the repo is here".

    That was the whole of the old condition, and a scoped download leaves a
    folder that exists and holds a tenth of the repo. `--fetch-missing` now
    always asks hf to complete it, which for a cache that IS complete costs one
    etag revalidation — nothing, on a release script.
    """
    cache = _cache(tmp_path, CONTENTS)
    fetched = []
    monkeypatch.setattr(script, "fetch_missing",
                        lambda repo_id, into: fetched.append(repo_id))
    monkeypatch.setattr(script, "hub_listing", _listing(*CONTENTS))

    script.main(["--cache", cache, "--model", "org/m", "--fetch-missing"])

    assert fetched == ["org/m"], "an existing folder was assumed complete"


def test_without_fetch_missing_nothing_is_downloaded(script, tmp_path, monkeypatch):
    """Reading a cache is the default and stays offline apart from the listing;
    downloading gigabytes is opt-in."""
    cache = _cache(tmp_path, CONTENTS)

    def no(*args, **kwargs):
        raise AssertionError("a plain run downloaded a model")

    monkeypatch.setattr(script, "fetch_missing", no)
    monkeypatch.setattr(script, "hub_listing", _listing(*CONTENTS))

    assert script.main(["--cache", cache, "--model", "org/m"]) == 0


def test_a_zero_byte_file_is_published_like_any_other(script, tmp_path):
    """An empty file is legal on the Hub. Refusing the repo over one would take
    a whole model off the mirror because of a file with nothing in it."""
    cache = _cache(tmp_path, {"model.safetensors": b"w" * 100, "empty.txt": b""})

    manifest = script.read_manifest(cache, "org/m",
                                    listing=_listing("model.safetensors",
                                                     "empty.txt"))

    empty = next(e for e in manifest["files"] if e["name"] == "empty.txt")
    assert empty["size"] == 0
    assert empty["sha256"] == hashlib.sha256(b"").hexdigest()


# -- ONE FILE, from a cache that holds only that file (AI-5m) --------------------
#
# `llama_text.download` fetches one GGUF out of a repo publishing dozens of
# quantizations, and the whole-repo mode above cannot serve it: it refuses a
# partial cache, so publishing one 2.6GB file would mean holding — and
# uploading — all 147.81GB of `unsloth/Qwen3.5-9B-GGUF`. This mode reads exactly
# one file, asks the Hub nothing (there is no completeness to prove), and writes
# its manifest to a key of its own while the BLOB stays in the shared per-repo
# blob space.

GGUF_NAME = "Model-Q4_K_M.gguf"
ONE_FILE = {GGUF_NAME: hashlib.sha256(b"quant").digest() * 6250}  # 200_000 B


def test_a_per_file_manifest_is_read_from_a_cache_holding_only_that_file(script,
                                                                        tmp_path):
    """The whole point: a partial cache is the NORMAL input here.

    Read out of the cache like everything else — commit from `refs/main`, etag
    from the blob filename, size and digest from the blob — and with no
    `complete` field, because one named file makes no claim about the repo.
    """
    cache = _cache(tmp_path, ONE_FILE)

    manifest = script.read_file_manifest(cache, "org/m", GGUF_NAME)

    etag = hashlib.sha256(ONE_FILE[GGUF_NAME]).hexdigest()
    assert manifest == {
        "schema": script.SCHEMA, "repo": "org/m", "commit": COMMIT,
        "files": [{"name": GGUF_NAME, "etag": etag, "size": 200_000,
                   "sha256": etag}],
    }
    assert "complete" not in manifest


def test_a_per_file_manifest_asks_the_hub_nothing(script, tmp_path, monkeypatch):
    """No listing call, because there is no completeness claim to earn.

    The whole-repo mode has to ask (a cache directory existing says nothing
    about it being whole), and that is exactly the cost this mode exists to
    avoid — the Hub's answer for a GGUF repo is the 147.81GB list.
    """
    cache = _cache(tmp_path, ONE_FILE)

    def no(*args, **kwargs):
        raise AssertionError("the per-file mode listed the repo")

    monkeypatch.setattr(script, "hub_listing", no)

    assert script.read_file_manifest(cache, "org/m", GGUF_NAME) is not None


def test_a_file_the_cache_does_not_hold_is_refused(script, tmp_path):
    """Refused, never trimmed to what is there — the same rule as the repo mode,
    for the same reason: a manifest of "whatever this machine happens to hold"
    is the bug and not the fix."""
    cache = _cache(tmp_path, ONE_FILE)

    with pytest.raises(ValueError, match="not in the cached snapshot"):
        script.read_file_manifest(cache, "org/m", "some-other-quant.gguf")


@pytest.mark.parametrize("bad", ["", "..", "sub/file.gguf", "/abs.gguf",
                                 ".hidden.gguf", "a?b.gguf"])
def test_a_filename_the_client_would_reject_is_never_published(script, tmp_path,
                                                               bad):
    """The name becomes one URL path SEGMENT of the manifest's key, and the
    client validates it as such. Publishing a name it refuses would produce an
    object nothing can read, which is the "fails silently" class this script
    exists to prevent."""
    cache = _cache(tmp_path, ONE_FILE)

    with pytest.raises(ValueError, match="not a publishable file name"):
        script.read_file_manifest(cache, "org/m", bad)


def test_the_per_file_plan_keeps_the_blob_in_the_shared_space(script, tmp_path):
    """Two objects: the blob at the EXISTING per-repo key, and the manifest at a
    per-file one.

    The shared blob key is what makes a repo mirrored both ways store one copy
    of each blob — the key already names the commit and the etag, so it cannot
    hold anything but those bytes whichever manifest points at it. Manifest
    last, as ever.
    """
    cache = _cache(tmp_path, ONE_FILE)
    manifest = script.read_file_manifest(cache, "org/m", GGUF_NAME)

    uploads = script.plan(cache, manifest, filename=GGUF_NAME)

    etag = manifest["files"][0]["etag"]
    assert [item["key"] for item in uploads] == [
        f"models/org/m/{COMMIT}/{etag}",
        f"models/org/m/files/{GGUF_NAME}/manifest.json",
    ]
    assert uploads[0]["immutable"] is True
    assert uploads[-1]["immutable"] is False
    assert uploads[0]["path"].endswith(os.path.join("blobs", etag))


def test_a_per_file_upload_sets_the_same_cache_control_as_the_repo_mode(script,
                                                                       tmp_path,
                                                                       monkeypatch):
    """One short-TTL manifest and one immutable blob — the same two answers, at a
    different key. And a blob whose key exists is still skipped."""
    cache = _cache(tmp_path, ONE_FILE)
    manifest = script.read_file_manifest(cache, "org/m", GGUF_NAME)
    uploads = script.plan(cache, manifest, filename=GGUF_NAME)
    monkeypatch.setattr(script.shutil, "which", lambda name: "/usr/bin/aws")
    calls = []

    class _Missing:
        returncode = 1
        stdout = b""

    def run(args, **kwargs):
        calls.append(args)
        return _Missing()

    written = script.upload(uploads, "s3://bucket/prefix", manifest, run=run)

    assert written == [f"models/org/m/{COMMIT}/{manifest['files'][0]['etag']}",
                       f"models/org/m/files/{GGUF_NAME}/manifest.json"]
    copies = [args for args in calls if args[:3] == ["aws", "s3", "cp"]]
    assert len(copies) == 2, copies
    assert "public, max-age=31536000, immutable" in copies[0]
    assert "max-age=60" in copies[1]
    assert copies[1][4].endswith(f"/models/org/m/files/{GGUF_NAME}/manifest.json")


def test_the_cli_publishes_one_file_of_a_repo(script, tmp_path, monkeypatch,
                                              capsys):
    """`--model org/name --file NAME`, dry run by default like everything else."""
    cache = _cache(tmp_path, ONE_FILE)

    def no(*args, **kwargs):
        raise AssertionError("a dry run shelled out to something")

    monkeypatch.setattr(script.subprocess, "run", no)
    monkeypatch.setattr(script.shutil, "which", no)
    monkeypatch.setattr(script, "hub_listing", no)

    code = script.main(["--cache", cache, "--model", "org/m", "--file", GGUF_NAME])

    out = capsys.readouterr().out
    assert code == 0
    assert f"would upload models/org/m/files/{GGUF_NAME}/manifest.json" in out


def test_a_file_missing_from_the_cache_is_skipped_but_the_run_fails(script,
                                                                   tmp_path,
                                                                   monkeypatch,
                                                                   capsys):
    """Same rule as a missing repo: tolerant loop, non-zero exit. A curated GGUF
    quietly absent from the mirror is invisible by design, so the exit code is
    the only place it can be caught."""
    cache = _cache(tmp_path, ONE_FILE)
    monkeypatch.setattr(script, "hub_listing", _listing(*ONE_FILE))

    code = script.main(["--cache", cache, "--model", "org/m",
                        "--file", GGUF_NAME, "--file", "absent.gguf"])

    out = capsys.readouterr().out
    assert code == 1, "an incomplete publish exited green"
    assert "absent.gguf: SKIPPED" in out
    assert f"would upload models/org/m/files/{GGUF_NAME}/manifest.json" in out


def test_a_file_without_a_model_is_a_usage_error(script):
    """`--file` names a file OF a repo, and one repo: applying it to the whole
    suggested list would ask every model for somebody else's GGUF."""
    with pytest.raises(SystemExit):
        script.main(["--file", GGUF_NAME])
    with pytest.raises(SystemExit):
        script.main(["--model", "org/a", "--model", "org/b", "--file", GGUF_NAME])


def test_the_default_run_expands_a_curated_gguf_into_a_repo_and_a_file(script):
    """The default target list, and this is a BUG FIX as much as a feature.

    `catalog.all_suggested_ids()` includes `llamacpp-text`'s bare `.gguf`
    filenames, so the old default run looked for `models--LFM2.5-….gguf` in the
    cache, found nothing, and printed SKIPPED for every llama.cpp model — a
    default invocation that could not succeed and always exited 1. Those ids are
    now per-file targets against the recipe's repo, which is also the only shape
    that can be published at all: the repo is 147.81GB and the file is 2.6GB.
    """
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from fused_render.ai import catalog
    from fused_render.ai.runners import formats

    targets = script.suggested_targets()

    assert len(targets) == len(catalog.all_suggested_ids())
    for model_id, recipe in formats.GGUF_RECIPES.items():
        assert (recipe["repo"], recipe["file"]) in targets, model_id
        assert not any(repo == model_id for repo, _file in targets), (
            f"{model_id} is a FILENAME and would be looked up as a repo folder")
    # …and every other runner's ids are repo ids and stay whole-repo targets.
    assert all(filename is None for repo, filename in targets
               if repo not in {r["repo"] for r in formats.GGUF_RECIPES.values()})


def test_the_client_reads_a_per_file_manifest_the_script_wrote(script,
                                                              mirror_and_base,
                                                              tmp_path,
                                                              monkeypatch):
    """The round trip, per-file half: generator and the second reader agreeing,
    then a real download through `download_file`.

    Each half can be self-consistently wrong on its own — a script that reported
    the symlink's size and a client that trusted it would agree perfectly and
    produce a truncated blob under a real etag — so this drives both against
    real bytes, and lands them in hf's own cache layout.
    """
    mirror, base = mirror_and_base
    source = _cache(tmp_path, ONE_FILE)
    manifest = script.read_file_manifest(source, "org/m", GGUF_NAME)
    etag = manifest["files"][0]["etag"]
    routes = {f"/models/org/m/files/{GGUF_NAME}/manifest.json":
              json.dumps(manifest).encode(),
              f"/models/org/m/{COMMIT}/{etag}": ONE_FILE[GGUF_NAME]}
    _url, state = _start_server(b"", routes=routes)
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"])
    monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", "org/m")

    read = mirror.file_manifest("org/m", GGUF_NAME)

    assert read is not None, "the client rejected a manifest the script wrote"
    assert read["commit"] == manifest["commit"]
    assert read["files"] == manifest["files"]

    landed = str(tmp_path / "downloaded" / "models--org--m")
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": landed)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    monkeypatch.setattr(base, "SEGMENT_MIN_BYTES", 20_000)
    monkeypatch.setattr(base, "CHUNK_BYTES", 50_000)
    monkeypatch.setattr(base, "_cached_file", lambda repo, name: None)

    path = base.download_file("org/m", GGUF_NAME)

    assert open(path, "rb").read() == ONE_FILE[GGUF_NAME]
    # The blob NAME is the assertion that matters: hf's loaders find a file by
    # its etag, so bytes right and name wrong is cache-invisible forever.
    assert os.listdir(os.path.join(landed, "blobs")) == [etag]
    assert open(os.path.join(landed, "refs", "main")).read() == COMMIT


# -- the one boundary where a path becomes a name (and back) ---------------------


def test_a_platform_separator_never_reaches_the_wire(script, monkeypatch):
    """`wire_name` is the ONE place a filesystem path becomes a manifest name.

    Exercised directly rather than through a walk, because the bug it prevents
    cannot happen on this machine: `os.sep` is `/` here, so a nested name comes
    out right by accident. On Windows `os.path.relpath` returns
    `tokenizer\\vocab.json`, and the client rejects a `\\` outright — it is a
    traversal character there — so the generator would publish a manifest its own
    client refuses. Nothing downstream converts separators, deliberately: a wire
    name is `/`-separated everywhere, and both POSIX and Windows accept `/` in
    every filesystem call the client makes.
    """
    monkeypatch.setattr(script.os, "sep", "\\")
    monkeypatch.setattr(script.os.path, "relpath",
                        lambda path, root: "tokenizer\\vocab.json")

    assert script.wire_name("ignored", "ignored") == "tokenizer/vocab.json"


def test_a_wire_name_is_left_alone_when_it_is_already_one(script):
    """The POSIX case, which must stay a no-op."""
    root = os.path.join("a", "b")
    assert script.wire_name(os.path.join(root, "t", "v.json"), root) == "t/v.json"


def test_these_tests_cannot_reach_the_network():
    """The imported guard, asserted here too — see the import comment.

    This is the file where it mattered: Windows CI failed here on a 401 from
    huggingface.co, and on a machine with an `HF_TOKEN` it would have PASSED by
    downloading a real repo instead.
    """
    import socket

    with pytest.raises(AssertionError, match="tried to resolve"):
        socket.getaddrinfo("huggingface.co", 443)
