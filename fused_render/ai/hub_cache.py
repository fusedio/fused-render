"""Reading — and deleting from — the Hugging Face hub cache on this machine.

This is the whole of what `GET /api/ai-models` and `POST /api/ai-models/delete`
do, minus the two route decorators: the walk that finds every cached repo, the
metadata reading that says what each one IS (task, params, quantization, which
engine could load it), the size arithmetic that makes those numbers mean
"bytes actually on disk", and the deletions that free them.

**It moved out of `server/routers/ai_models.py`, where it was ~1500 lines under
three endpoints.** Two other routers already imported it there —
`ai_runtime.py` for `cached_models`/`cached_capability` (the catalog is built
from what is on disk) and `hub_models.py` for `_scan_repo`/`_revisions`/
`hub_cache_dir` (a search result has to know whether you already have it) — so
the layering was upside down: the module every AI surface reads was reachable
only through one page's router. It belongs beside `catalog.py`, `registry.py`
and `supervisor.py`, which are the other three things that know what a local
model is.

The routes did not move and their URLs did not change.

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
import sys
import time
from dataclasses import dataclass
from typing import NamedTuple

from fused_render._view_url_codec import canonical_fs_path
from fused_render.ai import registry as _ai_registry
from fused_render.ai import tasks as _tasks
from fused_render.ai.runners import formats

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
            if stat.S_ISLNK(st.st_mode):
                # Points back into this repo's blobs/ — its target is already
                # counted, so a link contributes to `newest` and to nothing
                # else. (Deliberate, and unchanged: a link still dates the
                # repo, but only real files carry a meaningful atime — loading
                # a model through a snapshot symlink touches the blob, not the
                # link — and counting the link's size would double-count.)
                if st.st_mtime > newest:
                    newest = st.st_mtime
                continue
            if sys.platform == "win32":
                # entry.stat() above is DirEntry.stat(): on Windows it is built
                # from the cached FindFirstFile/FindNextFile data, which has no
                # file-index or link-count field at all, so st_ino/st_dev/
                # st_nlink from it are always 0 — silently disabling the dedup
                # below on exactly the platform (no symlink permission) where
                # huggingface_hub falls back to hardlinks in the first place.
                # A real (uncached) stat is the only way to get true values;
                # the extra syscall is paid only here — once per real file,
                # Windows only — never on the POSIX path above, where
                # DirEntry.stat() already answers this correctly for free.
                #
                # Guarded like the entry.stat() above, and for the same
                # reason: this is a SECOND trip to the filesystem, so a blob
                # that a live download (or a `hf` cache cleanup) removes
                # between the scandir and here raises FileNotFoundError. Left
                # unguarded that aborted the whole listing — the exact
                # "report what we could see rather than failing the page"
                # contract the enclosing loop is built on.
                try:
                    st = os.stat(entry.path, follow_symlinks=False)
                except OSError:
                    continue
            # EVERY accumulator below reads the FINAL `st`, and none of them run
            # before the re-stat above can rule the file out. A file that
            # vanishes in that window has to count for nothing at all: dating
            # the repo from a blob whose size we then refuse to count is not a
            # partial answer, it is a wrong one. `lastUsed` is the field that
            # makes it concrete — it drives prune selection in the client, so a
            # deleted blob's atime leaking in here marks a stale repo as
            # recently used and protects it from the very cleanup that removed
            # the blob. (On win32 these now read the fresh stat rather than the
            # cached DirEntry one, which is also the stat that size and st_nlink
            # come from — one consistent view of the file, not two.)
            if st.st_mtime > newest:
                newest = st.st_mtime
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

# The vocabulary itself lives in `ai/tasks.py` — every `pipeline_tag` the Hub
# serves, vendored from `@huggingface/tasks`, each classified as served by a
# capability or explicitly not. This module produces TAGS from every evidence
# path below and never prose: one spelling, one glossary key, one thing to
# classify. (It used to end in `tag.replace("-", " ")`, which made the
# vocabulary open — see that module's docstring for the two bugs that caused.)

# transformers architecture suffix -> task. Ordered: the first match wins, so
# the more specific suffixes come before the ones they contain.
_ARCH_TASKS = (
    ("ForZeroShotImageClassification", "zero-shot-image-classification"),
    ("ForImageClassification", "image-classification"),
    ("ForImageSegmentation", "image-segmentation"),
    # The three other segmentation heads transformers ships. Read as nothing at
    # all they fell through to the format branch, which is text.
    ("ForSemanticSegmentation", "image-segmentation"),
    ("ForInstanceSegmentation", "image-segmentation"),
    ("ForUniversalSegmentation", "image-segmentation"),
    ("ForObjectDetection", "object-detection"),
    ("ForZeroShotObjectDetection", "zero-shot-object-detection"),
    # `Intel/dpt-beit-base-384`, and the case that showed the gap: its
    # downloaded card's front matter is `license: mit` and nothing else, so the
    # architecture is the whole of the local evidence and an unlisted suffix
    # meant "no task", which the format fallback then read as a chat model.
    ("ForDepthEstimation", "depth-estimation"),
    ("ForVideoClassification", "video-classification"),
    ("ForAudioClassification", "audio-classification"),
    ("ForAudioFrameClassification", "audio-classification"),
    ("ForTextToSpectrogram", "text-to-speech"),
    ("ForTextToWaveform", "text-to-speech"),
    ("ForVisualQuestionAnswering", "visual-question-answering"),
    ("ForDocumentQuestionAnswering", "document-question-answering"),
    ("ForMultipleChoice", "multiple-choice"),
    ("ForSequenceClassification", "text-classification"),
    ("ForTokenClassification", "token-classification"),
    ("ForQuestionAnswering", "question-answering"),
    ("ForSpeechSeq2Seq", "automatic-speech-recognition"),
    # An encoder-decoder (T5-shaped). Not the causal-LM path mlx-lm serves,
    # however much "generation" in the name suggests it — and the Hub retired
    # its own `text2text-generation` tag, so the honest surviving tag for what
    # such a checkpoint is USED for is `translation` (its sibling
    # `summarization` classifies the same way, and neither is served here).
    ("ForConditionalGeneration", "translation"),
    ("ForMaskedLM", "fill-mask"),
    ("ForCausalLM", "text-generation"),
    ("LMHeadModel", "text-generation"),
    ("ForCTC", "automatic-speech-recognition"),
)

# …ForConditionalGeneration is the same head for "translate this" and "transcribe
# this", so the model type is what separates them.
_AUDIO_MODEL_TYPES = {"whisper", "speech_to_text", "speecht5", "seamless_m4t"}

# …and the same head AGAIN for speech OUT. `Qwen/Qwen3-TTS-12Hz-1.7B-Base`
# publishes no `pipeline_tag` at all — its sibling VoiceDesign repo does — so
# the architecture is the only evidence there is, and read as a bare
# `…ForConditionalGeneration` it came out "translation": the right VERDICT (no
# runner either way) under a label that is simply wrong about what the model
# does. Matched on the model type rather than on the architecture name, the
# same way the audio-IN case above is, because the head is shared and the type
# is what distinguishes them. A substring test: the family names it
# (`qwen3_tts`, `parler_tts`, `xtts`), and an exact list would need a new entry
# for every synthesis family that ships — the maintenance that let the two
# multimodal families arrive mislabelled.
_SPEECH_OUT_MARKERS = ("_tts", "tts_", "vits", "bark", "vall_e")

# …and the same head again for a vision-language model. This sub-config is how
# a multimodal wrapper says so — a nested block per extra tower — and it is
# keyed on rather than a list of model types because the list would need a new
# entry for every family that ships (gemma3, gemma4, qwen3_5, whatever is
# next), which is the maintenance that let the last two arrive mislabelled.
#
# **A `vision_config` and ONLY a `vision_config`.** An audio tower is not
# evidence of a vision one, and treating any extra tower as multimodal-therefore
# -VLM made `Qwen2AudioForConditionalGeneration` — audio, no vision — an "image
# + text to text" model, which in this app means text generation and therefore a
# Load button aimed at a runner that cannot use it. The gemma-4 checkpoints have
# BOTH towers and are still vision-language models, which is why this is a test
# for vision rather than a test for exactly-one-tower.
_VISION_CONFIG = "vision_config"

# The audio-only case, which is a real thing with no runner here: mlx-lm
# resolves a checkpoint by importing `mlx_lm.models.<model_type>` and ships no
# `qwen2_audio`, so this label lives in `NO_RUNNER_YET` and the card correctly
# offers nothing to press.
_AUDIO_CONFIG = "audio_config"


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


# The glossary lives with the vocabulary (`ai/tasks.py`), keyed by TAG. It used
# to sit here keyed by prose label, which is how one concept read from a card
# and from a config produced two spellings and only one of them had a sentence.



@dataclass
class _RepoMeta:
    """What a repo is for and how big the model is — read from the default
    revision's snapshot, or empty when the download brought no evidence."""

    #: The Hub `pipeline_tag` this repo is, from whichever evidence answered —
    #: a TAG, never prose, whether it came from the card or from our own
    #: reading of a config (`ai/tasks.py`). None when nothing said.
    task: str | None = None
    task_source: str | None = None
    #: The config DECLARED an architecture and `_ARCH_TASKS` could not map its
    #: suffix — which is a fact, not a silence, and the difference matters to
    #: exactly one caller (`cached_capability`'s format fallback).
    #:
    #: transformers names a checkpoint's head in that string, and mlx-lm
    #: resolves a checkpoint by importing `mlx_lm.models.<model_type>` for a
    #: CAUSAL LM. So "this repo says it is a `…ForDepthEstimation`" is positive
    #: evidence that the text runner cannot open it, even though the task came
    #: out unknown — and treating it as "we know nothing, let the file
    #: extensions decide" is how `Intel/dpt-beit-base-384` landed in the
    #: Playground's TEXT section.
    unmapped_arch: bool = False
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
    # The snapshot's own top-level filenames. Carried for
    # `_catalog_with_downloads`'s benefit (ai_runtime.py): a curated id keyed
    # by FILENAME (`formats.GGUF_RECIPES`) can only tell whether it is
    # "downloaded" by checking one of these names, since the repo-level
    # `on_disk` set that check used for every other runner cannot see which
    # of a repo's several curated quantizations is actually present.
    names: frozenset[str] = frozenset()


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
    """The card's `pipeline_tag`, verbatim.

    A function rather than nothing, because the CARD path and the four evidence
    paths below must produce the same kind of value, and a reader of
    `_repo_meta` should be able to see that the card is not treated specially.
    Unrecognised tags are NOT rejected here — `tasks.classify` answers
    `UNKNOWN` for them and the card still shows what the author wrote."""
    return tag.strip()


