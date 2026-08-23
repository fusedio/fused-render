# Testing `text-embeddings`

A new capability (`registry.TEXT_EMBEDDINGS`) that turns text into vectors
through a text encoder — bge, e5, nomic-embed, EmbeddingGemma,
Qwen3-Embedding. It is **separate from the existing `embeddings`
capability**, which stays exactly as it was: that one serves dual encoders
(SigLIP/CLIP) and can put an image and a sentence in one space; this one
serves the retrieval models a RAG or search page actually wants. They hold
separate resident-model slots, so you can have one of each loaded at once.

Engines: **llama.cpp** on Windows/Linux CPU, **llama.cpp + Vulkan** on
Windows/Linux GPU, **MLX** on Apple Silicon.

---

## 1. Run the tests

Everything here runs with no model downloads and no runner venvs.

```bash
cd /path/to/wt-text-embeddings
uv venv
uv pip install -e ".[dev]"

./.venv/Scripts/python.exe -m pytest \
  tests/test_ai_text_embed_common.py \
  tests/test_ai_llamacpp_embed_worker.py \
  tests/test_ai_runtime_embed_text.py \
  tests/test_ai_text_embed_retrieval.py -q
```

On POSIX use `./.venv/bin/python` instead.

Expect **~65 passed, 3 skipped**. The three skips are
`test_ai_text_embed_retrieval.py`'s model-backed tests, which need real
weights — section 4 turns them on.

The neighbouring modules this change touches, all of which should be green:

```bash
./.venv/Scripts/python.exe -m pytest \
  tests/test_ai_formats.py tests/test_ai_registry.py tests/test_ai_tasks.py \
  tests/test_ai_runtime.py tests/test_hub_models.py tests/test_ai_runtime_embed.py \
  tests/test_ai_catalog_embeddings.py tests/test_ai_embed_common.py \
  tests/test_ai_benchmark.py tests/test_ai_benchmark_store.py \
  tests/test_ai_benchmark_api.py tests/test_ai_runner_deps.py \
  tests/test_ai_llamacpp_worker.py tests/test_ai_mlx_embed_worker.py \
  tests/test_ai_transformers_embed_worker.py \
  tests/test_supervisor_core.py tests/test_server_ai.py -q
```

And the frontend, which gained three capability-table entries:

```bash
cd frontend && bun test src/apps/ai_models/lib/     # 333 pass
```

> **Do not run the whole `tests/` directory to judge this branch.** Ten tests
> fail on `a014e786` — the branch point — for reasons that have nothing to do
> with it, so a full run is noise. Baselined by stashing onto the pristine
> tree:
>
> * `tests/test_ai_models_api.py` — 4 failures, and `tests/test_ai_worker_base.py`
>   — 5 failures: all `OSError: WinError 1314`, the fixtures create symlinks and
>   Windows refuses without Developer Mode.
> * `tests/test_ai_metrics.py::test_a_missing_claude_binary_is_counted` — 1
>   failure, expects `ai_unavailable` and gets `ai_error`.
>
> None of the ten is in a module this change edits.

---

## 2. Run the server from source

The installed desktop app is far behind — you must run from the worktree.
Do **not** try to reuse another checkout's `.venv` with `PYTHONPATH`: the
editable install uses a PEP 660 meta-path finder that beats `PYTHONPATH`, so
you silently get the other checkout's code.

```bash
cd /path/to/wt-text-embeddings
uv pip install -e ".[fused]"     # the [fused] extra pins fused==2.9.3b7;
                                 # without it NO model can load

# The React shell must exist, or create_app raises at startup:
(cd frontend && npm install && npm run build)

FUSED_RENDER_CORE_TEMPLATES="$PWD/fused_render/templates" \
  ./.venv/Scripts/python.exe -c \
  "import sys; sys.argv=['fused-render','--port','1789','--no-browser']; from fused_render.cli import main; main()"
```

