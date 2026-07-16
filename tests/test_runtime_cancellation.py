"""The injected runtime carries opt-in stale-request cancellation (SPEC RH-9 / D113).

`fused.runPython(pyPath, params, {key})` supersedes the prior in-flight call on the
same key — so scrubbing a slider through values cancels the requests it moved past
instead of computing and drawing them out of order. `{signal}` composes with it, and
a superseded/aborted call rejects with a benign AbortError the runtime swallows.

These are string-contract checks over the shipped `static/runtime.js`; the end-to-end
behaviour is exercised separately, but this guards the surface from silently regressing
(and keeps the seeded sine example wired to it).
"""
from pathlib import Path

import fused_render

_STATIC = Path(fused_render.__file__).parent / "static"
RUNTIME = (_STATIC / "runtime.js").read_text(encoding="utf-8")
SINE = (
    Path(fused_render.__file__).parent / "examples_seed" / "sine" / "sine.html"
).read_text(encoding="utf-8")


def test_runtime_runpython_takes_opts():
    # runPython grew a third `opts` argument carrying key/signal.
    assert "function runPython(pyPath, params, opts)" in RUNTIME


def test_runtime_has_keyed_channel_supersession():
    assert "inflightByKey" in RUNTIME  # the latest-wins channel registry
    assert "opts.key" in RUNTIME
    assert "prev.abort()" in RUNTIME  # a newer call aborts the stale one
    assert "new AbortController()" in RUNTIME


def test_runtime_composes_author_signal():
    assert "opts.signal" in RUNTIME
    # the caller's signal aborts our controller too
    assert "opts.signal.addEventListener" in RUNTIME


def test_runtime_swallows_abort_error():
    # A superseded/aborted call rejects with AbortError; it must NOT trip the
    # traceback overlay (RH-3/D17) or spam the console.
    assert 'err.name === "AbortError"' in RUNTIME
    assert "event.preventDefault()" in RUNTIME


def test_sine_example_uses_the_key():
    # The canonical slider example demonstrates the primitive.
    assert 'key: "sine"' in SINE