def _diffusers_task(class_name: str) -> str:
    """The pipeline class in this snapshot, as a Hub tag.

    The `text-to-*` tags rather than bare "video generation" / "image
    generation": a diffusers pipeline IS prompted, and folding the two
    spellings onto the Hub's own tag is what lets one glossary entry and one
    classification serve both this path and a model card that says the same
    thing."""
    lowered = class_name.lower()
    if "video" in lowered:
        return "text-to-video"
    if "audio" in lowered or "music" in lowered:
        return "text-to-audio"
    return "text-to-image"


def _declares_architecture(config: dict) -> bool:
    """Does this config NAME a head at all?

    Separate from `_architecture_task` returning None, which conflates "no
    architectures key" with "a key this table does not know" — and those are
    opposite facts for a caller deciding whether to trust the file extensions.
    """
    architectures = config.get("architectures")
    name = architectures[0] if isinstance(architectures, list) and architectures else None
    return isinstance(name, str) and bool(name)


def _architecture_task(config: dict) -> str | None:
    architectures = config.get("architectures")
    name = architectures[0] if isinstance(architectures, list) and architectures else None
    if not isinstance(name, str):
        return None
    for suffix, task in _ARCH_TASKS:
        if name.endswith(suffix):
            if suffix == "ForConditionalGeneration":
                # One head, four jobs, and the config is what tells them apart.
                model_type = config.get("model_type")
                if model_type in _AUDIO_MODEL_TYPES:
                    return "automatic-speech-recognition"
                if isinstance(model_type, str) and any(
                        marker in model_type.lower() for marker in _SPEECH_OUT_MARKERS):
                    return "text-to-speech"
                # A MULTIMODAL WRAPPER — a language model with a vision (and
                # sometimes audio) tower bolted on, which is what every current
                # Qwen3.5 and gemma-4 checkpoint is, including the ones this
                # app's own MLX catalog recommends. mlx-lm loads the language
                # tower and ignores the rest, so it is a chat model here; read
                # as a bare `…ForConditionalGeneration` it came out
                # "text-to-text generation", a label in NO_RUNNER_YET, and the
                # newest models the app suggests arrived on this page as T5s
                # with no Load button. The label is the one the CARD path
                # already produces for these repos, so the two agree.
                if _VISION_CONFIG in config:
                    return "image-text-to-text"
                if _AUDIO_CONFIG in config:
                    return "audio-text-to-text"
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


def _format_task(repo_id: str, names, dirnames, config: dict) -> tuple[str, str] | None:
    """What the WEIGHT LAYOUT says this is, when it says anything at all.

    Only the formats that are decisive about the modality — a `weights.npz` is
    a Whisper conversion and can be nothing else; a folder of mflux components
    is a diffusion pipeline. A directory of safetensors says nothing about what
    the model does, and is deliberately not here.

    Each predicate is `formats`', not a second reading of the same filenames,
    so the label a card shows and the engine that would load it are decided
    from one description of each backend.

    **Stricter than `formats.loaders` for CTranslate2 on purpose.** The runner
    loads anything with a `model.bin`, which is the right test for a loader and
    too loose for a label: "speech recognition" printed on a card because a
    stray pickle happened to be called model.bin would be a confident lie.
    """
    if formats.is_ct2_whisper(names, config):
        return "automatic-speech-recognition", "its CTranslate2 Whisper layout"
    if formats.is_mlx_whisper_snapshot(names, config):
        return "automatic-speech-recognition", "its MLX Whisper weights"
    if repo_id in formats.MFLUX_VARIANTS and formats.has_mflux_components(dirnames):
        return "text-to-image", "its MLX diffusion components"
    return None


