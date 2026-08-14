"""Source-contract tests for the git SCM view's runPython CHANNELS (§33).

`fused.runPython` supersedes by channel, and the default channel is the `.py`
PATH (`static/runtime.js`: `const key = opts.key === undefined ? pyPath : opts.key`).
Superseding is not a cancel — the older call is handed a promise that never
settles ("hang forever so the stale continuation never runs"). That is exactly
right for a stale repaint and fatal for a CONCURRENT read.

This view reads one `log.py` several different ways, so the rule matters here in
a way it did not for the read-only history view that preceded it (which only ever
had one call in flight). The bug these tests exist to prevent shipped once and
cost an afternoon to find, because every symptom pointed away from the cause:

    await Promise.all([
      fused.runPython(READER, { op: "overview" }),   # same channel…
      fused.runPython(READER, { op: "stashes"  }),   # …so this kills the one above
    ])

The stashes call superseded the overview call, the overview promise never
resolved, `Promise.all` never settled, and the page sat on its skeleton forever.
Nothing anywhere reported a problem: **both** HTTP requests were answered `200`,
the reader was correct and fast in isolation, there was no console error, no
rejected promise and no traceback overlay — the view simply never painted. A
DOM-level reproduction of the same script outside the shell renders it fine,
because the deadlock lives in the runtime's channel bookkeeping and not in the
view's logic at all.

So the contract is pinned in the source rather than left to a browser: every
reader call names its own channel. `chan(op)` keys on the op, which keeps the
half of superseding the view WANTS (a newer overview cancels an older overview)
while removing the half that deadlocks it (overview vs stashes).
"""
import os
import re

TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "git", "template.html")


def _source():
    with open(TEMPLATE, encoding="utf-8") as handle:
        return handle.read()


def _run_python_calls(source):
    """Every `fused.runPython(` call site, as (target, argument text).

    Scanned with a brace/paren walk rather than a regex over the whole call:
    the argument lists here span lines and contain nested object literals, so a
    lazy regex would stop at the first `)` inside `{ ... }` and report an
    argument list that is not the one in the file.
    """
    calls = []
    for match in re.finditer(r"fused\.runPython\(", source):
        i = match.end()
        depth, start = 1, i
        while i < len(source) and depth:
            if source[i] in "([{":
                depth += 1
            elif source[i] in ")]}":
                depth -= 1
            i += 1
        args = source[start:i - 1]
        calls.append((args.split(",", 1)[0].strip(), args))
    return calls


def test_the_view_actually_makes_several_reads_of_one_module():
    """The premise. If this ever stops being true the rest is vacuous."""
    calls = _run_python_calls(_source())
    reader_calls = [args for target, args in calls if target == "READER"]
    assert len(reader_calls) >= 2, (
        "expected the view to read log.py several ways; found "
        f"{len(reader_calls)} — has the view been restructured?")


def test_every_reader_call_names_its_own_channel():
    """No reader call may ride the DEFAULT channel.

    The default is the module path, so two of them in flight together is the
    deadlock in this module's docstring. `chan(op)` is the only sanctioned way
    to name one, so that the keys stay derived from the op rather than being
    hand-typed strings that can silently collide.
    """
    offenders = []
    for target, args in _run_python_calls(_source()):
        if target != "READER":
            continue
        if "chan(" not in args:
            offenders.append(" ".join(args.split())[:100])
    assert not offenders, (
        "these fused.runPython(READER, …) calls ride the default channel (the "
        ".py path), so any two of them in flight together deadlock — the older "
        "one is superseded and its promise never settles. Pass chan(\"<op>\"):\n  "
        + "\n  ".join(offenders))


def test_concurrent_reads_are_on_distinct_channels():
    """A `Promise.all` of reader calls must not repeat a channel.

    The guard above proves each call names a channel; this proves the names are
    different where it counts. Two calls both saying `chan("overview")` would
    satisfy the first test and deadlock exactly as before.
    """
    source = _source()
    for block in re.finditer(r"Promise\.all\(\[(.*?)\]\)", source, re.S):
        body = block.group(1)
        if "runPython" not in body:
            continue
        channels = re.findall(r"chan\(\s*\"([^\"]+)\"\s*\)", body)
        calls = body.count("fused.runPython(")
        assert len(channels) == calls, (
            "a Promise.all runs "
            f"{calls} runPython call(s) but names {len(channels)} channel(s); "
            "an unnamed one rides the default channel and deadlocks the rest")
        assert len(set(channels)) == len(channels), (
            f"two concurrent reads share a channel {channels} — the second "
            "supersedes the first and the first never settles")


def test_chan_derives_the_key_from_the_module_and_the_op():
    """`chan` must not collapse to a constant.

    A `chan` that ignored its argument would pass every test above while
    putting every call back on one channel.
    """
    source = _source()
    match = re.search(r"const chan = \(op\) => \(\{ key: ([^}]+) \}\)", source)
    assert match, "chan() is missing or has changed shape"
    body = match.group(1)
    assert "op" in body, f"chan() ignores its op argument: {body}"
    assert "READER" in body, (
        f"chan() does not namespace by module, so a key could collide: {body}")
