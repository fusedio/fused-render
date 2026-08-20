"""Everything the model_card view shows, read from the folder it is given.

Reads only. Nothing here loads weights, imports a machine-learning library, or
touches the network: a 40GB checkpoint is described from its metadata files and
its safetensors HEADERS, which is why opening the view is instant on a model
that would take minutes to load.

Two rules carry over from the AI Models page (SPEC HF-15/HF-19), and for the
same reasons:

* **Reads put the file's atime back.** "Last read" is what the AI Models page
  prunes by, and every file here is reached through a snapshot symlink, so
  inspecting a model would otherwise mark it as used and quietly protect it
  from the next prune. Looking at a thing is not using it.
* **A quantized checkpoint stores several weights per element.** Summing tensor
  shapes counts storage slots; a 4-bit checkpoint packs eight weights into each
  32-bit word. The count is unpacked from the width the config declares, and
  flagged as recovered rather than measured.

Self-contained on purpose: a template is a set of scripts run by the engine, not
part of the package (SPEC PY-15/D166), so it never imports `fused_render`.
"""
import json
import os
from urllib.parse import quote

_KIND_PREFIXES = {"models--": "model", "datasets--": "dataset", "spaces--": "space"}

# Where this repo lives on the Hub. The kind decides the path: a dataset is
# huggingface.co/datasets/<id>, and linking it as huggingface.co/<id> would be a
# 404 dressed up as a link. A folder with no kind is somebody's own checkout and
# has no Hub page to point at — the link is absent rather than guessed.
_HUB_ORIGIN = "https://huggingface.co"
_HUB_PATH = {"model": "", "dataset": "datasets/", "space": "spaces/"}

_PACKED_DTYPES = {"U8", "I8", "U16", "I16", "U32", "I32", "U64", "I64"}
_DTYPE_BITS = {
    "U8": 8, "I8": 8, "F8_E4M3": 8, "F8_E5M2": 8,
    "U16": 16, "I16": 16, "F16": 16, "BF16": 16,
    "U32": 32, "I32": 32, "F32": 32,
    "U64": 64, "I64": 64, "F64": 64,
}

# Config keys worth putting on screen, in the order a person reads them. Models
# disagree about names, so each row lists the spellings that mean the same
# thing and the first one present wins.
_NOTABLE = (
    ("Parameters declared", ("num_parameters",)),
    ("Hidden size", ("hidden_size", "d_model", "n_embd")),
    ("Layers", ("num_hidden_layers", "num_layers", "n_layer")),
    ("Attention heads", ("num_attention_heads", "n_head", "encoder_attention_heads")),
    ("Key/value heads", ("num_key_value_heads",)),
    ("Context length", ("max_position_embeddings", "n_positions", "max_seq_len")),
    ("Vocabulary", ("vocab_size",)),
    ("Torch dtype", ("torch_dtype",)),
)


def _read(path, limit):
    """Up to `limit` bytes, with the atime restored — see the module docstring."""
    try:
        before = os.stat(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit)
    except OSError:
        return None
    try:
        os.utime(path, (before.st_atime, before.st_mtime))
    except OSError:
        pass
    return data