def _has_torch_weights(snapshot_dir: str) -> bool:
    """Is there anything in this revision a safetensors reader could open?

    A WALK, like `_weight_files`, and for the same reason: a diffusers pipeline
    keeps its weights per component. The suffixes are `formats.TORCH_WEIGHTS`
    rather than a list spelled here, so the page and `formats.loaders()` cannot
    come to disagree about what counts as weights — this was the page's copy of
    the question the removed `runners/torch_text.py` asked before refusing a
    repo (D416), and `mlx-text` and the Diffusers rows still ask it.
    """
    for _dirpath, _dirnames, filenames in os.walk(snapshot_dir):
        if any(name.endswith(formats.TORCH_WEIGHTS) for name in filenames):
            return True
    return False


def _has_onnx_weights(snapshot_dir: str) -> bool:
    """Is there anything in this revision an `InferenceSession` could open?

    `_has_torch_weights`'s sibling, and a WALK for a stronger reason than that
    one has: every ONNX export this app reads keeps its graphs in an `onnx/`
    SUBDIRECTORY (`onnx/text_model.onnx`, `onnx/model.onnx`), so a check over the
    snapshot's top level would conclude that no ONNX repo anywhere holds weights
    — the format's convention is the subfolder, not the root.

    The suffixes are `formats.ONNX_WEIGHTS` rather than a list spelled here, for
    `_has_torch_weights`'s reason exactly: the page and `formats.loaders()` must
    not come to disagree about what counts as weights.
    """
    for _dirpath, _dirnames, filenames in os.walk(snapshot_dir):
        if any(name.endswith(formats.ONNX_WEIGHTS) for name in filenames):
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
    # The snapshot's top-level FOLDERS, and the repo this is. Both are read here
    # rather than beside the `loaders` call below, because the evidence chain
    # now uses them too: an mflux conversion is recognised by its component
    # folders and by its id being one this build can name a variant class for.
    try:
        dirnames = {e.name for e in os.scandir(snapshot) if _entry_is_dir(e)}
    except OSError:
        dirnames = set()
    repo_id = _repo_id_of(os.path.basename(os.path.normpath(repo_dir)))

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
        meta.task, meta.task_source = "feature-extraction", "its sentence-transformers config"
        library = library or "sentence-transformers"

    # Read once, whatever the task turned out to be: the architecture is only
    # one of the things config.json answers, and the weight width is needed even
    # for a repo whose card already named its task.
    config = _read_json(os.path.join(snapshot, "config.json")) or {} if "config.json" in names else {}
    if meta.task is None and config:
        task = _architecture_task(config)
        if task:
            meta.task, meta.task_source = task, "the architecture in config.json"
        elif _declares_architecture(config):
            # Read, and not recognised — see `_RepoMeta.unmapped_arch`.
            meta.unmapped_arch = True

    # A GGUF file names the LIBRARY and nothing else. It used to name the task
    # too — "text generation", unconditionally — which put a Load button on
    # `unsloth/FLUX.2-klein-4B-GGUF`, an image model, and is the precise failure
    # `capability_for_task` warns about. A GGUF is a container, not a modality,
    # so this still does not set `meta.task` for one — the LIBRARY tag here is
    # honest about any GGUF repo whatever it contains, but a TASK (and so a
    # Load button) only ever comes from `meta.loaders` below, which since
    # SPEC AI-11 asks the file's OWN `general.architecture` metadata
    # (`formats.is_text_gguf`) before ever calling one decisively
    # `llamacpp-text` — the FLUX.2 klein case this comment used to warn about
    # stays capability-less (its `general.architecture` is not a text one),
    # while a real Qwen3.5 GGUF resolves through `cached_capability`'s
    # `meta.loaders` fallback, the same way an unlabelled directory of
    # safetensors resolves through the two TEXT runners' shared capability.
    if any(n.lower().endswith(".gguf") for n in names):
        library = library or "gguf"

    # Last: the WEIGHT LAYOUT, which answers where nothing above did AND
    # overrules an answer this app cannot act on.
    #
    # The first half is the CTranslate2 case — a conversion carries no
    # pipeline_tag and no `architectures`, so this app's own recommended speech
    # model showed a card with no task line and no Load button, while
    # `faster_whisper/worker.py` recognises it from one filename.
    #
    # The second half is `mlx-community/FLUX.2-Klein-4B-4bit`, and it is why
    # this runs when a task is already known: it has no config.json at all, so
    # its card's `image-to-image` tag stood — a label in NO_RUNNER_YET — while
    # the very same snapshot is an mflux image model this machine serves. That
    # put ONE model on the page under two labels (the diffusers repo beside it
    # reads "image generation"), and it put a Load button under a task the app
    # says nothing can run, because the button keys on the FORMAT and the label
    # did not. Decisive format evidence settles both, so they cannot disagree.
    #
    # Only where the current answer maps to no capability, which is what keeps
    # this from overruling a card that was right: a genuine img2img repo with no
    # such evidence keeps its label, and so does a VLM whose label already
    # resolves to text generation.
    if not _tasks.classify(meta.task).supported:
        found = _format_task(repo_id, names, dirnames, config)
        if found:
            meta.task, meta.task_source = found

    if meta.task:
        meta.task_help = _tasks.help_for(meta.task)

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

    # A root `.gguf`'s OWN architecture metadata, read once here rather than
    # inside `formats.loaders` — that module is a pure evidence classifier
    # with no file I/O of its own (see its docstring), and this function
    # already has the snapshot path and the listing. Sorted so the answer is
    # deterministic when a repo somehow carries more than one root `.gguf`
    # (this app never produces one, but a Hub repo is not this app's to
    # control) — the first is representative, since every curated recipe
    # this runner reads means one file per repo per id.
    gguf_architecture = None
    gguf_files = sorted(n for n in names if n.lower().endswith(formats.GGUF_EXTENSION))
    if gguf_files:
        gguf_architecture = formats.gguf_architecture(os.path.join(snapshot, gguf_files[0]))

    # Which backend's `load()` would accept this, by format alone. Cached with
    # everything else because it reads the same listing and the same config —
    # and asked of `formats`, never re-derived here: a second copy of "what a
    # CTranslate2 repo looks like" is how a card comes to promise a load the
    # runner refuses.
    meta.loaders = formats.loaders(
        repo_id=repo_id,
        names=names,
        dirnames=dirnames,
        config=config,
        torch_weights=_has_torch_weights(snapshot),
        onnx_weights=_has_onnx_weights(snapshot),
        gguf_architecture=gguf_architecture,
    )
    # The snapshot's own top-level listing, carried past this function for
    # the one caller that needs it beyond format inference:
    # `_catalog_with_downloads` (ai_runtime.py) has to know whether a
    # SPECIFIC curated file is present, not merely whether the repo is —
    # `formats.GGUF_RECIPES` keys entries by filename precisely because one
    # repo can curate more than one quantization.
    meta.names = frozenset(names)

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


