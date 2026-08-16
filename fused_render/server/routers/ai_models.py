"""GET /api/ai-models (+ /status, /revisions) and POST /api/ai-models/delete
— what the Hugging Face cache holds on this machine, and the deletions that free
it, for the sidebar's "AI Models" page.

The cache is a *shared* directory: anything that speaks `huggingface_hub`
(transformers, sentence-transformers, diffusers, a template a user pasted in,
the `hf` CLI) downloads into the same tree, and nothing ever tells the user
what accumulated there or how much disk it is now worth. This module reads that
tree, and — only on an explicit request naming what to remove — deletes from
it.

This module still never FETCHES anything: the download the page now offers
(D258, superseding HS-1) belongs to `ai_runtime.py`, because a fetch is the
runner's — a GGUF image model and an MLX text model do not download the same
set of files. What is here stays the reader and the deleter, so the one module
that walks the cache is not also the one that grows it.

The layout it reads is `huggingface_hub`'s own (CACHE_STRUCTURE in their
docs)::

    <hub cache>/
      models--openai--whisper-small/
        refs/main                 -> a commit sha
        blobs/<sha>               the real bytes
        snapshots/<commit>/…      symlinks back into blobs/
      datasets--squad/
      spaces--user--demo/
      .locks/ version.txt         bookkeeping, skipped

Two consequences drive `_scan_repo`:

* **Size is measured with `lstat`, symlinks skipped.** A snapshot entry points
  at a blob in the *same* repo, so following it would count every file twice
  per revision — a two-revision repo would report triple its real footprint.
  Hardlinks (the same blob shared by two entries, and what Windows falls back
  to when it cannot symlink) are de-duplicated by `(st_dev, st_ino)` for the
  same reason. What is left is bytes actually on disk, which is the number the
  page exists to show.
* **The newest mtime includes the symlinks**, unlike the size — and excludes
  directories. A blob is written once and never touched again, but
  materialising a revision creates its snapshot links, so their mtimes are what
  "last pulled a revision of this repo" actually looks like on disk. Directory
  mtimes are left out because they also move on *deletion*, which would report
  a repo someone just emptied as freshly used.

`atime` rides along beside it for a different question — "when was this last
*read*", which is what pruning by age needs (a model pulled a year ago and
loaded this morning is in use; mtime cannot tell those apart). Only real files
carry it: reading a model through a snapshot symlink updates the BLOB's atime,
not the link's.

Repo ids are decoded the way `huggingface_hub` encodes them — kind prefix,
then the id with `/` written as `--` (`models--openai--whisper-small` ->
`openai/whisper-small`). A directory whose name carries no known kind prefix
is not a repo folder and is skipped, which is also what keeps `.locks/`,
`version.txt` and half-written `tmp*` dirs out of the list.

**Deletion** (`POST /api/ai-models/delete`, D250) carries the D3 `X-Fused`
guard like every other mutating POST, and is deliberately narrow:

* Targets are named by cache **folder name**, never by a path from the client:
  the name is checked to be a single path segment carrying a known kind prefix,
  and joined onto the resolved cache dir here. A path from a request body would
  make this endpoint an arbitrary-rmtree.
* A repo folder that is a **symlink** is refused rather than followed — those
  live on another disk (how people move a 40GB model off the boot volume), and
  deleting through the link would reach outside the directory this endpoint is
  scoped to.
* Deleting a **revision** removes only the blobs that revision alone
  references. A blob shared with another revision survives; refs pointing at
  the deleted commit go with it, since a ref to a revision that no longer
  exists is dangling. If it was the last revision, the repo folder goes too —
  a shell of refs and orphaned blobs is not something to leave behind.
* Every target is reported individually. One stale row must not lose the other
  nine deletions of a prune.
"""
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass

from fastapi import APIRouter, Body, Header

from fused_render._view_url_codec import canonical_fs_path
from fused_render.ai import registry as _ai_registry
from fused_render.ai.runners import formats
from fused_render.server.common import _error, _require_fused

router = APIRouter()

# Directory-name prefix -> the kind reported to the UI. This is also the
# allowlist: a hub-cache entry that starts with none of these is not a repo,
# and is not something this module will delete.
_KIND_PREFIXES = {"models--": "model", "datasets--": "dataset", "spaces--": "space"}


def hf_home() -> str:
    """The Hugging Face home dir — `HF_HOME`, else `$XDG_CACHE_HOME/huggingface`,
    else `~/.cache/huggingface` (huggingface_hub's own resolution order)."""
    env = os.environ.get("HF_HOME")
    if env:
        return os.path.expanduser(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = os.path.expanduser(xdg) if xdg else os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "huggingface")


def hub_cache_dir() -> str:
    """Where repo folders live: `HF_HUB_CACHE`, else the deprecated-but-still-honored
    `HUGGINGFACE_HUB_CACHE`, else `<hf_home>/hub`.

    Resolved per call rather than at import: the answer is whatever the process
    environment says right now, and a module constant would freeze one machine's
    answer into the module (and force every test to patch a private).
    """
    for var in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        env = os.environ.get(var)
        if env:
            return os.path.expanduser(env)
    return os.path.join(hf_home(), "hub")


class _TargetError(Exception):
    """A delete target we refuse, or cannot find.

    Raised per target and reported per target: a prune naming ten repos must
    not lose nine deletions because the tenth row was stale.
    """


@dataclass
class _RepoScan:
    """One repo folder's on-disk footprint (see the module docstring for why
    size, mtime and atime each treat symlinks differently)."""

    size: int
    files: int
    mtime: float
    atime: float
    oldest: float