def _read_json(path, limit=8 * 1024 * 1024):
    raw = _read(path, limit)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _front_matter(path):
    """Top-level scalars of a model card's YAML front matter, plus the first
    paragraph of prose after it. Not a YAML parser (a template may not add a
    dependency for two keys) — nested blocks are skipped, and a `tags:` list is
    collected because it is the one list worth showing."""
    raw = _read(path, 128 * 1024)
    if raw is None:
        return {}, [], ""
    # _read is deliberately binary (see its docstring), so a CRLF card — common
    # enough on its own (an author's editor, or a repo checked out with
    # core.autocrlf) — reaches here with the "\r\n" intact. The scalar/tag loop
    # below tolerates a stray "\r" because every value already goes through
    # .strip(), but the paragraph splitter looks for a literal "\n\n" blank
    # line, which a CRLF file never contains (each blank line is "\r\n\r\n", not
    # "\n\n") — so an un-normalized CRLF card silently loses its summary
    # instead of merely misreading a field. Normalize once, up front.
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---"):
        return {}, [], text.strip().split("\n\n")[0].strip()
    lines = text.split("\n")[1:]
    scalars, tags, collecting_tags, end = {}, [], False, len(lines)
    for index, line in enumerate(lines):
        if line.strip() in ("---", "..."):
            end = index
            break
        if collecting_tags:
            stripped = line.strip()
            if stripped.startswith("- "):
                tags.append(stripped[2:].strip().strip("'\""))
                continue
            collecting_tags = False
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip().strip("'\"")
        if key == "tags" and not value:
            collecting_tags = True
            continue
        if value:
            scalars[key] = value
    body = "\n".join(lines[end + 1:]).strip()
    # The first real paragraph — headings and badges are noise on a card.
    summary = ""
    for block in body.split("\n\n"):
        block = block.strip()
        if block and not block.startswith(("#", "<", "[!", "![")):
            summary = block
            break
    return scalars, tags, summary


def _safetensors_header(path):
    head = _read(path, 8)
    if head is None or len(head) < 8:
        return None
    length = int.from_bytes(head, "little")
    if not 0 < length <= 64 * 1024 * 1024:
        return None
    raw = _read(path, 8 + length)
    if raw is None or len(raw) < 8 + length:
        return None
    try:
        header = json.loads(raw[8:])
    except ValueError:
        return None
    return header if isinstance(header, dict) else None


def _quantized_bits(config):
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


def _tensor_rows(header, bits):
    """(params, estimated, dtype counts, per-tensor rows) for one weights file."""
    total, estimated, dtypes, rows = 0, False, {}, []
    for name, info in header.items():
        if name == "__metadata__" or not isinstance(info, dict):
            continue
        shape = info.get("shape")
        if not isinstance(shape, list):
            continue
        count = 1
        for dim in shape:
            if not isinstance(dim, int) or dim < 0:
                count = 0
                break
            count *= dim
        dtype = info.get("dtype") if isinstance(info.get("dtype"), str) else "?"
        if bits and dtype.upper() in _PACKED_DTYPES:
            per_word = _DTYPE_BITS.get(dtype.upper(), 0) // bits
            if per_word > 1:
                count *= per_word
                estimated = True
        dtypes[dtype] = dtypes.get(dtype, 0) + 1
        rows.append({"name": name, "shape": shape, "dtype": dtype, "params": count})
        total += count
    return total, estimated, dtypes, rows


def _resolve_root(path):
    """The directory to describe: a cache repo folder is described through the
    revision `refs/main` points at, since that is the one a load would get."""
    snapshots = os.path.join(path, "snapshots")
    if not os.path.isdir(snapshots):
        return path, None, []
    try:
        commits = sorted(
            entry.name for entry in os.scandir(snapshots) if entry.is_dir(follow_symlinks=False)
        )
    except OSError:
        return path, None, []
    refs = {}
    refs_dir = os.path.join(path, "refs")
    for dirpath, _dirnames, filenames in os.walk(refs_dir):
        for name in filenames:
            raw = _read(os.path.join(dirpath, name), 4096)
            if raw is not None:
                rel = os.path.relpath(dirpath, refs_dir)
                key = name if rel == "." else os.path.join(rel, name).replace(os.sep, "/")
                refs[key] = raw.decode("utf-8", errors="ignore").strip()
    main_commit = refs.get("main")
    current = main_commit if main_commit in commits else (commits[-1] if commits else None)
    revisions = [
        {"commit": commit, "refs": sorted(r for r, c in refs.items() if c == commit),
         "current": commit == current}
        for commit in commits
    ]
    root = os.path.join(snapshots, current) if current else path
    return root, current, revisions