#: The part-file suffixes a fetch leaves in `blobs/` while it is working — and
#: KEEPS when it is interrupted, so the next attempt resumes instead of starting
#: over (D275/AI-5i). Ours is `runners/worker_base.PART_SUFFIX`; `.incomplete` is
#: `huggingface_hub`'s own.
#:
#: Named here rather than imported from the fetcher because this module reads a
#: cache several writers share — `hf`, transformers, diffusers, a template a user
#: pasted in — so the reading has to hold for repos this app never fetched.
#: `test_ai_models_api.py` pins ours against the fetcher's own constant, so the
#: two names cannot drift apart in silence.
_PART_SUFFIXES = (".fusedpart", ".incomplete")


def _unfinished_fetch(repo_dir: str) -> bool:
    """Whether this repo folder is a download that never finished (D424).

    **POSITIVE EVIDENCE ONLY, and that is the whole design of this predicate.**
    A repo is called partial when something in it is the visible residue of a
    fetch that stopped — never merely because a completion marker is missing.
    The tempting readings all fail on repos that are perfectly complete:

    * **"no `.fused-fetch-<commit>.json` record"** — that record is written only
      by THIS app's fetcher, so every repo pulled by the `hf` CLI, by
      transformers, or by a version of this app older than the record would read
      as half-downloaded.
    * **"no `refs/`"** — a repo pinned at a commit sha has no ref by design
      (neither hf nor `_write_ref` writes one named after a sha), and would read
      the same way.
    * **"nothing here reads the format"** — that is `_engine`'s answer for a
      fully downloaded repo nobody's backend opens (a SigLIP tower, ACE-Step),
      and calling those partial would offer to resume a download that finished
      months ago. The format never enters this question.

    So two facts, both of which are something that IS there:

    1. **A part file in `blobs/`.** Only an interrupted (or in-flight) fetch
       leaves one: our own `finish()` renames the part over the blob and drops
       its sidecar, hf does the same with `.incomplete`, and the one path that
       abandons ours (`worker_base._clear_parts`, taken when a segmented fetch
       falls back to hf) deletes them before handing the repo over. A cancel
       does NOT go that way — `except Cancelled: raise` — which is exactly why
       the bytes, and therefore this evidence, are still here.
    2. **No snapshot directory at all.** A folder with blobs and nothing to open,
       which is the state hub search has always called `partial` and the state a
       cancel before the first file lands leaves behind.
    """
    try:
        # `list()`, not a `with`, like every other scandir in this module: the
        # walk in `_scan_repo` is the one every test stubs, and a reading that
        # needed the context-manager protocol would be the only one here that
        # could not be driven the same way.
        entries = list(os.scandir(os.path.join(repo_dir, "blobs")))
    except OSError:
        # No blobs/ yet, or unreadable. Not evidence either way — the snapshot
        # question below is what answers for a folder this bare.
        entries = []
    for entry in entries:
        # The sidecar beside a part file ends `.json` and does not match, which
        # is right: the part file IS the unfinished download, the sidecar is only
        # its bookkeeping, and `finish()` removes the two together.
        if entry.name.endswith(_PART_SUFFIXES):
            return True
    return not _snapshot_dirs(os.path.join(repo_dir, "snapshots"))


def _fetched_bytes(repo_dir: str, scanned: int) -> int:
    """How many of this repo's bytes actually ARRIVED (D440).

    **Not the same number as the folder's size, and the difference is the bug
    this exists for.** Our fetcher PREALLOCATES a part file to the full length of
    the file it is fetching, so a repo 15% of the way through a 1.6GB download
    measures 1.6GB — and a card drawing "how much of this is here" from that read
    as nearly finished while the job row beside it said 243 MB. `_scan_repo` is
    not wrong to count those bytes (the file really is that long, and this page's
    other job is telling you what is eating the disk); they are just not an answer
    to "how much arrived".

    So this is the SCANNED total with one correction applied per part file:
    subtract the length that was counted, add back what is durable. Starting from
    the scan rather than re-adding the blobs is what keeps a finished repo's two
    numbers identical — refs, snapshot entries and any stray file are counted
    once, by the one walk that already knows how to count them.

    Durable, per kind, from the evidence each kind carries:

    * **`.fusedpart`** — the sidecar's segment cursors, the same accounting a
      resume trusts: `flush()` fsyncs the data BEFORE recording an offset, so a
      recorded cursor is always bytes the disk really has (see
      `worker_base._FileFetch.flush`). No sidecar, or an unreadable one, counts
      ZERO rather than the file's length: positive evidence only, the same posture
      `_unfinished_fetch` takes, because the file may be pure preallocation.
    * **`.incomplete`** — `huggingface_hub` APPENDS, so the length already IS the
      progress. Left alone, which is why only our own suffix is corrected here.
    """
    corrected = scanned
    try:
        entries = list(os.scandir(os.path.join(repo_dir, "blobs")))
    except OSError:
        return scanned
    for entry in entries:
        if entry.name.endswith(worker_part_suffix() + ".json"):
            # The sidecar's own bytes. Real, counted by the scan, and not part of
            # the model — a card saying "9 bytes of this 1.6GB model are here"
            # because a sidecar exists would be a fraction made of bookkeeping.
            corrected -= _blob_size(entry.path)
            continue
        if not entry.name.endswith(worker_part_suffix()):
            continue
        corrected -= _blob_size(entry.path)
        corrected += _part_progress(entry.path + ".json")
    # A sidecar recording more than its part file is long would otherwise push
    # this above the scan; a negative is impossible but cheap to rule out.
    return max(0, corrected)


def worker_part_suffix() -> str:
    """Our own part suffix, which is the ONLY one `_fetched_bytes` corrects.

    A function rather than a second constant so the reason stays attached: hf's
    `.incomplete` is in `_PART_SUFFIXES` too (both mean "unfinished", which is
    what `_unfinished_fetch` asks), and it must NOT be corrected, because that
    writer appends and its length is already the progress.
    """
    return ".fusedpart"