def _scan_repo(root: str) -> _RepoScan:
    size = 0
    files = 0
    newest = 0.0
    used = 0.0
    oldest = 0.0
    # Only consulted for multiply-linked files — the common case (one link) never
    # touches the set, so a 30k-blob cache doesn't pay for a 30k-entry dict.
    seen: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            # A repo folder being written by a live download, or one we can't
            # read: report what we could see rather than failing the page.
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                # Directory mtimes are excluded from `newest` deliberately: a
                # dir's mtime moves whenever an entry is added OR removed, so an
                # emptied repo would still report "just now".
                stack.append(entry.path)
                continue
            if st.st_mtime > newest:
                newest = st.st_mtime
            if stat.S_ISLNK(st.st_mode):
                continue  # points back into this repo's blobs/ — already counted
            # Only real files carry a meaningful atime: loading a model through
            # a snapshot symlink touches the blob, not the link.
            if st.st_atime > used:
                used = st.st_atime
            # Oldest real file ≈ when this repo first landed here. The Hub's
            # release date is NOT on disk (see _repo's "added"), so this is the
            # only date about a model this machine actually knows.
            if oldest == 0.0 or st.st_mtime < oldest:
                oldest = st.st_mtime
            if st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            size += st.st_size
            files += 1
    return _RepoScan(size=size, files=files, mtime=newest, atime=used, oldest=oldest)


# -- reading files without counting as a read ----------------------------------


