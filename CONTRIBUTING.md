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

## Real-weights reproducers

Two test files load real model weights and are **skipped by default** — the
packages they need (`torch`, `transformers`, `onnxruntime`, `tokenizers`) live in
the AI runner venvs, built lazily on first Download, and are never dependencies
of the venv the rest of the suite runs under. A green `pytest` says nothing
about whether either one executed, which is why they take an explicit opt-in:
`FUSED_RENDER_REAL_WEIGHTS=1` turns every skip condition in them into a hard
failure naming what is missing, so a typo'd interpreter path or an evicted cache
entry cannot report "N skipped" and be mistaken for a pass.

Neither file ever downloads anything. Both are keyed on the snapshot already
being in the ordinary Hub cache; fetch it from the app's AI Models page first.

`tests/test_ai_transformers_embed_real_weights.py` — the torch embedding
runner's contract against `google/siglip2-base-patch16-384`. Run it on a
transformers-embed runner venv:

```
FUSED_RENDER_REAL_WEIGHTS=1 ~/.fused-render/venvs/<hash>/bin/python \
  -m pytest tests/test_ai_transformers_embed_real_weights.py
```

`tests/test_ai_onnx_embed_real_weights.py` — the **parity gate**, and the
evidence that licensed replacing the torch embedding runner with the ONNX one:
it asserts ≥0.999 cosine between the two engines' vectors on both towers, plus
that the fetched byte total still matches what `catalog.py` prices the download
at (those exports publish eight quantizations side by side, so a widened
`allow_patterns` would quietly turn a 1.5 GB pull into an 11.42 GB one).

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