`FUSED_RENDER_CORE_TEMPLATES` short-circuits template staging so in-repo
templates are read live — without it the server fails on a missing `vendor`
directory. Port 1777 is the desktop app; 1788 is often taken.

### The capability should now be visible

```bash
curl -s http://127.0.0.1:1789/api/ai/runtime -H 'X-Fused: 1' \
  | python -c "import json,sys; [print(r['code'], r['capability'], r['available'], r['active']) for r in json.load(sys.stdin)['runners'] if r['capability']=='text-embeddings']"
```

Expected on Windows/Linux x86_64:

```
mlx-text-embed          text-embeddings False False
llamacpp-embed          text-embeddings True  True
llamacpp-embed-vulkan   text-embeddings True  False
```

And the catalog, smallest first, default first:

```bash
curl -s http://127.0.0.1:1789/api/ai/catalog -H 'X-Fused: 1' \
  | python -c "import json,sys; [print(r['default'], [m['id'] for m in r['models']]) for r in json.load(sys.stdin) if r['capability']=='text-embeddings']"
```

---

## 3. Exercise the endpoint

### The refusals — these need no model and no network

```bash
E=http://127.0.0.1:1789/api/ai/embed-text
J='-H Content-Type:application/json -H X-Fused:1'

# 403 — the guard every mutating POST carries
curl -s -o /dev/null -w '%{http_code}\n' -X POST $E \
  -H 'Content-Type: application/json' -d '{"texts":["a"]}'

# 400 — images are meaningless here, and the message says where to go instead
curl -s -X POST $E $J -d '{"texts":["a"],"paths":["/photos/cat.png"]}'

# 400 — an unrecognised `kind` is refused, never silently defaulted
curl -s -X POST $E $J -d '{"texts":["a"],"kind":"queries"}'
```

### The cold-model fork

```bash
curl -s -X POST $E $J -d '{"texts":["hello world"]}'
```

```json
{"ok": false, "error": {"type": "model_loading",
 "message": "nomic-embed-text-v1.5.Q8_0.gguf is loading now",
 "jobId": "sys:ai-model:nomic-embed-text-v1.5.Q8_0.gguf"}}
```

**That 409 is the contract, not a failure** — the same fork `/api/ai` and
`/api/ai/embed` take. Watch the job, then ask again:

```bash
curl -s http://127.0.0.1:1789/api/jobs -H 'X-Fused: 1'
```