def _read_preserving_atime(path: str, limit: int) -> bytes | None:
    """Up to `limit` bytes of `path`, with the file's atime put back afterwards.

    EVERY read in this module goes through here, and that is the point. `lastUsed`
    — which the prune selection is built on — is "when was something in here last
    read", and a model card, a config, or a safetensors header is reached through
    the snapshot symlink, so reading it touches the BLOB's atime. Without the
    restore, opening the page would mark every repo it inspected as used today and
    quietly exclude it from the next prune: a measuring instrument changing what
    it measures.
    """
    try:
        before = os.stat(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except OSError:
        return None
    try:
        os.utime(path, (before.st_atime, before.st_mtime))
    except OSError:
        pass  # read-only mount, or a file that just went — the read still stands
    return data


def _read_json(path: str, limit: int = 4 * 1024 * 1024) -> dict | None:
    raw = _read_preserving_atime(path, limit)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


# -- what a model is FOR -------------------------------------------------------
# Nothing in the cache states a model's purpose outright, so it is read from the
# evidence the download happened to bring, best first:
#
#   1. README.md front matter — `pipeline_tag` IS the Hub's own answer, so when
#      the model card came down with the weights there is nothing to infer.
#   2. model_index.json — a diffusers pipeline; its `_class_name` names the job.
#   3. config_sentence_transformers.json / modules.json — an embedding model.
#   4. config.json `architectures` — the transformers head, which encodes the
#      task in its suffix (…ForCausalLM, …ForImageClassification).
#   5. a *.gguf file — a llama.cpp text model.
#
# Every answer carries WHERE IT CAME FROM, because 1 is a fact and 4 is a
# reading of one, and a UI that showed them identically would be overclaiming.

# Hub pipeline tags are already readable once the hyphens are spaces, so there
# is no mapping table to go stale — only these three, whose Hub spelling is
# jargon for what people actually call them.
_FRIENDLIER_TAGS = {
    "feature-extraction": "embeddings",
    "sentence-similarity": "sentence embeddings",
    "text2text-generation": "text-to-text generation",
    # Hyphens-to-spaces turns this one into the unparseable "image text to
    # text". It means a vision-language model: an image AND a prompt in, text
    # out, so the "+" is doing the work the hyphens could not.
    "image-text-to-text": "image + text to text",
    "any-to-any": "any input to any output",
    # These two exist to make the CARD path and the ARCHITECTURE path agree on
    # one spelling. Left alone, a whisper model read from its card said
    # "automatic speech recognition" while the same model read from its config
    # said "speech recognition" — one concept, two labels, and the glossary
    # (keyed by label) only had a sentence for one of them.
    "automatic-speech-recognition": "speech recognition",
    "zero-shot-image-classification": "zero-shot image classification",
    "zero-shot-classification": "zero-shot text classification",
    # Same reason as the two above: a diffusers video/audio pipeline read from
    # its `_class_name` already says "video generation" / "audio generation",
    # so the Hub tags for the same thing are folded onto those labels rather
    # than sprouting a second spelling with its own (missing) glossary entry.
    "text-to-video": "video generation",
    "image-to-video": "video generation",
    "text-to-audio": "audio generation",
}

# transformers architecture suffix -> task. Ordered: the first match wins, so
# the more specific suffixes come before the ones they contain.
_ARCH_TASKS = (
    ("ForZeroShotImageClassification", "zero-shot image classification"),
    ("ForImageClassification", "image classification"),
    ("ForImageSegmentation", "image segmentation"),
    ("ForObjectDetection", "object detection"),
    ("ForSequenceClassification", "text classification"),
    ("ForTokenClassification", "token classification"),
    ("ForQuestionAnswering", "question answering"),
    ("ForSpeechSeq2Seq", "speech recognition"),
    ("ForConditionalGeneration", "text-to-text generation"),
    ("ForMaskedLM", "fill mask"),
    ("ForCausalLM", "text generation"),
    ("LMHeadModel", "text generation"),
    ("ForCTC", "speech recognition"),
)

# …ForConditionalGeneration is the same head for "translate this" and "transcribe
# this", so the model type is what separates them.
_AUDIO_MODEL_TYPES = {"whisper", "speech_to_text", "speecht5", "seamless_m4t"}


# Storage width of each safetensors dtype, for the quantized case below. Only
# the INTEGER types matter: a float tensor stores one value per element, while
# an integer tensor in a quantized checkpoint stores several packed into each
# word.
_DTYPE_BITS = {
    "U8": 8, "I8": 8, "F8_E4M3": 8, "F8_E5M2": 8,
    "U16": 16, "I16": 16, "F16": 16, "BF16": 16,
    "U32": 32, "I32": 32, "F32": 32,
    "U64": 64, "I64": 64, "F64": 64,
}
_PACKED_DTYPES = {"U8", "I8", "U16", "I16", "U32", "I32", "U64", "I64"}


def _quantization(config: dict) -> int | None:
    """The declared weight width in bits, or None when the checkpoint is not
    quantized.

    Read from what the checkpoint SAYS about itself — MLX writes
    `quantization: {group_size, bits}`, transformers writes
    `quantization_config: {bits | load_in_4bit | load_in_8bit, …}` — never
    guessed from a filename. `mlx-community/…-4bit` is a naming convention, and
    a number this page prints must not rest on one.
    """
    for key in ("quantization", "quantization_config"):
        block = config.get(key)
        if not isinstance(block, dict):
            continue
        bits = block.get("bits") or block.get("w_bit") or block.get("weight_bits")
        if isinstance(bits, int) and 0 < bits < 32:
            return bits
        if block.get("load_in_4bit"):
            return 4
        if block.get("load_in_8bit"):
            return 8
    return None


# What each task actually MEANS, in terms of what goes in and what comes out.
# The labels above are the Hub's vocabulary (or our reading of an architecture),
# and "image + text to text" or "fill mask" tell you nothing if you have not met
# them before — so the card explains them on hover rather than leaving a phrase
# to be guessed at.
#
# Keyed by the LABEL, not the raw tag, so one table serves both the model-card
# path and the architecture path. Deliberately incomplete: the Hub adds tags,
# and a tag we have no sentence for still shows its label and its source, which
# is what an open vocabulary degrades to gracefully.
_TASK_HELP = {
    "text generation": "Continues or answers a prompt in text — chat models, code models, completion.",
    "text-to-text generation": "Rewrites text into other text — translation, summarising, reformatting.",
    "fill mask": "Fills in blanked-out words in a sentence. Mostly a building block for other models.",
    "text classification": "Sorts a piece of text into categories — sentiment, topic, spam.",
    "token classification": "Labels each word in a sentence — named entities, parts of speech.",
    "question answering": "Finds the span of a supplied document that answers a question.",
    "summarization": "Shortens a long text into its main points.",
    "translation": "Translates text from one language into another.",
    "embeddings": "Turns text into vectors, so things can be compared or searched by meaning.",
    "sentence embeddings": "Turns sentences into vectors, so similar sentences land near each other — search, clustering, RAG.",
    "image + text to text": "Takes an image AND a prompt, answers in text — describing a picture, reading a chart, visual chat.",
    "image to text": "Describes an image in words — captioning, OCR.",
    "text to image": "Generates a picture from a written description.",
    "image generation": "Generates a picture, usually from a written description.",
    "video generation": "Generates video frames, usually from a description or a still image.",
    "audio generation": "Generates sound — speech, music, effects.",
    "text to speech": "Reads text aloud as audio.",
    "speech recognition": "Transcribes speech in audio into text.",
    "image classification": "Says what an image is a picture of, from a fixed set of labels.",
    "zero-shot image classification": "Says what an image shows, against labels you supply at the time rather than a fixed set.",
    "zero-shot text classification": "Sorts text into categories you supply at the time rather than a fixed set.",
    "image segmentation": "Marks which pixels belong to which object.",
    "object detection": "Finds objects in an image and boxes them.",
    "depth estimation": "Estimates how far away each part of an image is.",
    "image to image": "Turns one picture into another — upscaling, restyling, inpainting.",
    "audio classification": "Sorts a sound into categories — which language, which speaker, what noise.",
    "any input to any output": "Handles several kinds of input and output — text, images, audio — in one model.",
}


@dataclass
class _RepoMeta:
    """What a repo is for and how big the model is — read from the default
    revision's snapshot, or empty when the download brought no evidence."""

    task: str | None = None
    task_source: str | None = None
    # One sentence on what the task means, when we have one for it.
    task_help: str | None = None
    params: int | None = None
    # True when `params` was recovered from packed weights (see
    # _safetensors_params) rather than read off unpacked shapes — the UI marks
    # it, because the arithmetic rests on the checkpoint's declared bit width.
    params_estimated: bool = False
    # "4-bit", "8-bit" — what the checkpoint declares about its weights.
    quantization: str | None = None
    library: str | None = None
    # Runner codes whose `load()` would accept this snapshot's FORMAT, from
    # `ai/runners/formats.py` — the same evidence each worker checks before it
    # imports anything. Empty is a real answer and the most useful one: no
    # backend that ships reads this repo.
    loaders: tuple[str, ...] = ()


def _front_matter(snapshot_dir: str) -> dict[str, str]:
    """Top-level SCALARS of a model card's YAML front matter.

    Deliberately not a YAML parser and deliberately not a YAML dependency (the
    package does not have one): this reads `key: value` lines between the
    opening and closing `---`, which is the shape `pipeline_tag` and
    `library_name` are always written in. Nested blocks (`model-index`,
    `widget`, tag lists) are skipped rather than half-understood — a key this
    misses degrades to the config.json reading below, which is the whole point
    of having a chain of evidence.
    """
    raw = _read_preserving_atime(os.path.join(snapshot_dir, "README.md"), 64 * 1024)
    if raw is None:
        return {}
    text = raw.decode("utf-8", errors="replace")
    if not text.startswith("---"):
        return {}
    lines = text.split("\n")[1:]
    out: dict[str, str] = {}
    for line in lines:
        if line.strip() in ("---", "..."):
            break
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue  # nested value, list item, or comment — not a top-level scalar
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip().strip("'\"")
        if value:
            out[key.strip()] = value
    return out


def _pipeline_task(tag: str) -> str:
    return _FRIENDLIER_TAGS.get(tag, tag.replace("-", " "))


def _diffusers_task(class_name: str) -> str:
    lowered = class_name.lower()
    if "video" in lowered:
        return "video generation"
    if "audio" in lowered or "music" in lowered:
        return "audio generation"
    return "image generation"


def _architecture_task(config: dict) -> str | None:
    architectures = config.get("architectures")
    name = architectures[0] if isinstance(architectures, list) and architectures else None
    if not isinstance(name, str):
        return None
    for suffix, task in _ARCH_TASKS:
        if name.endswith(suffix):
            if suffix == "ForConditionalGeneration" and config.get("model_type") in _AUDIO_MODEL_TYPES:
                return "speech recognition"
            return task
    return None


# A precision variant sits BESIDE the file it is a variant of in diffusers
# repos (`diffusion_pytorch_model.fp16.safetensors` next to
# `diffusion_pytorch_model.safetensors`). Both hold the same tensors, so a repo
# that pulled both must not report twice the parameters it has.
_VARIANT_SUFFIX = re.compile(r"\.(fp16|bf16|fp8|f16|f8|8bit|4bit)\.safetensors$", re.IGNORECASE)


def _weight_files(snapshot_dir: str) -> list[str]:
    """Every safetensors file of a revision, in tree order.

    A WALK, not a listing of the top level: a diffusers pipeline keeps its
    weights per component (`transformer/`, `unet/`, `vae/`, `text_encoder/`),
    which is exactly the layout behind the pipelines whose task we detect, so a
    top-level-only look would answer "no parameter count" for the models people
    most want the number for.

    Two things are dropped: a precision variant whose plain counterpart is also
    present (same tensors, one count), and a second name for a blob already
    counted.

    That second rule keys on `(st_dev, st_ino)` — the file's identity — rather
    than on its resolved path, which is the same rule `_scan_repo` uses for
    bytes and for the same reason. A resolved path collapses a symlink onto its
    target but leaves two HARDLINKS looking like two files, and a cache written
    where symlinks were unavailable is exactly where the aliases are hardlinks.
    Following the symlink is also what stat does here, so one key covers both.
    """
    found: list[str] = []
    counted: set[tuple[int, int]] = set()
    for dirpath, _dirnames, filenames in os.walk(snapshot_dir):
        here = set(filenames)
        for name in sorted(filenames):
            if not name.endswith(".safetensors"):
                continue
            if _VARIANT_SUFFIX.search(name) and _VARIANT_SUFFIX.sub(".safetensors", name) in here:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)  # follows the link: the blob's own identity
            except OSError:
                continue  # a dangling link has no header to read anyway
            key = (st.st_dev, st.st_ino)
            if key in counted:
                continue
            counted.add(key)
            found.append(path)
    return found


