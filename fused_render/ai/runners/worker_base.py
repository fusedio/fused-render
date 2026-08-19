"""The worker side of the contract, written once (SPEC AI-3, AI-9).

Every runner is a folder with a `pyproject.toml` and a `worker.py`, started by
`fused_render.ai.supervisor` on the interpreter built from that declaration.
What a worker DOES differs completely — mlx_text loads a language model and
streams tokens, diffusers_image loads a pipeline and writes a PNG — but what it
IS does not: four routes, a token in a header, a port the child publishes, a
state machine the supervisor polls, and progress posted to the download manager.

That invariant half lives here. A concrete worker supplies three functions and
calls `serve()`:

    download(model_id)          fetch what is missing; return where it landed
    load(model_id, fetched)     put it in memory; raise to fail
    generate(body[, write])     one request; NDJSON via `write` or a dict back

and gets `/health`, `/cancel`, `/quit`, `/generate`, `--download-only`, the port
handshake, the auth check, the error framing and the reporting for free.

**Why a shared module rather than two standalone files.** The obvious reading of
"a runner is a folder" is that each folder is self-contained, and the first cut
was exactly that: mlx_text/worker.py carried all of this inline. Copying it for
the image runner would have put the SUPERVISOR'S contract — the auth header's
name, the status file's shape, the state vocabulary it polls for, the way
download bytes are measured — in two places, and every bug in this feature so
far has been two places encoding one rule and drifting apart. The contract
belongs beside the thing that defines it, once.

This module is **stdlib only**, deliberately, for two reasons. It is imported by
every runner's interpreter, so anything imported here becomes a dependency of
every backend forever. And it means the contract is IMPORTABLE BY THE TESTS,
which is the only way any of it gets tested at all: neither concrete worker can
run on CI (one needs Metal, the other several GB of torch), but this can, with
stub callables standing in for the model.
"""

import argparse
import concurrent.futures
import contextlib
import fnmatch
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import stat
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

# ------------------------------------------------------------------- the state
#
# The vocabulary the SUPERVISOR polls for. `state` is the load-time machine —
# `ready` is the only value meaning the model can answer, `error` the only
# terminal failure — so these strings are contract, not description.

STATE = {
    "state": "starting",   # starting | downloading | loading | ready | error
    "model": "",
    "detail": "",
    "error": "",
    "resident_bytes": None,
    "loaded_at": None,
    #: "cuda" | "mps" | "cpu" — what the weights actually landed on, set by the
    #: runner's `load()`. Only the process holding them knows: the supervisor
    #: can see that this machine HAS a GPU and not that torch was built to use
    #: it, which on Windows is the common case rather than the exotic one (the
    #: PyPI torch wheel there is CPU-only). Reported because a model answering
    #: at three tokens a second is working perfectly and looks broken, and the
    #: device is the whole of the explanation.
    #:
    #: None from a runner that does not set it — one device, nothing to say.
    "device": None,
}
_state_lock = threading.Lock()

#: Set by `/cancel`. Long-running work checks it; what "stop" means is the
#: runner's to decide (a token loop breaks, a denoiser raises).
#:
#: Cleared by whichever generation OWNS `GENERATE_LOCK`, never by the handler on
#: its way in: a second request arriving while the first is generating would
#: otherwise erase the ✕ just pressed for the first, which then runs to
#: completion under a Stop that appeared to work.
CANCEL = threading.Event()

#: One generation at a time. A laptop has one GPU, and neither mlx's model
#: object nor a diffusers pipeline is safe to call from two threads — so a
#: second request waits rather than interleaves.
GENERATE_LOCK = threading.Lock()

TOKEN = os.environ.get("FUSED_AI_WORKER_TOKEN", "")
JOB_ID = ""
JOB_URL = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/") + "/api/jobs"

JOB_TIMEOUT_S = 3.0


def set_state(**fields):
    with _state_lock:
        STATE.update(fields)


def snapshot():
    with _state_lock:
        return dict(STATE)


def describe_failure(exc):
    """What a user should be told about `exc` — the CHAIN, never the top frame.

    `str(exc)` is the wrong answer whenever a library re-raises, and the
    libraries a runner loads all do. transformers wraps every import failure
    from its lazy-module machinery, so a missing stdlib module three layers
    down arrived on the AI Models page as:

        Could not import module 'AutoTokenizer'

    while the actual exception it was raised `from` said:

        ModuleNotFoundError: No module named 'filecmp'

    One of those names a thing the user can act on; the other sends them
    looking at the model, the repo and the download — all of which were fine.
    So the chain is walked to its root and reported with the top message.

    `__cause__` first, then `__context__`: an explicit `raise … from e` is the
    library telling us what it wrapped, and an implicit context is the next-best
    evidence when it did not bother.

    `__suppress_context__` is honoured, which is the same rule `traceback`
    itself follows and not a technicality here. `raise … from None` is a library
    saying the exception it caught is NOT the explanation — the shape an
    optional-dependency probe takes (`except ImportError: raise … from None`) —
    and walking past it would let a deliberately hidden ImportError become our
    "root cause", up to and including firing the stdlib hint about an
    interpreter that is perfectly complete.
    """
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__)
    text = f"{exc.__class__.__name__}: {exc}"
    if len(chain) > 1:
        root = chain[-1]
        text += f" — caused by {root.__class__.__name__}: {root}"
    hint = _stdlib_hint(chain)
    return text + hint if hint else text


def _stdlib_hint(chain):
    """The sentence for a missing STDLIB module, or "".

    This is a fact about the INTERPRETER, not about the environment built on
    it, and the difference is the whole point: a missing third-party package is
    fixed by rebuilding the runner's venv, while a missing stdlib module is
    baked into the interpreter that venv was created from, so rebuilding
    reproduces it exactly. Told the first story, a user retries forever.

    **`ModuleNotFoundError`, not `ImportError`**, and that is not pedantry:
    `from email import nope` raises a plain ImportError whose `.name` is
    `email` — a package that is present and fine — so keying on ImportError
    would accuse a complete interpreter of missing part of its stdlib and tell
    the user a rebuild cannot help. That is the exact class of confidently
    wrong cause this function exists to stop, which makes it worth being
    strict: only "the module was not found" earns the accusation.

    The TOP-LEVEL name decides, because `sys.stdlib_module_names` holds only
    top-level names while a partial stdlib fails as `No module named
    'email.mime'`. The full name is what gets reported — it is the thing that
    is actually missing.
    """
    for exc in chain:
        name = getattr(exc, "name", None) or ""
        if isinstance(exc, ModuleNotFoundError) and name.partition(".")[0] in sys.stdlib_module_names:
            return (
                f"\n\n`{name}` is part of the PYTHON STANDARD LIBRARY, so this is "
                f"the interpreter this environment was built on ({sys.base_prefix}) "
                "shipping without it — not a problem with this model and not "
                "something rebuilding the environment can fix. Please report it "
                "with this message."
            )
    return ""


class Cancelled(Exception):
    """Raised out of a progress callback when the app asked us to stop.

    A worker's heavy phases are opaque C calls with no interruption point, so
    the only place a stop can be honoured is the callback the library hands us —
    which is where this comes from.
    """


# ------------------------------------------------------- reporting to the app


#: The last thing `report` sent, so `heartbeat` can send it again. One slot:
#: a worker reports about one piece of work at a time.
_last_report = {}
_last_report_lock = threading.Lock()

#: How often a heartbeat re-sends it. Well under `jobs.STALE_AFTER_S` (30s) —
#: the number that matters is the GAP between real ticks, and for a denoiser
#: that gap is one step, which on a laptop is routinely longer than the whole
#: stale window.
HEARTBEAT_S = 5.0


def report(job=None, **fields):
    """One progress tick to the download manager. Never raises, never blocks long.

    Returns the stored record, or None. **The return value is load-bearing**: the
    manager's ✕ sets `cancel_requested` on the row, and the reply to the tick we
    were sending anyway is how that reaches a process sitting inside a
    multi-minute call. Reporting is otherwise decoration — if it fails the model
    still loads — so the socket timeout is short and every error is swallowed.

    `job` overrides the id: a load reports to the row the supervisor opened for
    it, while one image generation reports to its own per-request row.
    """
    job = job or JOB_ID
    if not job or not JOB_URL.startswith("http"):
        return None
    # Remembered before the send, so a heartbeat repeats what we MEANT to say
    # even if this particular tick never landed. Only a REAL tick writes this
    # slot — see `_send`, which is what the heartbeat calls.
    with _last_report_lock:
        _last_report.clear()
        _last_report.update(job=job, fields=dict(fields))
    return _send(job, fields)


