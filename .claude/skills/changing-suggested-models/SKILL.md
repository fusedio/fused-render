---
name: changing-suggested-models
description: Use when adding, removing, or swapping a model in one of the curated suggested-model lists in fused_render/ai/catalog.py — the AI Models page's shortlists. Covers the two id shapes (repo id vs. GGUF filename), publishing the new model to our own mirror, and verifying the mirror actually holds it before shipping.
---

# Changing Suggested Models

## Overview

`fused_render/ai/catalog.py`'s `SUGGESTIONS` table is the curated list the AI Models page shows per runner. **Editing that table is only half the change.** The other half is `scripts/build_model_mirror.py`, which publishes the model to our own CloudFront/S3 mirror (SPEC AI-5l/AI-5m). Skip it and the model still works — every download falls back to huggingface.co — but silently: the whole point of the mirror is that CloudFront's access logs are the only telemetry this app has for "did a user download a model" (D419), and a suggested model with no mirror objects is invisible to those logs forever, with nothing on screen to say so.

**The invariant that matters:** every id in `catalog.all_suggested_ids()` must have a manifest on the mirror once `FUSED_MODEL_MIRROR` is set for a build. `scripts/build_model_mirror.py --check <base-url>` is what proves that, and it is meant to gate a release the same way `making-a-release`'s steps gate a version bump.

## Step 1 — find the row, and get its id shape right

The curated lists live in `fused_render/ai/catalog.py`'s `SUGGESTIONS` dict, keyed by runner code (`mlx-text`, `llamacpp-text`, `diffusers-image`, `mflux-image`, `mlx-whisper`, `faster-whisper`). Every list is sorted **smallest `size_gb` first** — position 0 is also `default_for()`'s answer — so insert the new row at the size-sorted position, not at the end.

Read the id before writing anything else. There are exactly two shapes:

- **Every runner except `llamacpp-text`**: the `id` field is a plain Hub repo id, `org/name` (e.g. `mlx-community/Qwen3.5-4B-OptiQ-4bit`).
- **`llamacpp-text`**: the `id` field is a bare `.gguf` **filename** (e.g. `Qwen3.5-4B-Q4_K_M.gguf`), not a repo id. One GGUF repo publishes dozens of quantizations, so the page keys the curation by file, and the filename maps to a repo through `fused_render/ai/runners/formats.py`'s `GGUF_RECIPES`:

  ```python
  GGUF_RECIPES = {
      "Qwen3.5-4B-Q4_K_M.gguf": {
          "repo": "unsloth/Qwen3.5-4B-GGUF",
          "file": "Qwen3.5-4B-Q4_K_M.gguf",
      },
      ...
  }
  ```

  Adding a new `llamacpp-text` row means adding BOTH the `SUGGESTIONS` entry (keyed by filename) and a `GGUF_RECIPES` row (mapping that filename to its repo). Miss the `GGUF_RECIPES` row and every step below still "succeeds" against the wrong thing, or fails opaquely — the id has no recipe, so the publish and mirror-permission machinery cannot resolve a repo for it at all.

## Step 2 — why that distinction matters twice over

It is not just bookkeeping. The filename-vs-repo-id split decides two separate things, and getting it wrong breaks each differently:

1. **Whole-repo vs. per-file publish (AI-5l vs. AI-5m).** A GGUF repo can be 147.81GB for a 2.6GB quantization (`unsloth/Qwen3.5-9B-GGUF`). Publishing a `llamacpp-text` model the whole-repo way means holding and uploading the entire repo; the per-file mode (`--file`, below) publishes exactly the one file. Getting the shape wrong here is a cost problem, not a silent one — the build script needs 60x the disk and bandwidth it should.

2. **The mirror permission (`catalog.mirror_id`).** The runtime permission env var `FUSED_MODEL_MIRROR_OK` carries a repo id, and `mirror.allowed()` checks it against `_REPO_ID` (`org/name`). But `llama_text.download` names the recipe's REPO to the mirror, not the catalog's filename id. If the supervisor handed the worker the filename instead of the translated repo id, `mirror.allowed()` would refuse it — forever, silently, because a refused mirror probe looks exactly like an ordinary Hub download. **This already happened once**: `catalog.mirror_id()` exists specifically because an earlier version of this hook handed the untranslated filename to `FUSED_MODEL_MIRROR_OK`, which made the whole mirror path dead code for every real `llamacpp-text` model in production while the unit tests — which asserted equality with the catalog id, not with what the client accepts — stayed green (D420). If you are adding a new capability that reads `SUGGESTIONS` and might one day feed an id to `mirror.allowed()`/`FUSED_MODEL_MIRROR_OK` directly, route it through `catalog.mirror_id(model_id)` first, not the raw catalog id.

## Step 3 — publish to the mirror

The publisher is `scripts/build_model_mirror.py`. It reads a real Hugging Face hub cache directory and never a hand-written JSON file — see Step 4 for why. Typical flow for a new **non-GGUF** suggestion (an MLX/CTranslate2/Diffusers repo id):