def _has_torch_weights(snapshot_dir: str) -> bool:
    """Is there anything in this revision torch could open?

    A WALK, like `_weight_files`, and for the same reason: a diffusers pipeline
    keeps its weights per component. The suffixes are `formats.TORCH_WEIGHTS`
    rather than a list spelled here — this is the page's copy of the question
    `transformers_text/worker.py` asks before it refuses a repo.
    """
    for _dirpath, _dirnames, filenames in os.walk(snapshot_dir):
        if any(name.endswith(formats.TORCH_WEIGHTS) for name in filenames):
            return True
    return False


def _safetensors_params(path: str, quantized_bits: int | None = None) -> tuple[int, bool]:
    """Parameters in one safetensors file, and whether any of them were unpacked.

    The format opens with a little-endian u64 header length and that many bytes
    of JSON describing every tensor, so the count costs one small read rather
    than the multi-GB file. (`.bin` pickles and `.gguf` carry no equivalent
    cheap header, so a repo holding only those reports no count instead of a
    guess.)

    **A quantized checkpoint does not store one parameter per element.** A
    4-bit MLX or GPTQ checkpoint bit-packs eight weights into each `U32`, so
    summing shapes counts storage slots — a 12B model reports ~2B, which is not
    a small error but a different number entirely. When the config declares a
    bit width, each element of an INTEGER tensor is therefore expanded by how
    many weights that word holds (`storage bits / declared bits`), and the
    result is flagged as recovered rather than measured. Float tensors — scales,
    biases, anything left unquantized — are counted as they are.
    """
    head = _read_preserving_atime(path, 8)
    if head is None or len(head) < 8:
        return 0, False
    length = int.from_bytes(head, "little")
    # A sane header is kilobytes to a few MB; anything else is not safetensors.
    if not 0 < length <= 64 * 1024 * 1024:
        return 0, False
    raw = _read_preserving_atime(path, 8 + length)
    if raw is None or len(raw) < 8 + length:
        return 0, False
    try:
        header = json.loads(raw[8:])
    except ValueError:
        return 0, False
    if not isinstance(header, dict):
        return 0, False
    total = 0
    unpacked = False
    for name, info in header.items():
        if name == "__metadata__" or not isinstance(info, dict):
            continue
        shape = info.get("shape")
        if not isinstance(shape, list) or not shape:
            continue
        count = 1
        for dim in shape:
            if not isinstance(dim, int) or dim < 0:
                count = 0
                break
            count *= dim
        dtype = info.get("dtype")
        if quantized_bits and isinstance(dtype, str) and dtype.upper() in _PACKED_DTYPES:
            per_word = _DTYPE_BITS.get(dtype.upper(), 0) // quantized_bits
            if per_word > 1:
                count *= per_word
                unpacked = True
        total += count
    return total, unpacked


# Metadata is read once per snapshot directory and remembered: a snapshot's
# contents are immutable once written (every file in it is a link to a
# content-addressed blob), so its own mtime is a sufficient key, and a Refresh
# on a 40-repo cache re-reads nothing.
_META_CACHE: dict[str, tuple[float, _RepoMeta]] = {}


def _default_snapshot(repo_dir: str) -> str | None:
    """The revision to describe the repo by: whatever `refs/main` points at,
    else the most recently written snapshot."""
    snapshots_dir = os.path.join(repo_dir, "snapshots")
    entries = _snapshot_dirs(snapshots_dir)
    if not entries:
        return None
    by_name = {e.name: e.path for e in entries}
    main = _refs_by_commit(repo_dir).get("main")
    if main and main in by_name:
        return by_name[main]
    # `entries` are already directories (_snapshot_dirs filtered them), so the
    # only question left is which is newest — and a snapshot that vanished
    # between the two is simply not it.
    return max(entries, key=_entry_mtime).path


