"""The injected runtime cancels stale requests BY DEFAULT (SPEC RH-9 / D114).

`fused.runPython(pyPath, params)` belongs to a latest-wins channel keyed by the
`.py` path: a new call for a file supersedes the prior in-flight call for that same
file, so scrubbing a slider through values cancels the runs it moved past instead of
computing and drawing them out of order. A superseded call's promise never settles
(the stale continuation just stops). `opts.key: null` opts out (fully concurrent),
`opts.key` regroups, and `opts.signal` composes (an own-signal abort rejects with a
benign AbortError the runtime swallows).

These are string-contract checks over the shipped `static/runtime.js`; the end-to-end
behaviour is exercised separately, but this guards the surface from silently regressing
(and keeps the seeded sine example on the zero-config default).
"""
from pathlib import Path

import fused_render

_STATIC = Path(fused_render.__file__).parent / "static"
RUNTIME = (_STATIC / "runtime.js").read_text(encoding="utf-8")
_seed_root = Path(fused_render.__file__).parent / "examples_seed"
if not _seed_root.is_dir():  # dev checkout: seed lives at the repo root
    _seed_root = Path(fused_render.__file__).parent.parent / "examples_seed"
SINE = (_seed_root / "sine" / "sine.html").read_text(encoding="utf-8")


def test_runtime_exposes_local_env_identity():
    # fused.env is the runtime identity: "local" here, "hosted" on a deployed
    # artifact (SPEC RH-10). A page branches on it to skip local-only paths
    # (e.g. a 127.0.0.1 daemon) when served.
    assert 'env: "local"' in RUNTIME


def test_runtime_runpython_takes_opts():
    # runPython carries a third `opts` argument (key/signal).
    assert "function runPython(pyPath, params, opts)" in RUNTIME


def test_runtime_channel_defaults_to_pypath():
    # Default channel key is the .py path (cancellation on by default); an
    # explicit null opts out.
    assert "opts.key === undefined ? pyPath : opts.key" in RUNTIME
    assert "key !== null" in RUNTIME


def test_runtime_supersedes_prior_inflight_call():
    assert "inflightByKey" in RUNTIME  # the latest-wins channel registry
    assert "prev.abort()" in RUNTIME  # a newer call aborts the stale one
    assert "new AbortController()" in RUNTIME


def test_runtime_superseded_call_never_settles():
    # A superseded call is marked and returns a never-settling promise so the
    # stale continuation (even inside a try/catch) simply stops — no error flash.
    assert "_supersededByKey" in RUNTIME
    assert "new Promise(() => {})" in RUNTIME


def test_runtime_composes_author_signal():
    assert "opts.signal" in RUNTIME
    # the caller's signal aborts our controller too
    assert "opts.signal.addEventListener" in RUNTIME


def test_runtime_swallows_own_signal_abort_error():
    # An own-signal abort rejects with AbortError; it must NOT trip the traceback
    # overlay (RH-3/D17) or spam the console.
    assert 'err.name === "AbortError"' in RUNTIME
    assert "event.preventDefault()" in RUNTIME


def test_sine_example_relies_on_the_default():
    # The canonical slider example uses the zero-config default (no opts arg).
    assert 'fused.runPython("./sine.py", { n: "160", freq: String(freq) });' in SINE
    assert "key:" not in SINE  # no explicit key/opts needed anymore