```bash
python scripts/build_model_mirror.py --fetch-missing --model org/name
# review the printed plan (dry run by default), then:
python scripts/build_model_mirror.py --upload s3://<bucket>/<prefix> --model org/name
```

For a **GGUF** (`llamacpp-text`) suggestion, publish the one file, not the repo — pass `--file`, and note the repo comes from `GGUF_RECIPES`, not from the catalog id:

```bash
python scripts/build_model_mirror.py --fetch-missing \
    --model unsloth/Qwen3.5-4B-GGUF --file Qwen3.5-4B-Q4_K_M.gguf
python scripts/build_model_mirror.py --upload s3://<bucket>/<prefix> \
    --model unsloth/Qwen3.5-4B-GGUF --file Qwen3.5-4B-Q4_K_M.gguf
```

With no `--model` at all, the script defaults to every id in `catalog.all_suggested_ids()`, correctly expanded into `(repo, file)` targets for the GGUF ones — this is the invocation a release should run to publish everything the catalog currently promises:

```bash
python scripts/build_model_mirror.py --fetch-missing --upload s3://<bucket>/<prefix>
```

Every model missing from the local hf cache is `SKIPPED` and the run exits non-zero — by design, so a partial publish is never silently green.

## Step 4 — why the publisher reads a real cache, not hand-written JSON

`build_model_mirror.py`'s manifest is generated FROM `~/.cache/huggingface/hub` (or `--cache`): the commit comes from `refs/main`, the etag is the blob's own filename, and the size/sha256 come from the blob bytes. This is not a style preference — a transcribed etag, commit, or file layout is the one class of bug in this whole feature that fails **silently and permanently**: the client would download real bytes, file them under a name hf's own cache layout never produces, and every later load would miss the cache and re-download forever while the download itself reported success. Reading it out of a cache hf produced makes all three fields correct by construction instead of by care.

## Step 5 — verify before you ship

Two checks, and do both:

1. **The drift check** (`--check`, added for this purpose):

   ```bash
   python scripts/build_model_mirror.py --check https://render.fused.io/mirror
   ```

   This fetches every suggested target's manifest over HTTP and validates it through `fused_render/ai/runners/mirror.py`'s own reader — the same code path the app's download logic runs — rather than re-implementing the schema check. It prints one line per target (published, with file count and size, or `MISSING`) and exits non-zero if anything suggested is missing. Read-only: no upload, no `aws` CLI, no `huggingface_hub`. Run it after every publish and before every release that might ship a catalog change.

2. **A live spot check**, for the one thing `--check` cannot fully prove (that the OBJECT BODY is right, not just that it validates):

   ```bash
   curl -i https://render.fused.io/mirror/models/<org>/<name>/manifest.json
   # expect: 200, and the JSON body has "complete": true (repo mode)
   curl -i -H "Range: bytes=0-99" \
       https://render.fused.io/mirror/models/<org>/<name>/<commit>/<etag>
   # expect: 206 Partial Content — proves the CDN answers Range requests,
   # which is the whole transport `_segmented_fetch` depends on
   ```

## Removal

Taking a model OUT of `SUGGESTIONS` (or off `GGUF_RECIPES`) is safe to do without touching the bucket:

- **Safe to leave in S3 forever:** the immutable blobs (`<commit>/<etag>`) — they are content-addressed and harmless clutter at worst, and another model may share one.
- **Should be cleaned up, but not urgently:** the model's `manifest.json` (or `files/<filename>/manifest.json`). It just becomes an orphaned object nothing points to from the app; leaving it does not affect anyone, since nothing reaches it without the catalog id.
- **Must happen together:** removing a `llamacpp-text` id from `SUGGESTIONS` without removing its `GGUF_RECIPES` row (or vice versa) leaves one table pointing at a model the other no longer curates — re-add both or remove both.

## Common mistakes

- Editing `SUGGESTIONS` and shipping without ever running `build_model_mirror.py`. The model works (Hub fallback), but it is invisible to the download logs the mirror exists to produce, and nothing tells you that happened.
- Adding a `llamacpp-text` row without a matching `GGUF_RECIPES` entry, or with one that names the wrong repo.
- Publishing a GGUF suggestion the whole-repo way (omitting `--file`) — technically works, at 10-100x the necessary upload.
- Hand-writing or copy-pasting a manifest JSON instead of generating it from a real cache — looks identical to a real manifest and fails only later, permanently, and silently (Step 4).
- Trusting that unit tests asserting `mirror_id(id) == id` prove the permission wiring is correct. They don't, if `id` itself is untranslated — assert against what `GGUF_RECIPES` says the repo is, the same trap D420 records.
- Skipping `--check` (or the live spot check) before a release that touches `SUGGESTIONS` — the exit code exists specifically because "published 19 of 20" is easy to miss by eye in a log.