def _repo_meta(repo_dir: str) -> _RepoMeta:
    snapshot = _default_snapshot(repo_dir)
    if snapshot is None:
        return _RepoMeta()
    try:
        stamp = os.stat(snapshot).st_mtime
    except OSError:
        return _RepoMeta()
    cached = _META_CACHE.get(snapshot)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    meta = _RepoMeta()
    try:
        names = set(os.listdir(snapshot))
    except OSError:
        names = set()

    front = _front_matter(snapshot)
    library = front.get("library_name")
    tag = front.get("pipeline_tag")
    if tag:
        meta.task, meta.task_source = _pipeline_task(tag), "the model card's pipeline_tag"

    # …and `model_index.json` OUTRANKS it, which is the one place the card is
    # not the best answer. A card's `pipeline_tag` is one headline for a model
    # FAMILY — `black-forest-labs/FLUX.2-klein-4B` leads with `image-to-image`,
    # a label no runner here serves — while this file names the pipeline that
    # is actually in this snapshot, written by the library that will load it.
    # Ranking the card first took the Load button off the app's own recommended
    # image model, on the tab beside the one recommending it.
    if formats.DIFFUSERS_INDEX in names:
        index = _read_json(os.path.join(snapshot, formats.DIFFUSERS_INDEX)) or {}
        class_name = index.get("_class_name")
        if isinstance(class_name, str) and class_name:
            meta.task = _diffusers_task(class_name)
            meta.task_source = f"the diffusers pipeline {class_name}"
            library = library or "diffusers"

    if meta.task is None and ("config_sentence_transformers.json" in names or "modules.json" in names):
        meta.task, meta.task_source = "embeddings", "its sentence-transformers config"
        library = library or "sentence-transformers"

    # Read once, whatever the task turned out to be: the architecture is only
    # one of the things config.json answers, and the weight width is needed even
    # for a repo whose card already named its task.
    config = _read_json(os.path.join(snapshot, "config.json")) or {} if "config.json" in names else {}
    if meta.task is None and config:
        task = _architecture_task(config)
        if task:
            meta.task, meta.task_source = task, "the architecture in config.json"

    # A GGUF file names the LIBRARY and nothing else. It used to name the task
    # too — "text generation", unconditionally — which put a Load button on
    # `unsloth/FLUX.2-klein-4B-GGUF`, an image model, and is the precise failure
    # `capability_for_task` warns about. A GGUF is a container, not a modality,
    # and no runner that ships loads a GGUF-only repo anyway (transformers
    # refuses one by name), so the guess could never have been right in a way
    # that mattered: it only ever produced a button that fails.
    if any(n.lower().endswith(".gguf") for n in names):
        library = library or "gguf"

    # Last, and only where nothing above answered: the WEIGHT LAYOUT. A
    # CTranslate2 conversion carries no pipeline_tag and no `architectures`, so
    # this app's own recommended speech model showed a card with no task line
    # and no Load button — while `faster_whisper/worker.py` recognises it from
    # one filename. Same for an MLX conversion, whose `weights.npz` is a
    # Whisper checkpoint and can be nothing else.
    if meta.task is None:
        if formats.is_ct2_whisper(names, config):
            meta.task, meta.task_source = (
                "speech recognition", "its CTranslate2 Whisper layout")
        elif formats.has_mlx_whisper_weights(names):
            meta.task, meta.task_source = (
                "speech recognition", "its MLX Whisper weights")

    if meta.task:
        meta.task_help = _TASK_HELP.get(meta.task)

    quantized_bits = _quantization(config)
    if quantized_bits:
        meta.quantization = f"{quantized_bits}-bit"

    # Summed across every component of the revision (see _weight_files): for a
    # pipeline that is transformer + text encoders + VAE, i.e. the parameters
    # this repo actually holds, rather than a curated idea of which component
    # counts as "the model".
    total = 0
    estimated = False
    for path in _weight_files(snapshot):
        count, unpacked = _safetensors_params(path, quantized_bits)
        total += count
        estimated = estimated or unpacked
    meta.params = total or None
    meta.params_estimated = estimated
    meta.library = library

    # Which backend's `load()` would accept this, by format alone. Cached with
    # everything else because it reads the same listing and the same config —
    # and asked of `formats`, never re-derived here: a second copy of "what a
    # CTranslate2 repo looks like" is how a card comes to promise a load the
    # runner refuses.
    try:
        dirnames = {e.name for e in os.scandir(snapshot) if _entry_is_dir(e)}
    except OSError:
        dirnames = set()
    meta.loaders = formats.loaders(
        repo_id=_repo_id_of(os.path.basename(os.path.normpath(repo_dir))),
        names=names,
        dirnames=dirnames,
        config=config,
        torch_weights=_has_torch_weights(snapshot),
    )

    _META_CACHE[snapshot] = (stamp, meta)
    return meta


def _entry_is_dir(entry: os.DirEntry, *, follow: bool = True) -> bool:
    """`entry.is_dir()` for a tree that other processes are writing.

    A cache is a shared directory: a download finalising, or a delete from
    another window, can take an entry away between the `scandir` that listed it
    and the `stat` that asks about it. `_scan_repo` has always treated that race
    as "report what was there"; these two call sites used to raise instead and
    fail the whole listing with a 500, which is a worse answer than a row fewer.
    """
    try:
        return entry.is_dir(follow_symlinks=follow)
    except OSError:
        return False


def _entry_mtime(entry: os.DirEntry) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0  # gone mid-scan, so it cannot be the newest


def _snapshot_dirs(snapshots_dir: str) -> list[os.DirEntry]:
    """The revision directories under `snapshots/`. Symlinked entries are not
    followed — every deletion path below reasons about what is inside this
    repo."""
    try:
        entries = list(os.scandir(snapshots_dir))
    except OSError:
        return []
    # Filtered OUTSIDE the scandir's try: an entry that disappears here must
    # cost its own row, not the whole list of revisions.
    return [e for e in entries if _entry_is_dir(e, follow=False)]


