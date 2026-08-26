"""The deployed runtime must behave as the local one, not merely resemble it.

A page is authored against `static/runtime.js` and then deployed against
`static/workbench_runtime.js`.  Anywhere the two disagree is a bug the author
cannot see until the app is live and someone else is holding it, so the
divergences worth guarding are pinned here — string-contract checks over both
shipped files, the idiom test_runtime_cancellation.py already uses.
"""
from pathlib import Path

import fused_render

_STATIC = Path(fused_render.__file__).parent / "static"
LOCAL = (_STATIC / "runtime.js").read_text(encoding="utf-8")
HOSTED = (_STATIC / "workbench_runtime.js").read_text(encoding="utf-8")


def test_both_runtimes_reject_a_history_option_other_than_replace():
    # `{history: "push"}` is not a quiet no-op in either runtime: the local one
    # throws so a typo cannot silently hand back the push it was meant to
    # avoid, and the hosted one has to do the same or the typo survives the
    # deploy that made it matter.
    for source in (LOCAL, HOSTED):
        assert 'options.history must be "replace"' in source


def test_both_runtimes_reject_a_non_string_param_value():
    for source in (LOCAL, HOSTED):
        assert "must be a string or null" in source


def test_both_runtimes_reject_a_default_on_a_removal():
    for source in (LOCAL, HOSTED):
        assert "options.default is meaningless when removing" in source


def test_both_runtimes_reject_a_non_string_default():
    for source in (LOCAL, HOSTED):
        assert "options.default for" in source


def test_hosted_runtime_keeps_the_once_per_visit_history_push():
    # The local runtime spends exactly one history entry per visit on param
    # writes: the first USER-CAUSED write pushes (so Back restores the
    # as-loaded state), later writes replace on top of it, and a write made
    # before any gesture folds into the entry the user is standing on. A
    # hosted runtime that only ever replaces has no Back at all.
    assert "fusedParamEntry" in HOSTED
    assert "pushState" in HOSTED
    assert "sawGesture" in HOSTED
    for event in ("pointerdown", "keydown"):
        assert event in HOSTED


def test_hosted_runtime_honours_the_default_option():
    # A value equal to its declared default means the URL should say nothing.
    assert "meansDefault" in HOSTED


def test_hosted_run_python_resolves_its_route_before_touching_the_channel():
    # routeFor throws synchronously for an unknown path. Resolving it after the
    # supersede bookkeeping would let a bad path abort a good in-flight request
    # on the same opts.key and strand that key's record in `inflight`.
    resolved = HOSTED.index("routeUrl(routeFor(path))")
    supersede = HOSTED.index("inflight.set(")
    assert resolved < supersede, (
        "runPython must resolve its route before superseding the channel"
    )
