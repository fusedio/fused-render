"""The CPU embeddings runner on ONNX Runtime (SPEC §40).

Five lines of code and a `pyproject.toml`, and the manifest is the whole of it:
this folder installs plain `onnxruntime`, the CPU build. Its siblings
`onnx_embed_directml/`, `onnx_embed_cuda/` and `onnx_embed_rocm/` declare the
same dependencies at the same versions and differ ONLY in which onnxruntime
distribution they name; the runner itself is `runners/onnx_embed.py`, shared by
all four, because which wheel a user installed is a fact about the hardware they
picked on the Engines tab and never about how a forward pass through a dual
encoder runs.

A folder rather than a flag because `uv sync` runs BARE (`_env_install_worker`)
with cwd set to the runner folder: everything about an environment has to be
expressible in the `pyproject.toml` sitting beside it, so four environments mean
four folders. The supervisor spawns THIS file (`registry.Runner.worker`) on THIS
folder's venv (`registry.Runner.folder`), which is how the variant a user chose
becomes the process that runs.

Nothing here may grow a second line of behaviour. A provider order or a padding
rule that lived in one of these shells would be a difference between variants
that no test can see — the drift `runners/preview.py` documents at length, one
level out, and the same rule `transformers_embed/worker.py` states for the torch
family.
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import onnx_embed  # noqa: E402 - the whole runner; see runners/onnx_embed.py

if __name__ == "__main__":
    onnx_embed.main()