def _snapshot_blobs(snapshot_dir: str, blobs_dir: str) -> set[str]:
    """The blobs a revision references — resolved link targets that really land
    in this repo's `blobs/`.

    Anything resolving elsewhere is deliberately NOT returned: on Windows (and
    any filesystem without symlinks) a snapshot entry is the file itself, whose
    bytes go away with the snapshot directory; and a target outside `blobs/` is
    not a path this module will ever unlink.
    """
    base = os.path.realpath(blobs_dir)
    found: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(snapshot_dir):
        for name in filenames:
            target = os.path.realpath(os.path.join(dirpath, name))
            if os.path.dirname(target) == base:
                found.add(target)
    return found


def _blob_size(path: str) -> int:
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def _revisions(repo_dir: str) -> int:
    return len(_snapshot_dirs(os.path.join(repo_dir, "snapshots")))


def _ref_names(repo_dir: str) -> list[str]:
    """Branch/tag names under refs/ (`main`, a release tag, …)."""
    refs_dir = os.path.join(repo_dir, "refs")
    names: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(refs_dir):
        rel = os.path.relpath(dirpath, refs_dir)
        for name in filenames:
            names.append(name if rel == "." else f"{rel}/{name}".replace(os.sep, "/"))
    names.sort()
    return names


def _refs_by_commit(repo_dir: str) -> dict[str, str]:
    """ref name -> the commit it points at. The commit shas are read here (and
    only here): the listing names revisions, the revision view resolves them.

    Through _read_preserving_atime like every other read in this module — see
    its docstring for why inspecting the cache must not mark it as used.
    """
    refs_dir = os.path.join(repo_dir, "refs")
    out: dict[str, str] = {}
    for name in _ref_names(repo_dir):
        raw = _read_preserving_atime(os.path.join(refs_dir, name), 4096)
        if raw is not None:
            out[name] = raw.decode("utf-8", errors="ignore").strip()
    return out


def _engine(meta: _RepoMeta, capability: str | None) -> tuple[dict | None, str | None]:
    """Which backend would load this repo, and the capability it would load it
    AS — `(None, capability)` when nothing here reads the format.

    Two questions, deliberately answered together, because either alone lies.
    The FORMAT says which runners could open it (`meta.loaders`); the REGISTRY
    says which of those runs on this machine and which is serving right now.
    A card that showed only the first would offer MLX Whisper on a PC; a card
    that showed only the second would offer the CTranslate2 runner an MLX repo.

    **"Nothing reads this" and "what reads it does not run here" are different
    sentences**, so an unavailable runner is still reported — with the
    registry's own reason. A Windows user looking at `mlx-community/whisper-…`
    has not made a mistake they can fix by deleting it; they have a Mac's model.

    The capability comes back because for some repos the FORMAT is the only
    thing that knows it: a CT2 conversion has no pipeline_tag and an MLX
    conversion has no config, and both are speech models beyond doubt. Only the
    decisive formats may answer — a directory of safetensors says nothing about
    the modality (`formats.DECISIVE`).
    """
    if not meta.loaders:
        return None, capability
    runners = [r for r in _ai_registry.all_runners() if r.code in meta.loaders]
    if capability is None:
        decisive = [r for r in runners if r.code in formats.DECISIVE]
        capability = decisive[0].capability if decisive else None
    if capability is None:
        return None, None
    candidates = [r for r in runners if r.capability == capability]
    if not candidates:
        # The trap this whole field exists for: the task is one this app serves
        # and the FORMAT is one no runner of that capability reads. That is
        # `openai/whisper-large-v3` — a speech model, and unloadable by both
        # speech runners that ship.
        return None, capability
    # The one that would actually serve, when it is among them — so the tag
    # says what a Load would do rather than what could in principle.
    serving = _ai_registry.for_capability(capability)
    runner = next(
        (r for r in candidates if serving is not None and r.code == serving.code),
        None,
    ) or next((r for r in candidates if r.available().ok), None) or candidates[0]
    status = runner.available()
    return {
        "code": runner.code,
        "label": runner.label,
        "available": status.ok,
        "reason": status.reason or None,
    }, capability


def _repo(cache_dir: str, dirname: str, kind: str) -> dict:
    repo_dir = os.path.join(cache_dir, dirname)
    scan = _scan_repo(repo_dir)
    meta = _repo_meta(repo_dir)
    capability = (
        _ai_registry.capability_for_task(meta.task) if kind == "model" else None
    )
    engine, capability = (
        _engine(meta, capability) if kind == "model" else (None, None)
    )
    return {
        "id": _repo_id_of(dirname),
        # The cache folder name, which is what a delete request names (never a
        # path — see the module docstring).
        "dir": dirname,
        "kind": kind,
        # Canonicalized like every other fs path the frontend gets, so it can
        # go straight to navigate(path, {isDir: true}).
        "path": canonical_fs_path(repo_dir),
        "size": scan.size,
        "files": scan.files,
        "mtime": scan.mtime or None,
        # Newest atime — "last read", which is what pruning by age asks about.
        "lastUsed": scan.atime or None,
        # When the repo first landed here. NOT the model's release date: that
        # is Hub metadata and this page never goes to the network, so the
        # honest local answer is "you downloaded this then".
        "added": scan.oldest or None,
        # What the model is FOR, and where that was read from — a pipeline_tag
        # is the Hub's own answer, an architecture is our reading of one, and
        # the UI says which (see _repo_meta).
        "task": meta.task,
        "taskSource": meta.task_source,
        # One sentence on what that task means — the labels are the Hub's
        # vocabulary, which is jargon until someone explains it.
        "taskHelp": meta.task_help,
        "library": meta.library,
        # Parameter count, exact, from the safetensors headers. None when the
        # weights are in a format with no cheap header to read.
        "params": meta.params,
        # True when the count was recovered from packed weights rather than read
        # off unpacked shapes — the card marks it with a "≈".
        "paramsEstimated": meta.params_estimated,
        # What the checkpoint declares about its weight width ("4-bit").
        "quantization": meta.quantization,
        # Which capability could LOAD this, or None (SPEC §40). Answered here
        # because the task vocabulary and the capability vocabulary both live on
        # this side: a page deciding for itself would need a second copy of the
        # mapping, and would cheerfully try to load a diffusion model as a chat
        # model. None for a dataset, a Space, an embedding model, or anything no
        # runner serves yet.
        "capability": capability,
        # WHICH BACKEND would load it, or null when nothing here reads the
        # format — the fact a card most needs and the one it did not have. See
        # `_engine`: null and an unavailable runner are different answers.
        "engine": engine,
        "revisions": _revisions(repo_dir),
        "refs": _ref_names(repo_dir),
    }