def _send(job, fields):
    """POST one tick. The half of `report` that does NOT remember it.

    Split out for the heartbeat, and this is not tidiness. A heartbeat that
    called `report` would re-write `_last_report` with the payload it had just
    read — so a real tick landing between the read and that write was clobbered
    back to the older one, and every later beat repeated the stale numbers. The
    bar went BACKWARDS while the model was making progress, which is a worse lie
    than the stall this exists to prevent.
    """
    body = json.dumps({"id": job, **fields}).encode()
    request = urllib.request.Request(
        JOB_URL, data=body,
        headers={"Content-Type": "application/json", "X-Fused": "1",
                 "X-Fused-Worker": TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=JOB_TIMEOUT_S) as response:
            record = json.loads(response.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


@contextlib.contextmanager
def heartbeat():
    """Keep the job row alive for as long as the body runs.

    A row with no update in `jobs.STALE_AFTER_S` (30s) is reported as "no longer
    reporting", which is true of a page that was closed and a LIE about a worker
    that is simply slow. The image runner reports once per denoising step, and a
    FLUX step on a laptop routinely takes longer than the whole stale window — so
    a render that was progressing perfectly announced, at step 1 of 3, that
    nobody was reporting it.

    This is AI-5b's rule ("the poll doubles as the heartbeat") applied where it
    was missing. It lives in the base rather than in the denoiser because the
    property that causes it — progress whose natural granularity is coarser than
    30 seconds — belongs to the CONTRACT, and the next runner to have it should
    not have to rediscover this.

    Deliberately re-sends the LAST payload rather than inventing a new one: the
    bar must not move on a tick that learned nothing, and repeating `done`/
    `total` is what "still here, still on this step" looks like. Plain `report`,
    never `report_or_cancel` — a `Cancelled` raised on a timer thread is raised
    at nobody. The ✕ is still honoured where it always was, in the generating
    thread's own tick.
    """
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_S):
            with _last_report_lock:
                if not _last_report:
                    continue
                job = _last_report["job"]
                fields = dict(_last_report["fields"])
            # A terminal state is never repeated: the work is over and the row
            # is not ours to keep touching.
            if fields.get("state") in ("done", "error", "cancelled"):
                continue
            # Re-checked as late as possible. The work can finish during the
            # wait above, and the FIRST payload of a generation carries
            # `state: "running"` — so a beat that slipped through here after the
            # supervisor marked the row done would flip it back to running and
            # clear its `finished_at`.
            if stop.is_set():
                return
            _send(job, fields)

    thread = threading.Thread(target=beat, name="heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # JOINED, not just signalled. `stop.set()` cannot reach a beat already
        # inside its POST, and that tick would land after the work finished —
        # the same revival by a slower route. Bounded by the socket timeout it
        # is waiting on, and free in the common case: a generation shorter than
        # one interval leaves the thread parked in `wait`, which returns at once.
        thread.join(timeout=JOB_TIMEOUT_S + 1.0)


def report_or_cancel(job=None, **fields):
    """`report`, raising `Cancelled` if the reply says the ✕ was pressed."""
    record = report(job=job, **fields)
    if record and record.get("cancel_requested"):
        raise Cancelled()
    return record


#: A runner's own memory measurement, when it has one better than RSS. Set by
#: `serve()`. MLX is the reason it exists: its weights are memory-mapped and its
#: arrays are lazy, so RSS right after a load reports the interpreter and not
#: the model — 379 MB for a 6GB model, which is what sent us looking.
_measure = None


def resident_bytes():
    """What this model is costing in memory, or None.

    RSS by default: on Apple Silicon the GPU pool IS system memory, so it is the
    honest single number and there is no separate VRAM figure to reconcile it
    with. A runner that can do better supplies `memory=` to `serve()`, and the
    LARGER of the two wins — both are real measurements and neither is a
    superset (RSS includes the interpreter and framework; a framework allocator
    includes buffers that may not be faulted into RSS yet), so the cost is at
    least the larger.

    psutil comes with every runner's environment; if it is somehow absent the
    answer is whatever the runner could measure, or None rather than a guess.
    """
    own = None
    if _measure is not None:
        try:
            own = _measure()
        except Exception:  # noqa: BLE001 - a runner's own probe must never break /health
            own = None
    rss = None
    try:
        import psutil

        rss = int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:  # noqa: BLE001 - psutil raises its own family; none is fatal here
        rss = None
    candidates = [n for n in (own, rss) if isinstance(n, int) and n > 0]
    return max(candidates) if candidates else None


# --------------------------------------------------------- downloading weights
#
# Progress is measured from the DISK (SPEC AI-5b). `snapshot_download` exposes
# only its outer "Fetching N files" counter through `tqdm_class`; the per-file
# byte bars are internal. Reporting that counter as bytes is how a 4.6GB pull
# came to read "10 / 11 B", and during a single large shard it does not move at
# all — so the row also went stale mid-download and the manager declared nobody
# was reporting. Walking the repo folder answers both: real bytes, and a tick
# every second whatever huggingface_hub is doing inside.
#
# The FETCH is ours too (SPEC AI-5i). `snapshot_download` opens one connection
# per file and one file at a time, so a model whose bytes are a single 4.6GB
# shard downloads on exactly one connection — and an interruption throws the
# whole thing away, which matters because the supervisor kills the fetch on quit
# (AI-5e). What is below fetches with several connections at once, split across
# files AND inside one file with `Range`, recording per-segment offsets as the
# bytes land. Every failure and every incapability falls back to
# `snapshot_download` under the same progress wrapper: a download that got
# faster and sometimes broken would be a bad trade.

#: Below this a file is fetched whole: splitting a 200KB config across four
#: sockets costs four round trips to save nothing.
SEGMENT_MIN_BYTES = 32 * 1024 * 1024
#: Per file. Past a handful the Hub's per-connection throughput is the limit
#: rather than the connection count, and each one is another socket to retry.
MAX_SEGMENTS_PER_FILE = 4
#: Across everything — the ONE number that bounds how many sockets a download
#: opens. A pool per file would multiply the caps together.
MAX_CONNECTIONS = 8
#: Deliberately NOT hf's `.incomplete`. hf resumes one of those by seeking to
#: its current length; our segments write out of order, so a partial file of
#: length N does not mean the first N bytes are there, and handing hf one of
#: ours would produce a silently corrupt blob. A suffix of our own also keeps
#: the fallback clean — hf never sees our state at all.
PART_SUFFIX = ".fusedpart"
READ_BYTES = 1024 * 1024
#: Big enough that a filesystem which really allocates cannot hide it in a
#: block, small enough that paying for it on one is nothing.
SPARSE_PROBE_BYTES = 4 * 1024 * 1024
HTTP_TIMEOUT_S = 30.0
SEGMENT_ATTEMPTS = 5
RETRY_BACKOFF_S = 0.5
FLUSH_EVERY_S = 1.0
#: The revision both paths use, named rather than implied. It is hf's own
#: `snapshot_download` default, which is what keeps the fast path and the
#: fallback on one revision of a model.
DEFAULT_REVISION = "main"

_CONTENT_RANGE = re.compile(r"/(\d+)\s*$")
_RANGE_START = re.compile(r"^bytes\s+(\d+)-")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")

#: Everything a segment retries on. `HTTPException` earns its place: an
#: `IncompleteRead` or an `InvalidChunkLength` from a body that broke mid-stream
#: is not an `OSError`, and outside this tuple one such hiccup — among the
#: commonest ways a transport misbehaves — aborted the whole multi-file download.
_TRANSIENT = (OSError, urllib.error.URLError, http.client.HTTPException, ValueError)


class _Unsegmentable(Exception):
    """This repo cannot be fetched our way, so hf's downloader gets it back.

    Not an error in itself — no range support, a platform without `os.pwrite`,
    a Hub that reported no size — which is why it reads as a fallback rather
    than as a failed download.
    """


def repo_folder(model_id, repo_type="model"):
    """This repo's folder in the hub cache, or None.

    `repo_folder_name` is hf's OWN encoder for `org/name` -> `models--org--name`,
    used here rather than a `.replace("/", "--")` for the usual reason: the
    layout is theirs to change, and a second copy of it here would keep
    reporting numbers for a directory that no longer exists. If hf ever moves
    the helper, progress degrades to a pulse — never to a wrong figure.
    """
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        from huggingface_hub.file_download import repo_folder_name
    except ImportError:
        return None
    return os.path.join(HF_HUB_CACHE, repo_folder_name(repo_id=model_id, repo_type=repo_type))


def bytes_on_disk(folder):
    """How much of `folder` is on disk right now, in bytes — None if unknown.

    Counts the partial files a download in flight is writing — hf's
    `.incomplete` and our own `.fusedpart` — which is the whole point: they ARE
    the progress. Symlinks are skipped from the `lstat` result itself, so the
    snapshot entries are not counted a second time on top of the blobs they
    point at.

    A `.fusedpart` is measured by ALLOCATED BLOCKS rather than by length. Our
    segments write out of order, so the file is created at its final size with
    `ftruncate` and filled as a sparse file: `st_size` is the full 4.6GB from
    the first second, and reporting that would put the bar at 100% before a
    byte had arrived. `st_blocks` is what the download has actually put on the
    disk. Where the platform has no such notion (Windows), `st_blocks` is
    absent and the length is the honest answer anyway — nothing is sparse there.
    """
    if not folder:
        return None
    total = 0
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            blocks = getattr(info, "st_blocks", None)
            if name.endswith(PART_SUFFIX) and blocks is not None:
                total += min(info.st_size, blocks * 512)
            else:
                total += info.st_size
    return total


def selects(name, include=None, allow=None, ignore=None) -> bool:
    """Whether a repo file is in scope, with `huggingface_hub`'s OWN semantics.

    One function, because three readers ask it and they must not drift: the
    total on the bar, the list the segmented fetch works through, and
    `snapshot_download` itself on the fallback path. Hub's `filter_repo_objects`
    is `(no allow_patterns or any match) and (no ignore_patterns or no match)`,
    matched with `fnmatch` against the path RELATIVE to the repo root — where
    `*` crosses `/` like every other character, which is what makes
    `transformer/*.safetensors` a subtree rule rather than a one-level one.

    **`ignore` wins over `allow`**, as it does there. `include` is ours: a single
    exact filename, for a fetch of one GGUF out of a repo that publishes twenty.
    """
    if include is not None and name != include:
        return False
    if allow and not any(fnmatch.fnmatch(name, pattern) for pattern in allow):
        return False
    if ignore and any(fnmatch.fnmatch(name, pattern) for pattern in ignore):
        return False
    return True


def _repo_files(model_id, include=None, allow=None, ignore=None,
                revision=DEFAULT_REVISION):
    """`(sha, files)` — the commit this listing resolved to, and what to fetch.

    The sha comes back WITH the list because the two must not be decided
    separately. A listing at the repo's default branch paired with a fetch at a
    hardcoded "main" is two sources of truth that agree by coincidence: where
    they differ, we would fetch a genuinely different revision than the list
    implied, record a ref for it, and stay internally consistent while doing it
    — etag matches content, so nothing downstream could ever notice. The
    revision is therefore asked for explicitly (the same `main` hf's own
    `snapshot_download` defaults to, so the fast path and the fallback cannot
    land on different revisions of one model), and the fetch is pinned to the
    SHA that answer resolved to, which also settles the repo moving between the
    listing and the last byte.

    `files` is `(name, size)` for every file this download will ACTUALLY fetch.

    One metadata call, no weights, and ONE place that decides what is in scope —
    the total on the bar and the list the fetch works through come from the same
    filter, or the two disagree and a bar measures itself against files nobody
    is downloading.

    **Scoped, because a repo is rarely fetched whole.** `include` is a single
    filename (one GGUF out of a repo that publishes a dozen quantizations of the
    same model); `allow`/`ignore` are the same fnmatch patterns
    `snapshot_download` takes, applied by `selects` with the same precedence, so
    a download that fetches part of a repo does not measure itself against the
    rest of it.

    Raises, unlike its callers: the fetch cannot proceed on a guess, while the
    bar can.
    """
    from huggingface_hub import HfApi

    info = HfApi().model_info(model_id, revision=revision, files_metadata=True)
    files = []
    for sibling in getattr(info, "siblings", None) or []:
        name = getattr(sibling, "rfilename", None) or ""
        if not name:
            continue
        if not selects(name, include=include, allow=allow, ignore=ignore):
            continue
        files.append((name, getattr(sibling, "size", None)))
    return getattr(info, "sha", None), files


def repo_total_bytes(model_id, include=None, ignore=None):
    """The size of what will ACTUALLY be fetched, from the Hub, or None.

    Without it the bar has no total and shows as indeterminate — which is
    honest, and much better than a wrong total. Summing the whole repo when only
    part of it is being fetched is how a 2.6GB pull came to read as a fraction
    of 30GB and then jump to "complete" against a figure it never downloaded.
    """
    try:
        return _total_bytes(_repo_files(model_id, include=include, ignore=ignore)[1])
    except Exception:  # noqa: BLE001 - a missing total is a cosmetic loss, never fatal
        return None


def _total_bytes(files):
    """What `_repo_files` adds up to, or None for an indeterminate bar."""
    total = sum(size for _name, size in files if isinstance(size, int) and size > 0)
    return total or None


def _capped(done, total):
    """Never report more done than there is to do.

    `bytes_on_disk` measures the whole repo folder, and a SCOPED total covers
    only part of it — so a machine that already holds another quantization of
    the same model would otherwise report 8GB of a 2.6GB download.
    """
    if done is None or total is None:
        return done
    return min(done, total)


def _remove(path):
    with contextlib.suppress(OSError):
        os.remove(path)


def _hf_token():
    """The user's Hub token, or None.

    Sent on OUR requests as well as hf's: a gated repo answers the metadata call
    for an anonymous caller and then 401s on the blob, which reads as a broken
    download rather than as a missing login.
    """
    try:
        from huggingface_hub.utils import get_token

        return get_token()
    except Exception:  # noqa: BLE001 - an unreadable token is an anonymous fetch, not a failure
        return None


def _hub_file_meta(repo_id, filename, revision):
    """Everything one file needs to be fetched and filed: where, and what.

    `location` is the post-redirect CDN/Xet URL — the one worth range-fetching,
    and the one that expires mid-download. `etag` names the blob in the cache
    and `commit` names the snapshot folder its entry lives in; both are hf's own
    layout, so both come from hf rather than from a second derivation here.
    """
    from huggingface_hub import get_hf_file_metadata, hf_hub_url

    url = hf_hub_url(repo_id, filename, revision=revision)
    meta = get_hf_file_metadata(url, token=_hf_token())
    return {"url": url, "location": getattr(meta, "location", None) or url,
            "etag": getattr(meta, "etag", None),
            "commit": getattr(meta, "commit_hash", None),
            "size": getattr(meta, "size", None)}


def _open(url, token, start=None, end=None):
    """One GET, ranged when `start` is given.

    `identity` is not politeness: a gzipped body's bytes are not the file's
    bytes, and every offset here is an offset into the file.
    """
    headers = {"Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if start is not None:
        headers["Range"] = "bytes=%d-%s" % (start, "" if end is None else end)
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT_S)


def _supports_ranges(location, token):
    """Does this URL really serve ranges? One byte answers it.

    A 206 with a parseable `Content-Range`, or no. A server that answers a Range
    request with 200 is saying it will send the whole body every time, and four
    segments of that is four times the download — so a doubtful answer means one
    segment, never an optimistic four.

    Skipped entirely for a file too small to split, so a repo of small configs
    costs no extra request at all. None means the question could not be ASKED,
    which is a different thing from a no — see `_probe_host`.
    """
    try:
        with _open(location, token, 0, 0) as response:
            if getattr(response, "status", 200) != 206:
                return False
            return bool(_CONTENT_RANGE.search(response.headers.get("Content-Range") or ""))
    except _TRANSIENT:
        return None


def _probe_host(location, token, probes):
    """`_supports_ranges`, asked once per HOST for the length of one download.

    Range support belongs to the CDN answering, not to the path: every shard of
    a repo comes off the same host with the same presigning scheme. Asked per
    file it is a serial TLS handshake per file before a single byte moves — the
    same startup cost `_resolve` was parallelised to remove, reintroduced on a
    thirty-shard repo.

    True, False, or None for "could not ask" — and the three are distinct
    because two rules turn on the difference. Only an ANSWER is remembered:
    caching a probe that FAILED lets one transient 503 put every remaining shard
    of the repo on a single connection for the rest of the download, with
    nothing on screen to say the fast path switched itself off. And only an
    answer of NO is grounds for throwing away a recorded layout (see `plan`);
    silence is not.
    """
    host = urllib.parse.urlsplit(location).netloc
    if probes.get(host) is None:
        answer = _supports_ranges(location, token)
        if answer is None:
            return None  # this file goes on one connection; the next re-asks
        probes[host] = answer
    return probes[host]


def _sparse_ok(folder):
    """Can this filesystem hold a pre-sized file without allocating it?

    The whole design writes segments OUT OF ORDER, which means creating each
    part file at its final size up front. Where `ftruncate` allocates instead of
    punching a hole that costs the repo's full size — 25GB reserved before a
    byte downloads, on a filesystem that may not have it — and `bytes_on_disk`,
    which counts allocated blocks, would report 100% from the first second.
    Both are hf's job on such a filesystem, so this is a fallback condition and
    not a bug to work around.

    Asked once per download with a throwaway file, because asking it of the
    first real part file means the zero-fill has already happened.
    """
    probe = os.path.join(folder, ".fusedpart-probe")
    # Bound before the try, not inside it. Today the only escape from that block
    # is an OSError that returns early, so this cannot be read unbound — but that
    # is an argument about which exceptions three syscalls raise, and `_drain`
    # just showed what happens when such an argument stops holding.
    blocks = None
    try:
        os.makedirs(folder, exist_ok=True)
        fd = os.open(probe, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.ftruncate(fd, SPARSE_PROBE_BYTES)
            blocks = getattr(os.fstat(fd), "st_blocks", None)
        finally:
            os.close(fd)
    except OSError:
        return False
    finally:
        _remove(probe)
    return blocks is not None and blocks * 512 < SPARSE_PROBE_BYTES // 2


def _segment_count(size):
    if size < SEGMENT_MIN_BYTES:
        return 1
    return min(MAX_SEGMENTS_PER_FILE, -(-size // SEGMENT_MIN_BYTES))


def _segments(size, count):
    """Split [0, size) into `count` contiguous ranges. `done` is the cursor."""
    span = size // count
    return [{"start": i * span,
             "end": size - 1 if i == count - 1 else (i + 1) * span - 1,
             "done": 0}
            for i in range(count)]


def _seg_complete(seg):
    return seg["start"] + seg["done"] > seg["end"]


class _FileFetch:
    """One file's download: its part file, its segments, its sidecar.

    Owns everything between "we know the etag" and "the snapshot entry exists",
    because those two are the only points at which the state on disk is state hf
    would recognise. Everything in between is ours and carries our own suffix.
    """

    def __init__(self, folder, repo_id, filename, revision, meta, token, stop,
                 probes=None):
        self.folder = folder
        self.repo_id = repo_id
        self.filename = filename
        #: Every name in the repo that resolves to this blob. A repo really does
        #: publish the same bytes twice, and one etag is one blob — so the extra
        #: names are LINKS to make, never a second download to run.
        self.filenames = [filename]
        self.revision = revision
        self.meta = meta
        self.token = token
        self.stop = stop
        self.probes = {} if probes is None else probes
        self.size = meta["size"]
        self.blob = os.path.join(folder, "blobs", meta["etag"])
        self.part = self.blob + PART_SUFFIX
        self.sidecar = self.part + ".json"
        self.snapshot = os.path.join(folder, "snapshots", meta["commit"])
        self.lock = threading.Lock()      # guards the segment cursors
        self.flush_lock = threading.Lock()  # one writer of the sidecar at a time
        self.fd = None
        self.segments = []
        self.pending = 0
        self.flushed = 0.0

    # -- planning ---------------------------------------------------------

    def plan(self):
        """The segments still to fetch. Empty means the bytes are already here.

        Segments share ONE fd, opened read-write and pre-sized, and write
        through `os.pwrite` — no userspace buffering, so bytes a segment has
        counted are bytes the kernel already has. That is precisely what makes a
        `SIGKILL` mid-download resumable rather than merely restartable.
        """
        if os.path.exists(self.blob) and os.path.getsize(self.blob) == self.size:
            # Whatever a previous attempt left is dead the moment the blob
            # exists: nothing will ever resume into it, and unremoved it is a
            # multi-gigabyte leak inside the hub cache that also goes on
            # counting towards the bar.
            _remove(self.part)
            _remove(self.sidecar)
            return []
        os.makedirs(os.path.dirname(self.blob), exist_ok=True)
        saved = self._saved()
        if saved is not None:
            # The layout to resume with is the layout the bytes were fetched
            # INTO. A probe that fails for a moment (a 503 on the one-byte
            # request) must not cost us that: re-deriving on silence yields one
            # segment, a segment-count mismatch, and the deletion of gigabytes
            # of durable, correctly recorded progress — for a network condition
            # that says nothing about the bytes on disk.
            #
            # A probe that ANSWERS NO is different, and asking is what makes the
            # difference visible. Without it, a server that has stopped honouring
            # ranges hands byte 0 to every segment past the first, `_whole_body`
            # refuses, and the refusal takes down the whole repo — the fallback
            # then deleting this file's sidecar along with every OTHER file's
            # progress. Restarting this one file whole is strictly cheaper.
            self.segments = _segments(self.size, len(saved))
            if not self._restore(saved):
                saved = None
            elif len(saved) > 1 and _probe_host(self.meta["location"],
                                                self._cdn_token(),
                                                self.probes) is False:
                saved = None
        if saved is None:
            # …and once the sidecar is out, its layout goes with it. Kept, it
            # would split a download that starts from zero by a number that
            # described a file we just deleted: one connection for a 4.6GB
            # shard, or dozens for a small one.
            count = _segment_count(self.size)
            if count > 1 and _probe_host(self.meta["location"],
                                         self._cdn_token(),
                                         self.probes) is not True:
                count = 1
            self.segments = _segments(self.size, count)
            _remove(self.part)
            _remove(self.sidecar)
        self.fd = os.open(self.part, os.O_RDWR | os.O_CREAT, 0o644)
        os.ftruncate(self.fd, self.size)
        self.flush(force=True)
        pending = [seg for seg in self.segments if not _seg_complete(seg)]
        self.pending = len(pending)
        return pending

    def _cdn_token(self):
        """The credential to send with the BLOB request — usually none.

        huggingface_hub drops `Authorization` the moment the download URL
        differs from the Hub URL, and S3 is the reason: a presigned URL already
        carries its credentials in the query string, and a request bearing two
        authentication mechanisms is refused with a 400. Sent anyway, the probe
        fails and every segment burns its whole retry budget for any user with a
        token set — which is everyone pulling a gated model — and the download
        falls back to something SLOWER than what this replaced, silently,
        because the fallback is invisible by design.

        Computed per request rather than stored: `_re_resolve` can hand us a
        location on a different host than the one we started on.
        """
        return None if self.meta["location"] != self.meta["url"] else self.token

    def _saved(self):
        """The segments a previous run recorded for THIS file, or None.

        Identity first — etag, size, and a part file still as long as it was —
        because a sidecar belonging to a different revision of the file would
        have us skip bytes that were never fetched, and the result is a blob of
        exactly the right length that is silently wrong. The layout itself is
        checked in `_restore`, against the segments derived from this answer.
        """
        try:
            with open(self.sidecar) as handle:
                state = json.load(handle)
            if state["etag"] != self.meta["etag"] or state["size"] != self.size:
                return None
            saved = state["segments"]
            if not saved or os.path.getsize(self.part) < self.size:
                return None
            return saved
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _restore(self, saved):
        """Put those offsets back, or say no.

        Validated in full BEFORE a single cursor is moved, so a half-accepted
        sidecar cannot land either.
        """
        try:
            if len(saved) != len(self.segments):
                return False
            for seg, old in zip(self.segments, saved):
                if old["start"] != seg["start"] or old["end"] != seg["end"]:
                    return False
                if not 0 <= old["done"] <= seg["end"] - seg["start"] + 1:
                    return False
        except (KeyError, TypeError):
            return False
        for seg, old in zip(self.segments, saved):
            seg["done"] = old["done"]
        return True

    def flush(self, force=False):
        """Record the offsets, durably, at most once a second.

        The ORDER here is the correctness argument for the whole feature:
        snapshot the cursors, fsync the DATA, then write the snapshot down.
        Recorded offsets are therefore always bytes the disk already has, never
        bytes still in flight — which a kill would lose while the sidecar went
        on claiming them, and a resume would then skip.

        Driven by the writing threads rather than by a timer of its own: a
        segment that is not moving has nothing new to record, and a thread would
        be one more thing to shut down. Written atomically, because a torn
        sidecar loses the whole download.
        """
        with self.lock:
            now = time.monotonic()
            if not force and now - self.flushed < FLUSH_EVERY_S:
                return
            self.flushed = now
            state = {"etag": self.meta["etag"], "size": self.size,
                     "segments": [dict(seg) for seg in self.segments]}
        with self.flush_lock:
            if self.fd is not None:
                os.fsync(self.fd)
            tmp = self.sidecar + ".tmp"
            with open(tmp, "w") as handle:
                json.dump(state, handle)
            os.replace(tmp, self.sidecar)

    # -- moving the bytes -------------------------------------------------

    def run(self, seg):
        """Fill one segment, reconnecting until it is done or the budget is out.

        Reconnects on both kinds of interruption: an exception, and a body that
        simply ends early — a server closing mid-stream raises nothing.

        The budget resets on the CURSOR MOVING across a whole attempt, which is
        not the same as bytes arriving, and the difference is both a hang and an
        abort. Bytes arriving is too generous: a server that ignores `Range` and
        truncates hands back the same prefix every time, `_whole_body` rewinds
        the cursor to zero to take it safely, and a budget keyed on bytes never
        expires — the job hangs with the bar oscillating between 0% and 50%
        until someone kills the process. And it is too mean, because it was read
        from a drain that a raising `read()` never returns from: half a gigabyte
        on disk followed by a connection reset counted as a failed attempt, so a
        link that resets reliably exhausted the budget and took the whole
        multi-file download into a fallback that then deleted every recorded
        byte. The cursor before and after answers both.
        """
        ranged = len(self.segments) > 1
        refreshed = False
        tries = 0
        reason = "nothing was attempted"
        while tries < SEGMENT_ATTEMPTS and not self.stop.is_set():
            if _seg_complete(seg):
                return
            start = seg["start"] + seg["done"]
            want_range = ranged or seg["done"] > 0
            before = seg["done"]
            try:
                with _open(self.meta["location"], self._cdn_token(),
                           start if want_range else None,
                           seg["end"] if want_range else None) as response:
                    if want_range:
                        if getattr(response, "status", 200) != 206:
                            start = self._whole_body(seg)
                        else:
                            self._check_range(response, start)
                    self._drain(response, seg, start)
                if _seg_complete(seg):
                    return
                reason = f"the stream ended at byte {seg['start'] + seg['done']}"
            except urllib.error.HTTPError as error:
                if error.code in (401, 403) and not refreshed:
                    # `location` is a presigned CDN URL and a multi-hour
                    # download outlives it. Re-resolving does NOT count against
                    # the budget: an expired signature is not evidence that the
                    # file is unreachable. Its own failure is an ordinary
                    # network fault and must be COUNTED rather than escape —
                    # otherwise one unlucky moment aborts the whole download.
                    refreshed = True
                    try:
                        self._re_resolve()
                        continue
                    except _Unsegmentable:
                        raise
                    except Exception as again:  # noqa: BLE001 - hf raises its own family
                        reason = f"re-resolving after HTTP {error.code}: {again}"
                else:
                    reason = f"HTTP {error.code}"
            except _TRANSIENT as error:
                reason = f"{error.__class__.__name__}: {error}"
            if seg["done"] > before:
                # The cursor moved, so the connection worked, so BOTH allowances
                # come back. The re-resolve is one per stall and not one per
                # segment: a presigned URL is good for minutes and a
                # multi-gigabyte download is not, so a second expiry is ordinary
                # — and unhandled it spends the whole retry budget on 401s and
                # aborts into a fallback that then deletes the resumable state.
                tries, refreshed = 0, False
            else:
                tries += 1
                time.sleep(min(5.0, RETRY_BACKOFF_S * tries))
        if self.stop.is_set():
            return
        raise RuntimeError(f"{self.filename}: gave up at byte "
                           f"{seg['start'] + seg['done']} — {reason}")

    def _re_resolve(self):
        """A fresh presigned URL for this file. Only the LOCATION may change.

        `etag`, `size` and `commit` are what the blob path, every segment offset
        and the snapshot folder were derived from before any thread started. A
        repo updated mid-download therefore has to abort, never continue: the
        new revision's bytes written at the old revision's offsets and published
        as `blobs/<old-etag>` are a mix of two revisions at exactly the right
        length, under a name hf will then serve from cache forever.
        """
        fresh = _hub_file_meta(self.repo_id, self.filename, self.revision)
        for field in ("etag", "size", "commit"):
            if fresh.get(field) != self.meta[field]:
                raise _Unsegmentable(
                    f"{self.filename}: the repo changed mid-download "
                    f"({field} {self.meta[field]!r} -> {fresh.get(field)!r})")
        self.meta = fresh

    def _check_range(self, response, start):
        """A 206 is not a promise that it is the range we ASKED for.

        A proxy that clamps ranges answers `bytes=1150000-` with `Content-Range:
        bytes 0-…/size` — the scattering `_whole_body` refuses, wearing a legal
        status code. Written where it was asked for, one body's bytes land at
        four different offsets and the file is exactly the right LENGTH and
        entirely wrong content.
        """
        header = (response.headers.get("Content-Range") or "").strip()
        match = _RANGE_START.match(header)
        if not match or int(match.group(1)) != start:
            raise _Unsegmentable(
                f"{self.filename}: asked for byte {start}, got "
                f"{header or 'a 206 with no Content-Range'}")

    def _whole_body(self, seg):
        """Handle a 200 answering a request we ranged, or refuse to.

        A server that answered the probe with a 206 and then ignores `Range` is
        sending byte 0 to every segment. Writing that at a segment's own offset
        produces a file of exactly the right LENGTH and entirely wrong content —
        the one failure mode of this whole design that no size check would
        catch. Only the segment that starts at zero can use such a body, and it
        has to rewrite from the top rather than resume into it.
        """
        if seg["start"]:
            raise _Unsegmentable(
                f"{self.filename}: the server ignored Range on a segment "
                f"starting at byte {seg['start']}")
        with self.lock:
            seg["done"] = 0
        return 0

    def _drain(self, response, seg, start):
        """Copy this response into the part file, advancing the cursor as it goes.

        Deliberately reports nothing: what an attempt achieved is the cursor's
        movement, which the caller can still read after this raises — and a
        `read()` raising mid-body, on top of bytes already written, is the case
        a returned flag got wrong.

        Three paths leave without the loop body ever running — an empty first
        read, no room left in the segment, and `stop` already set on entry —
        and the last of those is the ORDINARY one: `stop` is set exactly when a
        sibling segment has failed, so every other segment arrives here to wind
        down. A vestigial `return moved` survived the rewrite of this function
        and raised `UnboundLocalError` on all three, which is a `NameError` and
        therefore not in `_TRANSIENT`: it escaped the retry loop entirely and
        turned a tidy wind-down into the fallback deleting every recorded byte.
        """
        offset = start
        while not self.stop.is_set():
            chunk = response.read(READ_BYTES)
            if not chunk:
                break
            # A server ignoring the END of the range must not overrun into the
            # next segment's bytes.
            room = seg["end"] - (seg["start"] + seg["done"]) + 1
            if room <= 0:
                break
            chunk = chunk[:room]
            written = 0
            while written < len(chunk):
                written += os.pwrite(self.fd, chunk[written:], offset + written)
            offset += len(chunk)
            with self.lock:
                seg["done"] += len(chunk)
            self.flush()

    # -- publishing -------------------------------------------------------

    def finish(self):
        """Publish the blob and link it. The LAST segment's thread runs this.

        Checked against the CURSORS, never against the part file's length. The
        file is `ftruncate`d to its final size before a byte arrives, so its
        length is right from the first second and a sparse file of pure holes
        passes a size check — which would put a zero-filled blob under a real
        etag into the hub cache, where hf serves it from cache forever. The
        cursors are the same durable-byte accounting the sidecar records, and
        they are the only evidence there is that the file is whole.

        No hash, like huggingface_hub itself, which relies on TLS and
        `Content-Length`: re-reading every gigabyte off the disk would give back
        a good part of what this feature is for.
        """
        if self.fd is not None:
            os.fsync(self.fd)
            landed = sum(seg["done"] for seg in self.segments)
            missing = [seg for seg in self.segments if not _seg_complete(seg)]
            if missing or landed != self.size:
                raise RuntimeError(
                    f"{self.filename}: {landed} of {self.size} bytes landed, "
                    f"{len(missing)} segment(s) short")
            os.close(self.fd)
            self.fd = None
            os.replace(self.part, self.blob)
            _remove(self.sidecar)
        return self.link()

    def link(self):
        """The snapshot entry hf's own loaders read.

        A RELATIVE symlink into `blobs/`, matching hf's `_create_symlink`: an
        absolute one breaks the moment the cache is moved or read through
        another mount. Windows without developer mode cannot make one at all,
        and hf's own answer there is a copy, so ours is too.
        """
        targets = [os.path.join(self.snapshot, name) for name in self.filenames]
        for target in targets:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            relative = os.path.relpath(self.blob, os.path.dirname(target))
            _remove(target)
            try:
                os.symlink(relative, target)
            except OSError:
                shutil.copyfile(self.blob, target)
        return targets[0]

    def close(self):
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None


def _resolve(repo_id, filenames, revision):
    """One metadata call per file, concurrently.

    Serially this is a round trip per file before a single byte moves, which on
    a repo of thirty shards is several seconds of nothing happening — the exact
    thing this feature exists to remove.
    """
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MAX_CONNECTIONS, len(filenames)),
            thread_name_prefix="meta") as pool:
        return list(pool.map(
            lambda name: _hub_file_meta(repo_id, name, revision), filenames))


def _run_segment(fetch, seg):
    """One unit of work in the pool: fill a segment, and finalise if it was the
    last one its file was waiting on."""
    try:
        try:
            fetch.run(seg)
        finally:
            fetch.flush(force=True)
        if not _seg_complete(seg):
            return  # abandoned because a sibling segment failed; nothing to publish
        with fetch.lock:
            fetch.pending -= 1
            last = fetch.pending == 0
        if last:
            fetch.finish()
    except BaseException:
        # The other segments stop pulling bytes nobody is going to use, and
        # what they already wrote is recorded before they go. That state is for
        # a LATER RUN of the app: this attempt is about to hand the repo to
        # huggingface_hub, which fetches those files itself (see `_clear_parts`).
        #
        # `finish()` is INSIDE this guard, not after it. It fails for reasons
        # that have nothing to do with the download — a full disk, an
        # `os.replace` across devices, another instance publishing the same blob
        # first — and outside the guard that exception reached the caller with
        # `stop` still clear, so the pool's own shutdown waited for every
        # remaining segment of every remaining file to finish first: minutes and
        # gigabytes spent on a download that had already failed.
        fetch.stop.set()
        raise


def _segmented_fetch(model_id, filenames, revision, ref=DEFAULT_REVISION):
    """Fetch `filenames` into the hub cache ourselves. Returns the snapshot dir.

    `revision` is REQUIRED and has no default on purpose: it must be the commit
    the caller's file list resolved to, and a default here is exactly how a list
    taken from one revision came to be fetched at another. `ref` is the branch
    NAME that resolved to it, recorded so a later offline load can resolve the
    same name — hf writes that ref too, and a cache without it needs the network
    to answer a question it already knows.

    The units of work in the pool are SEGMENTS ACROSS ALL FILES under one cap,
    which is what makes `MAX_CONNECTIONS` mean what it says: a pool per file
    would multiply the two caps together and open thirty sockets on a repo of
    thirty shards.

    **No cache lock, unlike `snapshot_download`, and deliberately.** Two app
    instances fetching one repo would write the SAME bytes at the SAME offsets:
    the etag names the content, so there is no version of this race that puts
    wrong bytes in a blob. What can happen is wasted work — one instance's
    `os.replace` pulls the part file out from under the other, whose next
    syscall fails and whose download falls back to hf — and that costs a slower
    download, never a corrupt cache. Inside one app it cannot happen at all:
    the supervisor's deterministic job id joins a second Download of a model
    onto the first (AI-5a).
    """
    if not hasattr(os, "pwrite"):
        # Windows. Buffered seek-and-write would break the guarantee the whole
        # design rests on — that a counted byte is a written byte.
        raise _Unsegmentable("os.pwrite is unavailable on this platform")
    folder = repo_folder(model_id)
    if not folder:
        raise _Unsegmentable("the hub cache layout is unavailable")
    if not filenames:
        raise _Unsegmentable("the Hub listed no files for this repo")
    if not _sparse_ok(folder):
        raise _Unsegmentable(f"{folder} cannot hold a sparse file")

    token = _hf_token()
    stop = threading.Event()
    probes = {}  # host -> range support, so a repo of shards probes once
    fetches, by_etag = [], {}
    for name, meta in zip(filenames, _resolve(model_id, filenames, revision)):
        if not (isinstance(meta.get("size"), int) and meta.get("etag")
                and meta.get("commit")):
            raise _Unsegmentable(f"{name}: the Hub reported no size, etag or commit")
        already = by_etag.get(meta["etag"])
        if already is not None:
            # One etag is one blob, and a repo really does publish the same
            # bytes under two names. A second fetch of it would share the part
            # file, the sidecar and the blob path with the first: the bytes
            # pulled twice, and whichever `os.replace` lost the race finding
            # nothing there and taking the whole download into the fallback.
            already.filenames.append(name)
            continue
        fetch = _FileFetch(folder, model_id, name, revision, meta, token, stop,
                           probes)
        by_etag[meta["etag"]] = fetch
        fetches.append(fetch)

    commits = {fetch.meta["commit"] for fetch in fetches}
    if len(commits) != 1:
        # One revision is one commit. Two would mean the repo moved under us
        # mid-listing, and half a snapshot of each is not a snapshot.
        raise _Unsegmentable(f"one revision reported {len(commits)} commits")
    if _COMMIT_SHA.match(revision or "") and revision not in commits:
        # Asked for a commit and given another: the listing this file set came
        # from no longer describes what the Hub is serving.
        raise _Unsegmentable(f"asked for commit {revision}, the Hub resolved "
                             f"{commits.copy().pop()}")

    try:
        work = []
        for fetch in fetches:
            pending = fetch.plan()
            if pending:
                work.extend((fetch, seg) for seg in pending)
            else:
                fetch.finish()  # already on disk, or restored complete: just file it
        if work:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(MAX_CONNECTIONS, len(work)),
                    thread_name_prefix="fetch") as pool:
                futures = [pool.submit(_run_segment, f, seg) for f, seg in work]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
    finally:
        for fetch in fetches:
            fetch.close()

    _write_ref(folder, ref, commits.pop())
    return fetches[0].snapshot


def _write_ref(folder, ref, commit):
    """`refs/<branch>` -> the commit, so a later load resolves it offline.

    Only for a branch NAME: a revision that is itself a sha needs no ref, and
    writing one named after a sha is not something hf would ever read.
    """
    if not ref or _COMMIT_SHA.match(ref):
        return
    path = os.path.join(folder, "refs", ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(commit)


def _clear_parts(folder):
    """Drop our partial files before handing the repo back to huggingface_hub.

    Not because hf would misread them — the suffix exists precisely so it never
    sees them — but because hf is about to fetch those same files ITSELF. Kept,
    they would count towards the progress bar for a download nothing is writing,
    and then sit in the hub cache forever beside the blob hf finished: nothing
    ever resumes into a part file whose blob already exists.

    So the resume story is honest about its scope. It covers the app being
    killed, quit or crashed — the case that motivated it (AI-5e) — and not a
    fetch that failed its way into the fallback, which re-downloads.
    """
    for dirpath, _dirs, files in os.walk(folder or ""):
        for name in files:
            if PART_SUFFIX in name:
                _remove(os.path.join(dirpath, name))


def _fallback(model_id, error):
    """Say why we are back on hf's downloader. The supervisor captures stderr,
    so a fallback that happens in the field is diagnosable rather than merely
    slow."""
    sys.stderr.write(
        f"[fused] segmented fetch of {model_id} unavailable, falling back to "
        f"huggingface_hub: {error.__class__.__name__}: {error}\n")
    _clear_parts(repo_folder(model_id))


def fetch_with_progress(model_id, call, total=None, detail="Fetching weights…",
                        job=None, row=None):
    """Run `call()` on a thread, reporting bytes-on-disk once a second.

    `call` is whatever huggingface_hub function actually fetches — a whole
    snapshot for one runner, a single GGUF file for another — and this is the
    part neither of them should write twice: the poll is the progress AND the
    heartbeat, without which a long single-file download reports nothing for
    minutes and the manager calls the row abandoned.

    **`job`/`row` exist because not every fetch belongs to a download job.** A
    runner that pulls a component model DURING a request — the speech detector,
    the two diarization models — is reporting into a row the supervisor opened
    for a transcription, not into this process's `JOB_ID` (which is the model's
    own load row, long since finished). `job` sends the tick to the right row;
    `row` is that row's IDENTITY (`supervisor.transcribe_row_fields`), restated
    on every tick because the manager can evict and rebuild any row at any tick
    and a report with no `title` is refused outright.

    `kind`/`unit` are this function's own and override the row's, deliberately:
    for the length of the fetch the row IS a download and 6MB of 26MB is what a
    person wants to see. The next tick from the work itself restates the row's
    own pair, so the flip is for the duration and not a rename.

    **The tick carries the ✕ back**, and that became load-bearing the moment a
    fetch could land on a transcription row. It ticked with a plain `report`
    while these fetches owned a model-load row, whose ✕ the supervisor answers
    by killing the process — so nothing here had to. A component fetch reports
    into a row whose `cancellable` is True and whose ✕ must stop THIS work, and
    with a plain `report` the user pressed it, the manager set
    `cancel_requested`, and 33MB carried on downloading behind a row that went
    on saying "running". The reply to the tick we were sending anyway is the
    only channel that reaches a thread parked inside huggingface_hub.

    `CANCEL` is consulted too, but ONLY when `job` was passed. That flag is the
    `/cancel` route's, it belongs to the generation holding `GENERATE_LOCK`, and
    it is cleared by `_single`/`_stream` on the way in — so it means this fetch
    exactly when this fetch is inside a request. A model download runs on
    `_bring_up`'s own thread with no such lock, where a flag left set by an
    earlier cancelled generation would abort a download nobody asked to stop.

    **A ✕ that lands as the fetch FINISHES is not honoured**, which is the same
    rule `_call_with_ticks` states: the bytes are on the disk, and throwing them
    away would make the next attempt re-download what this one already has. So
    the final report is a plain `report`. The abandoned fetch thread is a
    daemon nobody waits for — it finishes into a result that is discarded, and
    huggingface_hub resumes partial files, so the bytes are not lost either.
    """
    folder = repo_folder(model_id)
    if total is None:
        total = repo_total_bytes(model_id)
    identity = {**(row or {}), "kind": "download", "unit": "bytes"}

    def tick(**fields):
        """One progress report that can carry a ✕ back. See the docstring."""
        report_or_cancel(job=job, **identity, state="running", **fields)
        if job is not None and CANCEL.is_set():
            raise Cancelled()

    tick(detail=detail, done=_capped(bytes_on_disk(folder), total), total=total)

    result = {}

    def run():
        try:
            result["value"] = call()
        except BaseException as e:  # noqa: BLE001 - carried out and re-raised on the caller's thread
            result["error"] = e

    thread = threading.Thread(target=run, name="fetch", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(timeout=1.0)
        if not thread.is_alive():
            # Finished during the join. Ticking now would be the late-cancel
            # the docstring refuses — the bytes are already on the disk.
            break
        tick(done=_capped(bytes_on_disk(folder), total), total=total,
             detail=detail)
    if "error" in result:
        raise result["error"]
    # Land on the total rather than on the last walk: the snapshot symlinks are
    # not counted, so a finished repo measures slightly under its own size and a
    # bar that stopped at 98% reads as a download that gave up.
    report(job=job, **identity, state="running",
           done=total or bytes_on_disk(folder), total=total)
    return result["value"]


# ------------------------------------------------------- the already-cached path
#
# **A model already complete on disk is resolved WITHOUT touching the network**,
# and the reason is that "Fetching weights…" for a cached model was costing about
# a second of wall clock before any weight was read. Measured on this machine for
# `mlx-community/whisper-tiny.en-8bit`, fully cached: `download_snapshot` 483ms
# and `download_file` 456ms, against ~14ms for the actual `load()` inside
# mlx-whisper. All of it is Hub round-trips — `HfApi().model_info(files_metadata=
# True)` is 228ms on its own and hf's own `snapshot_download` spends another
# ~220ms revalidating etags — and it is also the source of the "You are sending
# unauthenticated requests to the HF Hub" line in every worker log. The same two
# answers off the cache alone are 0.13ms and 0.14ms.
#
# **The trade, stated so the next reader does not have to rediscover it: a model
# already complete on disk will NOT pick up a newer Hub revision.** Nothing here
# re-checks `main` once the cache can answer, so a repo that was re-uploaded
# under the same branch keeps serving the bytes this machine already has until
# something else forces a re-check (a cache clear, a fetch of a file this
# snapshot does not have, or a caller that scopes the download differently).
# That is deliberate (D359): bring-up latency and working offline are worth more
# here than revision freshness, because these are pinned model snapshots a user
# downloaded on purpose rather than a moving dependency — and a silently changing
# set of weights under a name the user chose would be the worse surprise anyway.
#
# What must NOT change is a first download, and that is the whole shape of this:
# the local attempt either answers completely or it is discarded, and everything
# below it — the metadata call, the total, the segmented fetch, the progress
# reporting — runs exactly as it did before.

#: hf's own marker for a blob it is still writing. Ours is `PART_SUFFIX`.
_HF_PART_SUFFIX = ".incomplete"


def _has_partial_files(folder):
    """Whether an interrupted download left anything behind in `folder`.

    Recent `huggingface_hub` verifies snapshot completeness against its own
    cached tree listing and raises `LocalEntryNotFoundError` for a partial one —
    but a runner venv resolves `huggingface_hub` unpinned, so this file does not
    get to assume that check exists, and the cost of trusting a partial cache is
    a `load()` that fails on a missing shard instead of a download that finishes.
    The marker files are right there, so ruling it out is one cheap walk of a
    folder that holds tens of files.
    """
    if not folder:
        return False
    for _dirpath, _dirs, files in os.walk(folder):
        for name in files:
            if name.endswith(_HF_PART_SUFFIX) or name.endswith(PART_SUFFIX):
                return True
    return False


def _cached_path(model_id, resolve):
    """`resolve()`'s answer if the cache can serve it with NO network, else None.

    `resolve` is the hf call with `local_files_only=True` — the same function and
    the same arguments the networked path uses, so the local answer cannot be a
    differently-scoped one.

    **Every failure is a None**, deliberately broad: hf raises
    `LocalEntryNotFoundError` for a cache that cannot answer (verified against
    huggingface_hub 1.28 — an absent repo comes back in 0.08ms), an `OSError` for
    a cache directory it cannot read, and an `ImportError` is what a venv without
    the library looks like. None of those is a failure worth reporting, because
    the answer to all of them is the networked path this returns to — which does
    its own error handling and its own degradation. Catching narrowly here would
    turn a new hub error type into a broken bring-up instead of a slow one.

    The path is checked before it is trusted: it comes from a call this file did
    not make itself, and a cache directory removed under a resolved ref would
    otherwise be handed to `load()` as a snapshot.
    """
    if _has_partial_files(repo_folder(model_id)):
        return None
    try:
        path = resolve()
    except Exception:  # noqa: BLE001 - see the docstring: the answer is always the networked path
        return None
    return path if path and os.path.exists(path) else None


def download_snapshot(model_id, allow_patterns=None, ignore_patterns=None, **kwargs):
    """The repo, with progress. What most runners mean by "download".

    The total is measured against the SAME patterns the download uses, or a pull
    that deliberately fetches part of a repo measures itself against weights it
    was never going to fetch — a bar that stalls partway and then jumps. The
    segmented fetch takes its file list from the same filter, for the same
    reason.

    Both scopes are first-class arguments rather than `**kwargs` precisely so
    that they reach `_repo_files` too: an `allow_patterns` that only reached
    `snapshot_download` would fetch a tenth of a repo behind a bar priced at all
    of it.
    """
    def local():
        from huggingface_hub import snapshot_download

        return snapshot_download(model_id, allow_patterns=allow_patterns,
                                 ignore_patterns=ignore_patterns,
                                 local_files_only=True, **kwargs)

    # Already on disk, in scope and complete? Then there is nothing to download
    # and nothing to report: no metadata call, no etag revalidation, no bar that
    # fills in one tick. See the note above this function for what that costs in
    # revision freshness. The patterns and any extra kwargs go through unchanged,
    # so what the cache is asked for is exactly what would have been fetched.
    cached = _cached_path(model_id, local)
    if cached:
        return cached

    # ONE listing, serving the bar's total, the list to fetch AND the revision
    # to fetch it at. Asking twice is a second round trip before any byte moves;
    # deciding the revision separately is how a list from one revision comes to
    # be fetched at another.
    sha, files, total = None, None, None
    try:
        sha, files = _repo_files(model_id, allow=allow_patterns,
                                 ignore=ignore_patterns)
        total = _total_bytes(files)
    except Exception as error:  # noqa: BLE001 - the bar can proceed on a guess; the fetch cannot
        _fallback(model_id, error)

    def hub():
        from huggingface_hub import snapshot_download

        return fetch_with_progress(
            model_id,
            lambda: snapshot_download(model_id, allow_patterns=allow_patterns,
                                      ignore_patterns=ignore_patterns, **kwargs),
            total=total)

    if kwargs or files is None or not sha:
        # An extra argument changes WHAT is fetched — `allow_patterns`, a
        # revision, a local dir — and a fetch that quietly ignored one would
        # download the wrong thing. Ours honours exactly the two it knows about.
        # A listing with no sha is the same problem: nothing to pin to.
        return hub()
    try:
        names = [name for name, _size in files]
        return fetch_with_progress(
            model_id, lambda: _segmented_fetch(model_id, names, sha), total=total)
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - every failure degrades to hf's downloader
        _fallback(model_id, error)
    return hub()


def download_file(repo_id, filename, detail=None, job=None, row=None):
    """One file out of a repo — a GGUF checkpoint, say — with progress.

    The total is THAT FILE's size, not the repo's. A repo that publishes a dozen
    quantizations of the same model sums to tens of gigabytes, and measuring a
    2.6GB pull against that is how a download reads as barely started for its
    whole life and then jumps to complete.

    `job`/`row` for a fetch that happens inside a REQUEST rather than inside a
    download — the diarization models on the first `diarize: true`, the speech
    detector on a machine whose Download predates it. Without them the tick goes
    to this process's `JOB_ID`, which is the model's own load row: finished,
    and reopened as a running download of something the user never asked for
    while the row they ARE watching says nothing. See `fetch_with_progress`.
    """
    def local():
        from huggingface_hub import hf_hub_download

        return hf_hub_download(repo_id=repo_id, filename=filename,
                               local_files_only=True)

    # The same fast path `download_snapshot` takes, and it matters most for the
    # SMALL components: the 2MB speech detector and the two diarization models
    # are fetched inside a transcription, so on a warm cache their 456ms each was
    # latency a user waits through on the way to a transcript they already had
    # the bytes for.
    cached = _cached_path(repo_id, local)
    if cached:
        return cached

    # One listing here too, for the revision as much as for the total: a GGUF
    # fetched at a revision its listing never described is the same bug as a
    # whole snapshot fetched that way, one file wide.
    sha, total = None, None
    try:
        sha, files = _repo_files(repo_id, include=filename)
        total = _total_bytes(files)
    except Exception as error:  # noqa: BLE001 - the bar can proceed on a guess; the fetch cannot
        _fallback(repo_id, error)
    detail = detail or f"Fetching {filename}…"

    def hub():
        from huggingface_hub import hf_hub_download

        return fetch_with_progress(
            repo_id,
            lambda: hf_hub_download(repo_id=repo_id, filename=filename),
            total=total, detail=detail, job=job, row=row)

    if not sha:
        return hub()
    try:
        return fetch_with_progress(
            repo_id,
            lambda: os.path.join(_segmented_fetch(repo_id, [filename], sha),
                                 filename),
            total=total, detail=detail, job=job, row=row)
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - every failure degrades to hf's downloader
        _fallback(repo_id, error)
    return hub()


# -------------------------------------------------------------------- bring-up


def _bring_up(model_id, download, load):
    """download -> load -> ready, on its own thread, reporting every step.

    `load` is handed what `download` returned — a snapshot path, a dict of
    paths — rather than resolving the files a second time. The first cut had
    `load` call the downloader again for its path, which re-ran the Hub metadata
    call and re-reported a finished download on every load of a cached model.
    """
    try:
        set_state(state="downloading", detail="Fetching weights…")
        fetched = download(model_id)

        set_state(state="loading", detail="Loading weights into memory…")
        # No total: this is one long opaque step, and an invented percentage is
        # what makes live work read as frozen.
        report(kind="task", unit="", done=None, total=None,
               detail="Loading weights into memory…")
        load(model_id, fetched)

        set_state(state="ready", detail="", error="",
                  resident_bytes=resident_bytes(), loaded_at=time.time())
        # That figure is already stale — with lazy, memory-mapped weights most
        # of the model has not been touched yet. `/health` re-measures on every
        # poll, which is what the number on screen actually comes from.
        report(state="done", detail="Model loaded")
    except Cancelled:
        # **A ✕ is not a failure, and saying it is costs more than a wrong word.**
        # A terminal `state="error"` on the row CLEARS `cancel_requested`
        # (`jobs.upsert`: a finished job cannot be cancelled) — so the
        # supervisor's own poll, which is the thing that would have written the
        # right verdict half a second later, can no longer see the ✕ that caused
        # this at all. It then reads /health, finds "error", and reports the
        # download the user stopped as a load that crashed.
        #
        # The health state stays "error" because that is the only non-ready
        # terminal this contract has, and the supervisor's post-spawn loop is
        # watching for exactly it; `error="cancelled"` is the literal string
        # `_failure_text`/`_bring_up` switch on, so the supervisor's independent
        # verdict AGREES with the row instead of overwriting it.
        set_state(state="error", error="cancelled")
        report(state="cancelled")
    except BaseException as e:  # noqa: BLE001 - this thread's only job is to explain a failure
        # Deliberately broad and deliberately last: this thread is the only
        # thing that can say why a load failed, and an unhandled exception here
        # would leave /health saying "loading" forever.
        message = describe_failure(e)
        set_state(state="error", error=message)
        report(state="error", message=message)
        traceback.print_exc(file=sys.stderr)


# ----------------------------------------------------------------- HTTP server


def _handler(generate, streaming):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # the supervisor captures stderr; per-request noise is not useful

        def _authorized(self):
            # The token is a header the supervisor generated and passed in this
            # process's environment. A foreign page that guessed the ephemeral
            # port still cannot drive the model, and the value never lands in a
            # log line or a Referer.
            if TOKEN and self.headers.get("X-Fused-Worker") == TOKEN:
                return True
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authorized():
                return
            if self.path.startswith("/health"):
                # Measured HERE, not read back from the state set at load time.
                # It used to be stored once, right after `load()` returned, and
                # served unchanged forever — so the supervisor's `refresh_memory`
                # re-read the same frozen number every poll, and a model whose
                # weights fault in during its first generation was reported at
                # whatever it happened to cost before it had done anything.
                health = snapshot()
                if health.get("state") == "ready":
                    health["resident_bytes"] = resident_bytes()
                self._json(health)
                return
            self._json({"error": "not found"}, status=404)

        def do_POST(self):
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                self._json({"error": "body must be JSON"}, status=400)
                return

            if self.path.startswith("/quit"):
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.1), os._exit(0)),
                                 daemon=True).start()
                return

            if self.path.startswith("/cancel"):
                CANCEL.set()
                self._json({"ok": True})
                return

            if self.path.startswith("/generate"):
                if snapshot()["state"] != "ready":
                    self._json({"error": "the model is not loaded"}, status=409)
                    return
                # NOT cleared here. `CANCEL` belongs to the generation that is
                # RUNNING, and this handler may be a second request waiting for
                # `GENERATE_LOCK`: clearing before the lock erases the ✕ the
                # user just pressed for the first one, which then runs to
                # completion under a Stop that appeared to work. Each generation
                # clears the flag once it owns the lock — see `_generation`.
                if streaming:
                    self._stream(body)
                else:
                    self._single(body)
                return

            self._json({"error": "not found"}, status=404)

        def _single(self, body):
            """One JSON reply, for work that produces an ARTEFACT rather than a
            stream. An image is not a sequence of tokens, and pretending it is
            would buy nothing — its progress is steps, and those go to the job
            row where the download manager can already draw them."""
            with GENERATE_LOCK, heartbeat():
                CANCEL.clear()
                try:
                    self._json({"ok": True, "result": generate(body)})
                except Cancelled:
                    self._json({"ok": True, "cancelled": True})
                except BaseException as e:  # noqa: BLE001 - must reach the client
                    traceback.print_exc(file=sys.stderr)
                    self._json({"ok": False, "error": describe_failure(e)})

        def _stream(self, body):
            """NDJSON, chunked. `{"type":"chunk"}` lines closed by
            `{"type":"done"}` — the shape `fused.ai`'s reader already speaks."""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def write(payload):
                line = (json.dumps(payload) + "\n").encode()
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()

            with GENERATE_LOCK, heartbeat():
                CANCEL.clear()
                try:
                    generate(body, write)
                except BaseException as e:  # noqa: BLE001 - must reach the client
                    write({"type": "done", "ok": False,
                           "error": describe_failure(e)})
                    traceback.print_exc(file=sys.stderr)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    return Handler


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET


def build_server(generate, streaming=False, host="127.0.0.1"):
    """The HTTP half on an ephemeral port, unstarted. Split out of `serve` so a
    test can drive the real routes without spawning a process."""
    return _Server((host, 0), _handler(generate, streaming))


def serve(download, load, generate, streaming=False, memory=None, argv=None):
    """Parse the supervisor's argv and run this worker. Does not return.

    `--download-only` fills the cache and exits; the exit CODE is the answer
    there, because the supervisor waits on the process rather than on a health
    route, so a failure must not be swallowed into a status nobody reads.
    """
    global JOB_ID, _measure

    _measure = memory
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    # Not required: a download-only run serves nothing, so it has no port to
    # publish and no status file to publish it in.
    parser.add_argument("--status", default="")
    parser.add_argument("--job", default="")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args(argv)
    JOB_ID = args.job
    set_state(model=args.model)

    if args.download_only:
        try:
            download(args.model)
        except Cancelled:
            # Still non-zero — the weights are not on the disk and a zero would
            # report the download DONE — but not a traceback: `_fetch_only`
            # tails this log for the message it puts on a failed row, and a
            # stack trace for something the user deliberately pressed is the
            # noise that made a cancel look like a crash. The supervisor tells
            # the two apart by the row's own ✕, not by what is written here.
            sys.stderr.write("cancelled\n")
            sys.exit(1)
        except BaseException as e:  # noqa: BLE001 - stderr is the supervisor's report
            traceback.print_exc(file=sys.stderr)
            sys.stderr.write(f"\n{e.__class__.__name__}: {e}\n")
            sys.exit(1)
        sys.exit(0)

    if not args.status:
        sys.stderr.write("--status is required unless --download-only\n")
        sys.exit(2)

    # Bind :0 and publish what we got. Anything the parent reserved could be
    # taken between its bind and our exec, so the child is the one that picks.
    server = build_server(generate, streaming)
    port = server.server_address[1]
    tmp = args.status + ".tmp"
    with open(tmp, "w") as handle:
        json.dump({"port": port, "pid": os.getpid(), "model": args.model}, handle)
    os.replace(tmp, args.status)

    threading.Thread(target=_bring_up, args=(args.model, download, load),
                     name="load", daemon=True).start()
    server.serve_forever()
