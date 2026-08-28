"""The CUDA image runner (SPEC §40).

Five lines of code and a `pyproject.toml`, and the manifest is the whole of it:
this folder installs PyPI's default torch, which IS the
CUDA build. Its siblings
`diffusers_image/` and `diffusers_image_rocm/` declare the same
dependencies at the same versions and differ ONLY in which index torch comes
from; the runner itself is `runners/torch_image.py`, shared by all three, because
which wheel a user installed is a fact about the hardware they picked on the
Engines tab and never about how a denoising loop runs.

A folder rather than a flag because `uv sync` runs BARE (`_env_install_worker`)
with cwd set to the runner folder: everything about an environment has to be
expressible in the `pyproject.toml` sitting beside it, so three environments mean
three folders. The supervisor spawns THIS file (`registry.Runner.worker`) on THIS
folder's venv (`registry.Runner.folder`), which is how the variant a user chose
becomes the process that runs.

Nothing here may grow a second line of behaviour. A `_placement()` or a dtype
rule that lived in one of these three shells would be a difference between
variants that no test can see — the drift `runners/preview.py` documents at
length, one level out.
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch_image  # noqa: E402 - the whole runner; see runners/torch_image.py

if __name__ == "__main__":
    torch_image.main()