def main(path):
    path = os.path.abspath(path)
    root, revision, revisions = _resolve_root(path)

    # Name and kind come from the CACHE REPO folder, which may be this one or —
    # when someone browses into `…/<repo>/snapshots/<commit>` from the listing —
    # its grandparent. Without that, opening a snapshot shows a commit sha where
    # the model's name belongs, which is the one thing this view must get right.
    folder = os.path.basename(path.rstrip("/\\"))
    parent = os.path.dirname(path.rstrip("/\\"))
    if os.path.basename(parent) == "snapshots":
        repo_folder = os.path.basename(os.path.dirname(parent))
        if repo_folder.startswith(tuple(_KIND_PREFIXES)):
            folder, revision = repo_folder, revision or os.path.basename(path.rstrip("/\\"))
    kind = next((k for prefix, k in _KIND_PREFIXES.items() if folder.startswith(prefix)), None)
    name = "/".join(folder.split("--")[1:]) if kind else folder

    files, weights_files = [], []
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                size = os.stat(full).st_size
            except OSError:
                size = 0
            files.append({"name": rel, "size": size})
            if filename.endswith(".safetensors"):
                weights_files.append((rel, full))
    files.sort(key=lambda f: (-f["size"], f["name"]))

    config = _read_json(os.path.join(root, "config.json")) or {}
    index = _read_json(os.path.join(root, "model_index.json")) or {}
    card, tags, summary = _front_matter(os.path.join(root, "README.md"))
    bits = _quantized_bits(config)

    total_params, estimated, largest, per_file = 0, False, [], []
    seen = set()
    for rel, full in weights_files:
        try:
            key = os.stat(full)
            identity = (key.st_dev, key.st_ino)
        except OSError:
            continue
        if identity in seen:
            continue  # the same blob under a second name — counted once
        seen.add(identity)
        header = _safetensors_header(full)
        if header is None:
            continue
        params, file_estimated, dtypes, rows = _tensor_rows(header, bits)
        total_params += params
        estimated = estimated or file_estimated
        per_file.append({"file": rel, "tensors": len(rows), "params": params, "dtypes": dtypes})
        largest.extend(rows)
    largest.sort(key=lambda row: -row["params"])

    notable = []
    for label, keys in _NOTABLE:
        for key in keys:
            if key in config and not isinstance(config[key], (dict, list)):
                notable.append({"label": label, "value": config[key]})
                break

    return {
        "name": name,
        "kind": kind or "folder",
        # quote(), not an f-string of the raw id: a repo id is user data on its
        # way into a URL, and the canonical encoder is the one that gets every
        # case right rather than the ones we thought of. safe="/" keeps the
        # org/name separator a separator.
        "hubUrl": f"{_HUB_ORIGIN}/{_HUB_PATH[kind]}{quote(name, safe='/')}" if kind else None,
        "root": root.replace(os.sep, "/"),
        "revision": revision,
        "revisions": revisions,
        "card": {
            "pipelineTag": card.get("pipeline_tag"),
            "library": card.get("library_name") or ("diffusers" if index else None),
            "license": card.get("license"),
            "baseModel": card.get("base_model"),
            "tags": tags[:12],
            "summary": summary[:600],
        },
        "config": {
            "architectures": config.get("architectures") if isinstance(config.get("architectures"), list) else [],
            "modelType": config.get("model_type"),
            "pipelineClass": index.get("_class_name"),
            "quantization": f"{bits}-bit" if bits else None,
            "notable": notable,
        },
        # The tokenizer section keys off this, and off `root` above: resolving
        # which revision to describe happens HERE, once, and `tokenize_text.py`
        # is handed the answer rather than working it out again from its own
        # copy of the cache layout.
        "hasTokenizer": os.path.isfile(os.path.join(root, "tokenizer.json")),
        "params": {"total": total_params or None, "estimated": estimated},
        "weights": per_file,
        "largest": largest[:12],
        "files": files[:200],
        "fileCount": len(files),
        "totalSize": sum(f["size"] for f in files),
    }