def _listing() -> dict:
    cache_dir = hub_cache_dir()
    repos: list[dict] = []
    try:
        entries = list(os.scandir(cache_dir))
    except OSError:
        entries = []
    for entry in entries:
        # Symlinks ARE followed here, unlike inside a repo: a repo folder
        # symlinked in from another disk (how people move a 40GB model off the
        # boot volume) is a real cached repo, and its files still measure
        # correctly since the walk lstats what it finds on the other side. A
        # broken link answers False and drops out, as does one that vanished
        # between the scandir and here (_entry_is_dir).
        if not _entry_is_dir(entry):
            continue
        kind = next(
            (k for prefix, k in _KIND_PREFIXES.items() if entry.name.startswith(prefix)), None
        )
        if kind is None:
            continue  # .locks/, tmp dirs, anything that isn't a repo folder
        repos.append(_repo(cache_dir, entry.name, kind))
    # Biggest first: the page's job is "what is this costing me", and a name
    # sort buries the 8GB checkpoint among forty 2MB tokenizer repos.
    repos.sort(key=lambda r: (-r["size"], r["id"]))
    return {
        "cacheDir": canonical_fs_path(cache_dir),
        "hfHome": canonical_fs_path(hf_home()),
        "exists": os.path.isdir(cache_dir),
        "totalSize": sum(r["size"] for r in repos),
        "repos": repos,
    }


# -- target resolution ---------------------------------------------------------
# Everything destructive goes through these two. A request names a cache FOLDER
# (and optionally a revision); the path is built here, from the cache dir this
# server resolved, and never taken from the client.


def _segment(name: object, what: str) -> str:
    if not isinstance(name, str) or not name:
        raise _TargetError(f"{what} is required")
    if name in (".", "..") or "/" in name or "\\" in name or name != os.path.basename(name):
        raise _TargetError(f"{name!r} is not a {what}")
    return name


def _repo_id_of(dirname: str) -> str:
    """`models--openai--whisper-small` -> `openai/whisper-small`.

    One derivation, because two things ask it now: the listing labels a card
    with it, and the delete endpoint asks the supervisor whether THAT id is
    loaded. A bare repo id (no org) has one segment and comes back unchanged.
    """
    return "/".join(dirname.split("--")[1:])


def _resolve_repo_dir(cache_dir: str, name: object) -> str:
    """The absolute path of a cache repo folder named by a request. Read paths
    accept a symlinked folder; `_require_deletable` is what refuses it."""
    repo = _segment(name, "cache folder name")
    if not any(repo.startswith(prefix) for prefix in _KIND_PREFIXES):
        raise _TargetError(f"{repo!r} is not a Hugging Face cache repo folder")
    path = os.path.join(cache_dir, repo)
    if not os.path.isdir(path):
        raise _TargetError(f"{repo} is not in this cache")
    return path


def _require_deletable(repo_dir: str) -> None:
    if os.path.islink(repo_dir):
        raise _TargetError(
            f"{os.path.basename(repo_dir)} is a symlink into another location — "
            "delete it where the files really live"
        )


def _require_not_in_use(dirname: str) -> None:
    """Refuse to delete files a worker is loading, holding, or fetching.

    This module owns the cache and the supervisor owns the processes, and until
    now neither asked the other anything. Deleting a repo mid-load removes the
    shards `from_pretrained` is still reading, and the error arrives minutes
    later looking like a corrupt model; deleting a RESIDENT model is quieter and
    worse, because the weights are already mapped — on POSIX the delete succeeds,
    the page says the model is gone, and it keeps answering until an unload
    makes the bytes disappear for real.

    Checked at the endpoint rather than trusted to a disabled button: the button
    is the courtesy, this is the guarantee (MD-11). Imported at call time — this
    module must stay usable on a machine with no runners at all.
    """
    from fused_render.ai import supervisor

    reason = supervisor.busy_reason(_repo_id_of(dirname))
    if reason is None:
        return
    remedy = ("Unload it first." if reason == "in memory"
              else "Wait for it to finish, or cancel it in the download manager.")
    raise _TargetError(f"{_repo_id_of(dirname)} is {reason}. {remedy}")


# -- deletion ------------------------------------------------------------------


def _delete_repo(repo_dir: str) -> int:
    """Remove a whole repo folder; returns the bytes it held."""
    freed = _scan_repo(repo_dir).size
    shutil.rmtree(repo_dir)
    # The lock folder mirrors the repo's name and is bookkeeping for a repo that
    # no longer exists — leaving it behind litters the cache with lock dirs for
    # repos nobody can see. ignore_errors: it may not exist, and a lock we
    # cannot remove is not a reason to report the deletion as failed.
    shutil.rmtree(
        os.path.join(os.path.dirname(repo_dir), ".locks", os.path.basename(repo_dir)),
        ignore_errors=True,
    )
    return freed