def _part_progress(sidecar: str) -> int:
    """Durable bytes recorded for one part file, or 0 when nothing says."""
    try:
        with open(sidecar) as handle:
            state = json.load(handle)
        segments = state["segments"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    done = 0
    for segment in segments:
        try:
            width = int(segment["end"]) - int(segment["start"]) + 1
            done += max(0, min(int(segment["done"]), width))
        except (KeyError, TypeError, ValueError):
            continue
    return done


def _engine(meta: _RepoMeta, reading: _tasks.Classification) -> tuple[dict | None, str | None]:
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
    capability = reading.capability
    if not meta.loaders:
        return None, capability
    runners = [r for r in _ai_registry.all_runners() if r.code in meta.loaders]
    if capability is None and not reading.ruled_out:
        # **Only for a task we could not identify.** A RULED-OUT task is not
        # rescued by the format — the bug this guard fixes was a cached
        # diffusers VIDEO pipeline whose `_class_name` said `text-to-video`
        # while nothing here generated video at all, and the diffusers
        # runners are DECISIVE about the format regardless — so this branch
        # used to answer "text-to-image" and put a Load button on it.
        # `text-to-video` itself stopped being an example of a ruled-out
        # task once `ltx-video` shipped (SPEC §40's LTX-2.3 plan;
        # `ai/tasks.py` maps it to `VIDEO_GENERATION` now, genuinely
        # SUPPORTED), but the guard is unchanged and still needed: `image-
        # to-video` is a still-ruled-out sibling tag (no runner here is
        # image-conditioned) that a decisive format could resurrect the
        # identical way — see `test_ai_models_api.py`'s own test for that
        # exact case. The mflux/CT2 cases the branch exists for are
        # unaffected either way, because `_format_task` has already
        # overruled their misleading labels by the time we get here, making
        # the reading SUPPORTED rather than ruled out.
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
    # **The engine that would SERVE, not merely one that could read it.** A
    # capability holds one resident model and the registry picks which backend
    # loads it, so a repo readable by the OTHER backend is not loadable today
    # however available that backend is: `black-forest-labs/FLUX.2-klein-4B` is
    # a Diffusers repo on a Mac whose image engine is MLX FLUX, and a Load there
    # reaches a runner that refuses it by name. That is the exact promise this
    # field exists to stop the page making.
    serving = _ai_registry.for_capability(capability)
    if serving is not None and any(r.code == serving.code for r in candidates):
        status = serving.available()
        return {
            "code": serving.code,
            # All three, named so a reader cannot pick the wrong one by
            # accident: the card's tag wears the FAMILY (a tag is a format
            # claim, and the hardware qualifier is neither part of that claim
            # nor a fact about the file), its hover wears the short name that
            # says which build would load it, and the full one stays here for
            # anything that has to match the Preferences picker word for word.
            "label": serving.label,
            "shortLabel": serving.short,
            "familyLabel": serving.family,
            "available": status.ok,
            "reason": status.reason or None,
        }, capability
    # Otherwise: name the backend that DOES read it, and say what stands in the
    # way. Both reasons are actionable and they are different actions — one is
    # "this needs another machine", the other "this needs the other engine,
    # which is one tab away on the page this sentence is printed on".
    # ONE probe per candidate, and the chosen row's status is the one already
    # read — never `available()` again on the winner (D382). These probes read
    # live device state now, so a second call can straddle a `modprobe` or a
    # container restart and answer differently: `available: false` with
    # `reason: null` (a card refusing to load with nothing saying why) or an
    # `ok` status still carrying the refusal that picked this row.
    probed = [(runner, runner.available()) for runner in candidates]
    runner, status = next((pair for pair in probed if pair[1].ok), probed[0])
    reason = status.reason or None
    if status.ok and serving is not None:
        # Names the TAB, not a settings page: the engine picker moved onto
        # /ai-models, so the remedy for a card the user is looking at is beside
        # the card. Directions are worth less the further away they point.
        reason = (f"{capability} is set to {serving.short}, which does not read "
                  f"this format — switch it on the Engines tab")
    elif status.ok:
        reason = f"nothing serves {capability} on this machine"
    # The same three names as the serving branch above — the card that wears
    # this payload is the same card, and a tag left blank on exactly the repos
    # that need explaining is the worst place to drop a key.
    return {
        "code": runner.code,
        "label": runner.label,
        "shortLabel": runner.short,
        "familyLabel": runner.family,
        "available": False,
        "reason": reason,
    }, capability


class CacheReading(NamedTuple):
    """What the local cache says a repo is, for a caller that is not this page.

    `cached` is whether there is a revision to read at all; `capability` is what
    a load of it would be (None when nothing here can tell); `looks_like` is the
    same evidence in words, for an error message.

    `support` and `reason` are the THREE-STATE answer behind that `capability`
    (`ai/tasks.py`): a null capability is "we run this kind of model but not
    from this format", "we do not run this kind of model at all", or "we cannot
    tell what this is", and a refusal that cannot tell them apart cannot explain
    itself. `tag` is the vocabulary key those came from, for a caller that wants
    to link to the glossary rather than reprint a sentence.

    `runner_code`/`runner_reason` are `_engine`'s own answer to "which BACKEND
    reads this file", carried past `capability` because a capability CAN be
    shared by runners reading mutually unloadable layouts (video generation
    was two such runners until D468 dropped one). `capability` alone
    cannot tell a caller whether the SERVING runner is the one that reads
    this repo — `runner_code` is that runner's code, and it can differ from
    whichever runner `registry.for_capability(capability)` resolves to right
    now. `None` for a repo `_engine` never named a specific runner for (not
    cached, a component, or the rare case where several runners share a
    capability with no decisive format evidence at all — `mlx-text`'s own
    fallback below).
    """

    cached: bool
    capability: str | None
    looks_like: str | None
    support: str = _tasks.UNKNOWN
    reason: str = ""
    tag: str | None = None
    runner_code: str | None = None
    runner_reason: str | None = None


def has_vision_tower(repo_id: str) -> bool:
    """Does the cached snapshot of `repo_id` declare a vision tower?

    **Exported, and read by `ai_runtime._accepts_image`** (AI-11j): a
    TEXT_GENERATION entry may be handed an image only when the resolved
    runner is the MLX one AND the checkpoint it names actually has a vision
    tower, and this is how that half is answered WITHOUT loading the model —
    the AI Models page draws an attach button off this before any request
    ever reaches the worker.

    Read straight off `config.json` — a `vision_config` block and/or an
    `image_token_id`, the same evidence `_architecture_task` already uses
    (see `_VISION_CONFIG`, above) to route a unified vision-language
    checkpoint to `image-text-to-text` rather than plain `text-generation`.

    **Gotcha, verified by hand: do not decide this by globbing weight
    files.** `Qwen3.5-*-OptiQ` keeps its vision tower in a SIDE-CAR
    `optiq/optiq_vision.safetensors`, never in `model.safetensors` — a
    non-recursive glob over the snapshot's top level concludes these
    checkpoints have no vision weights at all, which is wrong for every
    OptiQ entry this app's own catalog recommends. `config.json` declares
    `vision_config`/`image_token_id` regardless of where the tower's OWN
    weights physically live, which is why this reads THAT and never a file
    listing.

    False whenever the answer cannot be determined — no snapshot on disk, an
    unreadable or empty config, a hostile repo id — because an attach button
    whose request then 400s is exactly the failure AI-11j exists to prevent,
    and "no image support" is the failure-closed direction for a control this
    permissive.
    """
    dirname = "models--" + repo_id.replace("/", "--")
    # The same path-segment guard `cached_capability` applies below: a repo id
    # reaches here out of a request body, and is not a place to go looking
    # for `..` or a path separator.
    if dirname != os.path.basename(dirname) or ".." in dirname or "\\" in dirname:
        return False
    repo_dir = os.path.join(hub_cache_dir(), dirname)
    snapshot_dir = _default_snapshot(repo_dir)
    if snapshot_dir is None:
        return False
    config = _read_json(os.path.join(snapshot_dir, "config.json"))
    if not config:
        return False
    return _VISION_CONFIG in config or "image_token_id" in config


def cached_capability(repo_id: str) -> CacheReading:
    """Which capability `repo_id` would load as, read off the local snapshot.

    **Exported, and read by the load route** (`ai_runtime.py`, D321). A load
    that omitted `capability` used to mean text generation unconditionally, so
    an MLX diffusion repo went to mlx-lm and came back as a FileNotFoundError
    about a `config.json` that repo has never had. The evidence that settles it
    is the evidence this page already draws its Load button from — so it is
    asked HERE rather than re-derived there, because a card that offers Load and
    a load that then refuses must not be able to disagree.

    Local only: `_repo_meta` reads the snapshot directory and nothing else, so
    this adds no network call to a path that had none. A repo with no revision
    on disk is `cached=False` — an interrupted download leaves a folder behind,
    and a folder is not evidence.
    """
    dirname = "models--" + repo_id.replace("/", "--")
    # Built from a request body, so it is checked to be one path segment, the
    # same way `_segment` checks a delete target. Anything else reads as "not
    # cached" rather than as a path to go looking at.
    if dirname != os.path.basename(dirname) or ".." in dirname or "\\" in dirname:
        return CacheReading(False, None, None)
    repo_dir = os.path.join(hub_cache_dir(), dirname)
    if not os.path.isdir(repo_dir) or _default_snapshot(repo_dir) is None:
        return CacheReading(False, None, None)

    meta = _repo_meta(repo_dir)
    # The page's own join, in the page's own order: the task first, then the
    # decisive formats, which is what `_engine` exists to combine.
    reading = _tasks.classify(meta.task)
    _row, capability = _engine(meta, reading)
    if (capability is None and meta.loaders
            and reading.support == _tasks.UNKNOWN and not meta.unmapped_arch):
        # Nothing DECISIVE and nothing that told us what this IS, but the
        # runners that read the format may still agree about it: a directory of
        # safetensors with a `config.json` mlx-lm can resolve is read only by
        # the TEXT runner, and their shared capability is a fact about the
        # format rather than a guess about the model. This is what keeps every
        # existing `load(id)` on an unlabelled chat repo working.
        #
        # **Gated on UNKNOWN, and that gate is the point.** A task we recognise
        # and have ruled out must not be rescued here: `SymphonyGen/SymphonyGen`
        # is a symbolic-music policy (`reinforcement-learning`, four `.pt`
        # checkpoints) and this fallback made it a chat model in the
        # Playground's TEXT section, with a Load aimed at an mlx-lm that has no
        # `config.json` to read. `formats.loaders` refuses that repo now too —
        # two guards, because the failure was silent in both.
        found = {r.capability for r in _ai_registry.all_runners()
                 if r.code in meta.loaders}
        if len(found) == 1:
            capability = found.pop()

    component = formats.component(repo_id)
    if component:
        # A component is nobody's `load()` target, and the refusal reads much
        # better naming what it actually is: "a speech detector that belongs to
        # MLX Whisper" rather than "a model repo".
        return CacheReading(
            True, None,
            f"{_article(component['part'])} {component['part']} that belongs to "
            f"{component['owner']}")
    if reading.label:
        looks_like = f"{_article(reading.label)} {reading.label} model"
    elif meta.library:
        looks_like = f"{_article(meta.library)} {meta.library} repo"
    else:
        looks_like = None
    # A repo whose TASK is served but whose FORMAT is not keeps its ruled-out
    # sentence from `_engine`'s own reason (the card shows that one); what
    # travels here is the vocabulary's answer, which is the half a load route
    # needs to explain a refusal without re-deriving anything.
    return CacheReading(True, capability, looks_like,
                        reading.support, reading.reason, reading.tag,
                        _row.get("code") if _row else None,
                        _row.get("reason") if _row else None)


def _article(word: str) -> str:
    """"a" or "an". A label this reads wrong ("a image to image model") is a
    sentence a page author is meant to act on, so it is worth the four lines."""
    return "an" if word[:1].lower() in "aeiou" else "a"


class CachedModel(NamedTuple):
    """One MODEL repo on this disk, as a caller building a picker needs it.

    `capability` is what a load of it would be, or None when nothing here can
    tell. `size` is every byte the repo occupies, measured — the same number this
    page's own row reports.

    **`loaders` is not decoration, and a caller that ignores it will offer models
    that cannot be loaded.** A capability is not enough: in this app a repo belongs
    to a BACKEND, and the capability's two or three backends read mutually
    unloadable formats. `openai/whisper-large-v3` is a speech model that NEITHER
    shipping speech runner reads; `mlx-community/Qwen3-8B-MLX-4bit` is a text model
    that llama.cpp cannot open, so on a Mac switched to llama.cpp it is an
    unusable download. Both have a perfectly good `capability`. The
    format's own answer — which runner codes would accept this snapshot, straight
    from `ai/runners/formats.py`, the same evidence each worker checks before it
    imports anything — is the half that settles it, so it travels with the row.
    """

    repo_id: str
    capability: str | None
    size: int
    loaders: tuple[str, ...] = ()
    # The snapshot's own top-level filenames — see `_RepoMeta.names`, whose
    # value this carries verbatim. Needed by the same caller for the same
    # reason: `on_disk` (a set of repo ids) cannot say WHICH of a repo's
    # curated quantizations is present, only that the repo is.
    files: frozenset[str] = frozenset()
    # What the model DOES and why we do not run it, when we do not
    # (`ai/tasks.py`). Carried so a picker can SHOW an unloadable download
    # rather than silently dropping it: "you have this, and here is why there
    # is no button" is a sentence only this side can write, and a page that
    # omits the row instead answers the user's next question ("where did my
    # download go?") with nothing at all.
    task: str | None = None
    support: str = _tasks.UNKNOWN
    reason: str = ""


#: `cached_models()`'s memo: cache dir -> (read time, signature, answer). See the
#: function for why there are two invalidation conditions rather than one.
_CACHED_MODELS: dict[str, tuple[float, tuple, list[CachedModel]]] = {}

#: How long a `cached_models()` answer may stand when neither the cache dir's own
#: signature nor any repo's has moved. A BACKSTOP, not the mechanism — the
#: signatures are what make a finished download visible immediately, so this only
#: has to bound the one case they cannot see (bytes growing inside a blob that is
#: already listed), and it is minutes rather than seconds because every second
#: shaved off it buys another full recursive stat-walk of the whole cache.
_CACHED_MODELS_TTL = 300.0

#: `_repo_size()`'s memo: repo dir -> (signature, bytes). Unbounded like
#: `_META_CACHE`, and for the same reason: one small tuple per repo the user has
#: ever had cached, on a machine where each of those repos is gigabytes.
_SIZE_CACHE: dict[str, tuple[tuple, int]] = {}

#: The clock, through a module-local name so a test can freeze it WITHOUT freezing
#: `time.time` for every other thread in the process. `ai_models.time` IS the stdlib
#: module, so patching an attribute on it is process-wide: `jobs.py` stamps five
#: fields off `time.time()` and the supervisor stamps `started_at`/`loaded_at`, all
#: from daemon threads that keep running during a test.
_now = time.time


def _repo_signature(repo_dir: str) -> tuple:
    """The four directory mtimes that decide what a repo folder currently holds.

    A blob arriving renames into `blobs/`; a new revision creates a directory under
    `snapshots/`; a revision going away moves `refs/`; the folder itself moves when
    any of those subdirectories is created or removed. Each bumps the mtime of the
    parent stat-ed here, so four stats answer "has anything about this repo changed"
    — against the tens of thousands of syscalls `_scan_repo` spends to answer "how
    big is it", which is the same question one level too precise.

    The one change no directory mtime can see is a file ALREADY THERE growing:
    writes do not touch a directory's mtime. That is a download in flight, whose
    bytes the job row reports live and far better than a cache walk ever could, and
    `_CACHED_MODELS_TTL` is what bounds it.
    """
    out = []
    for name in ("", "blobs", "snapshots", "refs"):
        try:
            out.append(os.stat(os.path.join(repo_dir, name)).st_mtime_ns)
        except OSError:
            out.append(None)
    return tuple(out)


def _repo_size(repo_dir: str, signature: tuple) -> int:
    """A repo's measured footprint, cached on `_repo_signature`.

    Keyed the way `_META_CACHE` keys its own reading, and for the same reason: the
    walk behind it is recursive and `cached_models()` wants the result only to round
    it to one decimal GB.
    """
    hit = _SIZE_CACHE.get(repo_dir)
    if hit is not None and hit[0] == signature:
        return hit[1]
    size = _scan_repo(repo_dir).size
    _SIZE_CACHE[repo_dir] = (signature, size)
    return size


def cached_models() -> list[CachedModel]:
    """Every model repo on this disk that something could load, with its capability.

    **Exported, and read by `/api/ai/catalog`** (D323). A model the user downloaded
    from the Discover tab's Hub search used to appear in no page's picker at all:
    pages read the curated catalog, the curation cannot know what somebody fetched,
    and this scan — the only thing that does know — was reachable only from the AI
    Models page's own endpoint. So the join happens over THIS function, and the
    capability every entry carries is the same reading `_repo` draws its Load button
    from rather than a second copy of the inference.

    Datasets, Spaces and component repos are dropped: `kind` and
    `formats.COMPONENT_REPOS` already say those are nobody's `load()` target, and a
    picker offering one is a Load that fails. A repo whose capability cannot be
    inferred is KEPT with `capability=None` — "is this on the disk" is still a
    question worth answering about it, and the caller decides whether an
    uncategorised repo belongs in a categorised list. So is one whose format no
    runner reads: `loaders` is empty and the caller must check it (see `CachedModel`).

    **Memoised on MTIMES, not on a timer, because a page polls this route and the
    walk behind it is recursive.** The signature is every candidate repo folder's
    name plus its `_repo_signature` — four stats each, so ~120 syscalls for a
    thirty-repo cache against the tens of thousands `_scan_repo` spends across it.
    Anything that changes what the cache HOLDS moves that signature: a new repo
    folder appearing, a blob renaming into an existing `blobs/`, a revision arriving
    or going away. So a completed download is visible on the very next read and never
    waits out a timer — that is the bug this whole change exists to fix, and a TTL
    alone would have reintroduced it.

    `_CACHED_MODELS_TTL` is then the backstop for the one change no directory mtime
    can see (see `_repo_signature`), which is why it is minutes rather than seconds:
    the signature is doing the work, and every second shaved off the timer would buy
    another recursive stat-walk of the whole cache for nothing.
    """
    cache_dir = hub_cache_dir()
    try:
        entries = list(os.scandir(cache_dir))
    except OSError:
        entries = []
    # Symlinked-in repo folders are followed, exactly as `_listing` follows them:
    # moving a 40GB model off the boot volume does not stop it being a cached repo.
    # Datasets, Spaces, `.locks/` and in-flight tmp dirs drop out on the prefix.
    repos = [
        (entry.name, os.path.join(cache_dir, entry.name))
        for entry in entries
        if entry.name.startswith("models--") and _entry_is_dir(entry)
    ]
    signature = tuple(sorted(
        (name, _repo_signature(repo_dir)) for name, repo_dir in repos
    ))
    hit = _CACHED_MODELS.get(cache_dir)
    if hit is not None:
        read_at, seen, answer = hit
        if seen == signature and _now() - read_at < _CACHED_MODELS_TTL:
            return answer

    by_dir = dict(signature)
    models: list[CachedModel] = []
    for name, repo_dir in repos:
        repo_id = _repo_id_of(name)
        if formats.component(repo_id) is not None:
            continue
        # A fetch that never finished is not a model this disk HAS (D424). It is
        # the same reading the AI Models card now draws its "partly downloaded"
        # state from, asked here so a page's picker and `/api/ai/catalog` cannot
        # offer to load half a snapshot — and so a curated model whose first
        # download was cancelled goes back to being a recommendation.
        if _unfinished_fetch(repo_dir):
            continue
        # The load route's own inference, asked rather than re-derived: a picker
        # that offers a model and a `load()` that then refuses it must not be able
        # to disagree. `cached=False` is an interrupted download's leftover folder.
        reading = cached_capability(repo_id)
        if not reading.cached:
            continue
        # The FORMAT's own reading, carried so the caller can ask the question a
        # capability cannot answer: would the backend serving that capability here
        # actually open this repo? `_repo_meta` is memoised on the snapshot mtime,
        # and `cached_capability` above has already paid for it.
        repo_meta = _repo_meta(repo_dir)
        size = _repo_size(repo_dir, by_dir[name])
        models.append(CachedModel(
            repo_id, reading.capability, size, repo_meta.loaders, repo_meta.names,
            _tasks.label_for(reading.tag), reading.support, reading.reason))
    _CACHED_MODELS[cache_dir] = (_now(), signature, models)
    return models


def is_downloaded(model_id: str, cached: list[CachedModel] | None = None) -> bool:
    """Does this disk hold `model_id` — a repo id OR a curated GGUF filename?

    **The one place that answers "is this catalog entry on this machine".** It
    was a closure inside `ai_runtime._catalog_with_downloads` and is now shared,
    because a second reader appeared (`routers/ai_benchmark._benchmarkable_models`)
    and wrote its own version — which got it wrong in the one way this function
    exists to prevent, admitting every curated id because `catalog.for_capability`
    is the CURATION and knows nothing about the filesystem. Two answers to this
    question is how a page comes to offer a Run (or a Load) for bytes that are
    not here.

    Two id shapes, and the second is why `model_id in {m.repo_id …}` is not
    enough on its own: `formats.GGUF_RECIPES` keys `llamacpp-text`'s catalog
    entries by the GGUF's own FILENAME (AI-5m), because a repo id cannot address
    one of a repo's several curated quantizations — so a filename id resolves
    through the recipe's `(repo, file)` pair against `CachedModel.files`, the
    snapshot's own top-level filenames.

    `cached` is the already-paid-for `cached_models()` answer; callers making
    several of these in a row should pass it rather than paying the memo lookup
    per id. Either way a PARTLY downloaded repo is already absent from that list
    (D424's `_unfinished_fetch` skip), so "downloaded" here means a fetch that
    finished — which is what keeps a caller from resuming a multi-GB pull.
    """
    models = cached_models() if cached is None else cached
    recipe = formats.GGUF_RECIPES.get(model_id)
    if recipe is None:
        return any(model.repo_id == model_id for model in models)
    return any(model.repo_id == recipe["repo"] and recipe["file"] in model.files
               for model in models)


def _repo(cache_dir: str, dirname: str, kind: str) -> dict:
    repo_dir = os.path.join(cache_dir, dirname)
    scan = _scan_repo(repo_dir)
    meta = _repo_meta(repo_dir)
    repo_id = _repo_id_of(dirname)
    # A repo the USER never chose: the GGUF transformer the diffusers recipe
    # swaps in, the Silero detector the MLX whisper runner filters silence with.
    # Both land in this cache like any model and neither is one, so the engine
    # join is not asked at all — its two answers here would be "no engine" (true,
    # and no explanation of a 2.4GB row) or, if a component ever came in a
    # loadable shape, a Load button for something nothing serves.
    component = formats.component(repo_id)
    reading = (
        _tasks.classify(meta.task)
        if kind == "model" and component is None else _tasks.NOTHING
    )
    engine, capability = (
        _engine(meta, reading) if kind == "model" and component is None
        else (None, None)
    )
    return {
        "id": repo_id,
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
        # The words, and the Hub tag they came from. Both, because the label is
        # what a card prints and the TAG is what a filter, a glossary lookup and
        # a link to the Hub all key on — one string doing both jobs is how the
        # two faces of this page came to spell one concept two ways.
        "task": reading.label,
        "taskTag": reading.tag,
        "taskSource": meta.task_source,
        # **Whether we run this KIND of model, said out loud** (`ai/tasks.py`).
        # Three states, because a null `capability` was three different facts
        # and a card cannot explain a fact it cannot see: "supported",
        # "no-runner" (a task we recognise and do not serve — video generation,
        # speech synthesis, a robot policy) and "unknown" (a tag this build has
        # never heard of). `supportReason` is the sentence for the second, and
        # the honest empty string for the others.
        "support": reading.support,
        "supportReason": reading.reason,
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
        # …or what this is a PART of, when it is not a model at all. Null for
        # everything a user downloaded on purpose. The bytes stay on the page
        # and stay deletable — this page's job includes showing what is eating
        # the disk, and hiding a 2.4GB row would be the opposite of that — but
        # the page files these under their own "Fetched by engines" heading, the
        # card says whose it is, and its Load is disabled with that as the
        # reason (never absent: a control that vanishes teaches nothing). See
        # `runners/formats.COMPONENT_REPOS`, which is where the ids live because
        # they are named inside runner venvs this process cannot import.
        "component": dict(component, id=repo_id) if component else None,
        "revisions": _revisions(repo_dir),
        "refs": _ref_names(repo_dir),
        # A download that never finished (D424) — the fact none of the fields
        # above could state, and the one that decides what the card OFFERS. A
        # partial repo has no engine and no Load, because half a snapshot is not
        # a loadable model; what it has is the rest of the download. Only asked
        # of models: a dataset or a Space in this cache is not something this
        # page can resume.
        "partial": kind == "model" and _unfinished_fetch(repo_dir),
        # Bytes that actually ARRIVED, which is `size` for everything finished and
        # very much less than it mid-fetch, since a part file is preallocated to
        # its full length (D440). Only the cards that draw a FRACTION read this;
        # every figure the page prints is still `size`, because what a folder
        # costs on the disk is the allocated bytes.
        "fetchedBytes": _fetched_bytes(repo_dir, scan.size),
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


def discard_empty_shell(repo_id: str) -> bool:
    """Remove `repo_id`'s cache folder when a stopped fetch left NOTHING in it (D437).

    The state a user hit in the wild: a cancelled download whose folder held one
    40-byte `refs/main` and not a single blob. The listing has to call that
    partial — no snapshot is exactly the evidence `_unfinished_fetch` reads — so
    the page drew a "partly downloaded" card, under Unrecognised (no files, so no
    task, so no capability), offering a resume of a download with nothing to
    resume from. The card has a way out of that now; this stops it being drawn.

    **Two positive conditions, both about emptiness, and no others.** No snapshot
    directory AND no blob of any kind — not even a part file. That is the whole
    of it: a folder in that state cannot resume, cannot load, and cannot tell
    anybody what it was going to be, so the bytes it is protecting do not exist.
    Notably NOT deleted:

    * a folder with part files in it — those bytes are exactly what a resume
      picks up (D275/AI-5i), and throwing them away is the behaviour that
      argument rejected;
    * a folder with a snapshot — some of the model is materialised and readable;
    * anything on the strength of a MISSING marker or of the fetch having failed.
      Emptiness is read off the folder, never inferred from the job's outcome, so
      calling this after a SUCCESSFUL fetch is a no-op rather than a hazard.

    Returns whether anything was removed. Never raises: this runs on a fetch
    thread's way out, and a cache folder it could not tidy is not a reason to
    turn a cancelled download into an error.
    """
    dirname = "models--" + repo_id.replace("/", "--")
    if dirname != os.path.basename(dirname) or ".." in dirname or "\\" in dirname:
        return False
    repo_dir = os.path.join(hub_cache_dir(), dirname)
    try:
        if not os.path.isdir(repo_dir) or os.path.islink(repo_dir):
            return False
        if _snapshot_dirs(os.path.join(repo_dir, "snapshots")):
            return False
        try:
            blobs = list(os.scandir(os.path.join(repo_dir, "blobs")))
        except OSError:
            blobs = []  # no blobs/ at all, which is the emptiest case of all
        if blobs:
            return False
        _delete_repo(repo_dir)
        return True
    except OSError:
        return False


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