The first load builds the `llamacpp_embed` runner venv (a ~25MB
`llama-cpp-python` wheel from the maintainer's index) and then fetches the one
146MB GGUF. Once the row reads `done`, repeat the call:

```json
{"ok": true, "result": {"vectors": [[...]], "dim": 768,
 "model": "nomic-embed-text-v1.5.Q8_0.gguf",
 "kind": "document", "promptScheme": "nomic"}}
```

### Refusing a repo that is not a text embedding model

This is the behaviour the existing `embeddings` capability gets wrong, and
the thing most worth checking by hand:

```bash
curl -s -X POST $E $J \
  -d '{"texts":["a"],"model":"sentence-transformers/all-MiniLM-L6-v2"}'
# 409 model_loading, then watch the job — it fails with a written sentence
# naming the repo, and no weights are fetched to reach that answer.

curl -s http://127.0.0.1:1789/api/jobs -H 'X-Fused: 1'
```

The job's error should read:

> `'sentence-transformers/all-MiniLM-L6-v2'` publishes no GGUF file this
> engine can load (… file(s) checked). It looks like a safetensors/PyTorch
> checkpoint — the format sentence-transformers and transformers read, which
> llama.cpp cannot open at all. Pass a GGUF conversion of the same model
> instead …

**Watch your network monitor while this runs.** The whole answer comes from
`list_repo_files`; nothing is downloaded. A GGUF repo that turns out to hold a
*chat* model is caught one step later, by a 2MB HTTP `Range` request against
the file's header — still before any weight moves. Try:

```bash
curl -s -X POST $E $J -d '{"texts":["a"],"model":"unsloth/Qwen3.5-4B-GGUF"}'
```

…which should refuse with "its header declares no pooling type" and point at
text generation.

### Query/document asymmetry

Retrieval models are asymmetric — e5 wants `query:`/`passage:`, bge instructs
the query only, Qwen3-Embedding ships a named instruct prompt. `kind` picks
the side:

```bash
curl -s -X POST $E $J -d '{"texts":["leaky roof"],"kind":"query"}'
curl -s -X POST $E $J -d '{"texts":["leaky roof"],"kind":"document"}'
```

The two return **different vectors** for the same string. `promptScheme` on
the reply says which convention was applied — a curated table for the ids this
app ships, a filename heuristic for anything else, so it is reported rather
than applied out of sight.

**Default is `"document"`.** The reasoning is in
`runners/text_embed_common.DEFAULT_KIND`: it is the side that keeps a corpus
internally consistent for someone who has not read about prompt schemes, and
on the bge family it means no prefix at all.

---

## 4. The demo page

```bash
# with the server from section 2 running
open http://127.0.0.1:1789/view?path=$PWD/docs/text-embeddings-demo.html
```

Or open `docs/text-embeddings-demo.html` in the FusedRender app.

A five-passage corpus and a query box. It embeds the corpus with
`kind:"document"`, the query with `kind:"query"`, ranks by a plain dot product
(legal only because vectors are unit-normalized — the page prints `v·v` as a
check), and handles the `model_loading` 409 with `fused.watchJob`, so the
first search on a cold model shows the real download.

The default query — *"keeping the bread culture alive"* — shares **not one
word** with the sourdough passage it should find. That is the point: word
overlap gets 2/5 on this corpus, a real encoder gets 5/5.

---

## 5. Turning on the model-backed retrieval tests

They skip unless the weights are already cached — deliberately, since a test
suite that fetches 146MB on first run is a suite people disable.

```bash
# in the runner venv, or any venv with llama-cpp-python + huggingface_hub
python -c "from huggingface_hub import hf_hub_download; \
  hf_hub_download('nomic-ai/nomic-embed-text-v1.5-GGUF','nomic-embed-text-v1.5.Q8_0.gguf')"

./.venv/Scripts/python.exe -m pytest tests/test_ai_text_embed_retrieval.py -q -rs
```

Point them at another curated model with
`FUSED_TEST_EMBED_REPO` / `FUSED_TEST_EMBED_FILE`.

---

## 6. Apple Silicon

`mlx-text-embed` was **written against the real `mlx-embeddings` 0.1.0 API but
never executed** — see the honesty note at the top of
`runners/mlx_text_embed/worker.py`, which names the three seams to check
first. The package genuinely ships text encoders (`bert.py`, `modernbert.py`,
`xlm_roberta.py`, `qwen3.py`, `gemma3_text.py`, `lfm2.py`,
`llama_bidirec.py` — read out of the published wheel), so this is an
implementation and not a scaffold, but it needs one run on a Mac.

```bash
# on Apple Silicon, from section 2's server
curl -s http://127.0.0.1:1789/api/ai/runtime -H 'X-Fused: 1' | grep mlx-text-embed
# mlx-text-embed should be available AND active — it outranks the llama.cpp
# rows on a Mac, exactly as mlx-text does for chat.

curl -s -X POST $E $J -d '{"texts":["hello world"]}'
# → loads mlx-community/multilingual-e5-small-mlx (253MB), 384-dim,
#   promptScheme "e5"
```

Note the MLX catalog is **different repos** from the llama.cpp one — this
engine reads safetensors, those read GGUF — so vectors from the two engines
are not comparable. That is the same relationship `mlx-text` and
`llamacpp-text` already have, and unlike the `embeddings` capability, whose
two runners do share a vector space.