def _delete_revision(repo_dir: str, revision: object) -> int:
    """Remove one revision: its snapshot directory, the blobs only it
    references, and any ref pointing at it. Returns the bytes freed."""
    commit = _segment(revision, "revision")
    snapshots_dir = os.path.join(repo_dir, "snapshots")
    target = os.path.join(snapshots_dir, commit)
    if not os.path.isdir(target) or os.path.islink(target):
        raise _TargetError(f"revision {commit} is not in this cache")

    blobs_dir = os.path.join(repo_dir, "blobs")
    kept: set[str] = set()
    for entry in _snapshot_dirs(snapshots_dir):
        if entry.name != commit:
            kept |= _snapshot_blobs(entry.path, blobs_dir)

    freed = 0
    for blob in sorted(_snapshot_blobs(target, blobs_dir) - kept):
        size = _blob_size(blob)
        try:
            os.remove(blob)
        except OSError:
            continue  # already gone, or held; the snapshot still goes
        freed += size
    # Whatever the snapshot dir holds in its own right — on a filesystem without
    # symlinks that is the revision's actual bytes, everywhere else a few
    # hundred bytes of stray files.
    freed += _scan_repo(target).size
    shutil.rmtree(target)

    refs_dir = os.path.join(repo_dir, "refs")
    for ref, points_at in _refs_by_commit(repo_dir).items():
        if points_at != commit:
            continue
        # A ref to a revision that no longer exists is dangling — and would make
        # the next `from_pretrained` resolve to nothing.
        path = os.path.join(refs_dir, ref)
        size = _blob_size(path)
        try:
            os.remove(path)
        except OSError:
            continue
        freed += size

    if not _snapshot_dirs(snapshots_dir):
        # Nothing is left to point at. The remaining shell (refs, any blob no
        # revision referenced) is litter, so the repo goes — which is also what
        # huggingface_hub's own delete-cache does with a last revision.
        freed += _delete_repo(repo_dir)
    return freed


# -- endpoints -----------------------------------------------------------------


# GET /api/ai-models/status was here: one isdir(), answering "does this machine
# have a hub cache at all" for the sidebar entry's gate. The entry is
# unconditional now (HF-8, D265) and nothing else ever asked, so the probe went
# with its only caller — `GET /api/ai-models` reports the same fact as `exists`
# for the page's empty state, which is the one reader left.


@router.get("/api/ai-models")
def api_ai_models():
    """Every repo in the hub cache, biggest first.

    Sync `def` on purpose: this walks a tree that can hold tens of thousands of
    blobs, so FastAPI runs it in the threadpool instead of stalling the event
    loop for every other request the page fires.
    """
    return _listing()


@router.get("/api/ai-models/revisions")
def api_ai_models_revisions(repo: str):
    """One repo's revisions, each with the bytes deleting it would actually
    free.

    `size` is the revision's EXCLUSIVE bytes — blobs no other revision
    references — because that is what a delete recovers; `shared` is what it
    holds in common with its siblings, and stays behind. Two revisions of a
    7GB model that differ in a config file are 7GB shared and a few KB each,
    and a row claiming 7GB apiece would be a lie in the one column this page
    exists for.

    Computed on demand rather than in the listing: it resolves every symlink in
    every snapshot, which the biggest-first overview does not need.
    """
    cache_dir = hub_cache_dir()
    try:
        repo_dir = _resolve_repo_dir(cache_dir, repo)
    except _TargetError as e:
        return _error(str(e), status=404)

    blobs_dir = os.path.join(repo_dir, "blobs")
    snapshots_dir = os.path.join(repo_dir, "snapshots")
    per_revision = {e.name: _snapshot_blobs(e.path, blobs_dir) for e in _snapshot_dirs(snapshots_dir)}
    refs_by_commit: dict[str, list[str]] = {}
    for ref, commit in _refs_by_commit(repo_dir).items():
        refs_by_commit.setdefault(commit, []).append(ref)

    revisions = []
    for commit, blobs in per_revision.items():
        others: set[str] = set()
        for other, other_blobs in per_revision.items():
            if other != commit:
                others |= other_blobs
        own = _scan_repo(os.path.join(snapshots_dir, commit))
        revisions.append(
            {
                "commit": commit,
                "refs": sorted(refs_by_commit.get(commit, [])),
                # own.size covers a snapshot dir that holds real files rather
                # than links (Windows), and is 0 in the ordinary symlink case.
                "size": sum(_blob_size(b) for b in blobs - others) + own.size,
                "shared": sum(_blob_size(b) for b in blobs & others),
                "files": len(blobs) + own.files,
                "mtime": own.mtime or None,
            }
        )
    revisions.sort(key=lambda r: (-r["size"], r["commit"]))
    return {"repo": repo, "revisions": revisions}


@router.post("/api/ai-models/delete")
def api_ai_models_delete(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Delete named repos and/or revisions, then answer with the fresh listing.

    Body: `{"targets": [{"dir": "models--org--name", "revision": "<sha>"|null}]}`.
    A missing `revision` deletes the whole repo folder.

    The reply is the same shape `GET /api/ai-models` returns, plus `freed`
    and `failures`, so the page swaps in state it just re-read from disk rather
    than patching rows it hopes are still true. Guarded by `X-Fused` (D3) like
    every mutating POST: this one removes multi-GB directories, and a blind
    cross-origin POST must not reach it.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    targets = body.get("targets")
    if not isinstance(targets, list) or not targets:
        return _error("'targets' must be a non-empty list")

    cache_dir = hub_cache_dir()
    freed = 0
    failures = []
    for target in targets:
        if not isinstance(target, dict):
            failures.append({"dir": None, "revision": None, "error": "target must be an object"})
            continue
        name, revision = target.get("dir"), target.get("revision")
        try:
            repo_dir = _resolve_repo_dir(cache_dir, name)
            _require_deletable(repo_dir)
            # Both kinds: a revision of a loaded model is the revision it is
            # holding open, so "just one revision" is not the safer request it
            # looks like.
            _require_not_in_use(os.path.basename(repo_dir))
            # `revision is None` is the whole repo; anything else is a revision
            # and must survive _segment. Testing truthiness instead would turn a
            # malformed revision ("", 0) into "delete the entire repo" — the
            # widest possible reading of the narrowest possible request.
            freed += (
                _delete_repo(repo_dir) if revision is None else _delete_revision(repo_dir, revision)
            )
        except _TargetError as e:
            failures.append({"dir": name, "revision": revision, "error": str(e)})
        except OSError as e:
            # Permission, a file held open, a disk error: this target failed and
            # the rest of the batch still runs.
            failures.append({"dir": name, "revision": revision, "error": str(e)})
    return {**_listing(), "freed": freed, "failures": failures}
