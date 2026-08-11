"""What local inference this machine can do, and which folder does it (SPEC §40).

A **runner** is a folder holding a `pyproject.toml` and a `worker.py`. That is the
whole of it — the same shape `templates/zarr_aoi/` uses for its tile daemon, and
for the same reason: the model runs in its OWN interpreter, built from its own
declaration, in its own process.

Three things follow from that, and they are the reasons this is a folder rather
than an import:

* **fused-render's venv never grows torch or mlx.** They are multi-GB and
  platform-specific; a file explorer that could not start without them would be a
  worse file explorer. The runner's `pyproject.toml` is the only place they are
  named, and `envinstall` (PY-18) builds it on first use — the same detached
  `uv sync` with the same progress record and the same verbatim errors that every
  other declaring folder gets. No new install machinery exists for AI.
* **A wedged model cannot take the app down.** OOM, a CUDA fault, a Rust panic
  inside a loader — all of it happens in a process the supervisor can kill.
* **Adding a backend is adding a folder.** MLX today; llama.cpp or transformers
  for Windows tomorrow is a new module here plus a new folder, and nothing else
  in the app has to learn about it.

**Availability is checked, never assumed.** MLX runs on Apple Silicon and nowhere
else, so `available()` answers with a REASON rather than a bool — "needs Apple
Silicon (this is linux/x86_64)" is something a page can show, while a silently
missing capability is something a user files a bug about.

Resolution is by CAPABILITY, not by model: a caller asks for `text-generation`
and gets whichever runner serves it here. A model id never picks the runner,
because the same repo can be servable by two backends and the choice belongs to
the machine, not to the string.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from typing import Callable

# One capability per runner for now. The constants are the vocabulary the whole
# feature speaks — the API's `capability` parameter, the catalog's grouping, and
# the supervisor's one-resident-model-per-capability rule all key off these.
TEXT_GENERATION = "text-generation"
IMAGE_GENERATION = "text-to-image"

RUNNERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runners")


@dataclass(frozen=True)
class Availability:
    """Whether a runner can run here, and — when it cannot — why not in words.

    The reason is user-facing. "needs Apple Silicon (this is linux/x86_64)" tells
    someone what to do with the information; a False does not.
    """

    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class Runner:
    """One backend: a capability, a folder, and a rule about where it runs."""

    code: str
    capability: str
    #: Folder holding `pyproject.toml` (its declaration) and `worker.py` (the
    #: process the supervisor starts). Both are read from here and nowhere else.
    folder: str
    #: What this backend is, in one line, for the page that has to explain a
    #: capability the machine cannot serve.
    label: str
    _available: Callable[[], Availability] = field(repr=False, default=lambda: Availability(True))

    def available(self) -> Availability:
        return self._available()

    @property
    def worker(self) -> str:
        return os.path.join(self.folder, "worker.py")

    @property
    def pyproject(self) -> str:
        return os.path.join(self.folder, "pyproject.toml")


def _apple_silicon() -> Availability:
    """MLX is Metal-only: Apple Silicon, and nothing else — not Intel Macs.

    Checked at call time rather than import time so a test can monkeypatch
    `platform` and get the answer it is asserting about.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin" and machine == "arm64":
        return Availability(True)
    return Availability(
        False,
        f"needs Apple Silicon — MLX runs on Metal only (this is {system.lower()}/{machine})",
    )


def _always() -> Availability:
    """torch + diffusers build wheels for macOS, Linux and Windows. Whether the
    machine is FAST enough is a different question, and not one to refuse on."""
    return Availability(True)


# The table. Ordered, and first-match-wins per capability — so a future
# cross-platform text runner sits after mlx_text and picks up the machines MLX
# turns down.
_RUNNERS: tuple[Runner, ...] = (
    Runner(
        code="mlx-text",
        capability=TEXT_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "mlx_text"),
        label="MLX (Apple Silicon)",
        _available=_apple_silicon,
    ),
    Runner(
        code="diffusers-image",
        capability=IMAGE_GENERATION,
        folder=os.path.join(RUNNERS_DIR, "diffusers_image"),
        label="Diffusers (PyTorch)",
        _available=_always,
    ),
)


def all_runners() -> tuple[Runner, ...]:
    return _RUNNERS


def by_code(code: str) -> Runner | None:
    return next((r for r in _RUNNERS if r.code == code), None)


def for_capability(capability: str) -> Runner | None:
    """The runner that serves `capability` HERE, or None.

    Availability is part of the resolution, not a check the caller does after:
    picking a runner that cannot run and failing later would report "the model
    failed to load" for a machine that was never going to be able to load it.
    """
    for runner in _RUNNERS:
        if runner.capability == capability and runner.available().ok:
            return runner
    return None


def capabilities() -> tuple[str, ...]:
    """Every capability the registry knows, servable here or not — the page
    lists them all so an unavailable one can say why."""
    seen: list[str] = []
    for runner in _RUNNERS:
        if runner.capability not in seen:
            seen.append(runner.capability)
    return tuple(seen)


def describe() -> list[dict]:
    """The registry as the API reports it: what exists, what runs here, and why
    not when it does not."""
    rows = []
    for runner in _RUNNERS:
        status = runner.available()
        rows.append(
            {
                "code": runner.code,
                "capability": runner.capability,
                "label": runner.label,
                "available": status.ok,
                "reason": status.reason or None,
            }
        )
    return rows
