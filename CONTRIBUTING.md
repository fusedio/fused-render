# Contributing to fused-render

For installing and running the released app, see the [README](README.md). This
page covers building from source and the local development loop. The full
design lives in `SPEC.md` / `ARCHITECTURE.md` / `DECISIONS.md`.

## Building from source

A source checkout builds the React shell once before the server starts:

```
cd frontend && npm install && npm run build   # Node 22
```

Or run `scripts/dev.sh` for a watch + server dev loop. Wheels and the DMG build
the shell automatically at package time, so installed users never need Node.

## Running the tests

The suite needs a Python 3.12 venv with the `dev` extra and a built shell
(`create_app()` refuses to start without `fused_render/static/shell-dist/`):

```
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev,bundled,fused]"
cd frontend && npm install && npm run build && cd ..
.venv/bin/python -m pytest -q -n auto          # Python; xdist-safe
cd frontend && npm run typecheck && bun test   # shell: tsc --noEmit, then the bun unit tests
```

CI (`.github/workflows/test.yml`) runs the same three things on every pull
request. `scripts/dev.sh` builds the venv and the shell for you if you would
rather start from the dev loop. One or two files: `pytest -n 0 tests/test_x.py`.

## Real-weights reproducers

One test file loads real model weights and is **skipped by default** — the
packages it needs (`onnxruntime`, `tokenizers`, `torch`, `transformers`) live in
the AI runner venvs, built lazily on first Download, or in no venv here at all,
and are never dependencies of the venv the rest of the suite runs under. A green
`pytest` says nothing about whether it executed, which is why it takes an
explicit opt-in: `FUSED_RENDER_REAL_WEIGHTS=1` turns every skip condition in it
into a hard failure naming what is missing, so a typo'd interpreter path or an
evicted cache entry cannot report "N skipped" and be mistaken for a pass.

It never downloads anything. It is keyed on the snapshots already being in the
ordinary Hub cache; fetch them from the app's AI Models page first.

`tests/test_ai_onnx_embed_real_weights.py` — the **parity gate**, and the
evidence that licensed replacing the torch embedding runner with the ONNX one:
it asserts ≥0.999 cosine between the ONNX runner's vectors and transformers'
own on both towers, plus that the fetched byte total still matches what
`catalog.py` prices the download at (those exports publish eight quantizations
side by side, so a widened `allow_patterns` would quietly turn a 1.5 GB pull into
an 11.42 GB one).

The ONNX-only half — dimensions, semantics, the fetched-bytes gate — runs on an
onnx-embed runner venv. The two parity assertions need **both** engines in one
interpreter, which no runner folder declares and none should, so build that one
by hand:

```
uv venv /tmp/parity
uv pip install --python /tmp/parity/bin/python \
  onnxruntime tokenizers torch transformers pillow
FUSED_RENDER_REAL_WEIGHTS=1 /tmp/parity/bin/python \
  -m pytest tests/test_ai_onnx_embed_real_weights.py
```

## Building the macOS app

Build the macOS app with:

```
bash scripts/build_dmg.sh   # py2app → dist/FusedRender-<version>.dmg
```

Signing and notarization are credential-driven — see
[docs/signing.md](docs/signing.md).
