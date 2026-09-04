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


def _function_body(source, signature):
    """The `{ ... }` body of a top-level function, by brace-depth walk — the
    same technique `_run_python_calls` uses for argument lists, needed here
    because these bodies span many lines and contain their own nested
    object literals that a lazy regex would stop inside."""
    start = source.index(signature) + len(signature)
    open_brace = source.index("{", start)
    depth, i = 1, open_brace + 1
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[open_brace + 1:i - 1]


def test_publish_never_runs_without_an_explicit_visibility():
    """The radios open unchecked (§ toolbar's Publish-to-GitHub dialog) — no
    default, because a "private" nobody chose is not a decision this app may
    make for someone's code. The button's own `disabled` mirrors that, but a
    disabled attribute is a suggestion a synthetic click can still bypass; the
    real enforcement has to live in the handler that actually calls gh.
    """
    body = _function_body(_source(), "async function ghPublishClick(repo)")
    guard = re.search(r"if\s*\(([^)]*)\)\s*return;", body)
    assert guard, f"ghPublishClick has no early-return guard at all:\n{body[:200]}"
    assert "ghVisibility" in guard.group(1), (
        "ghPublishClick's guard does not mention ghVisibility, so a click "
        f"with no radio picked could still reach the fetch: {guard.group(1)}")
    # And the guard has to run BEFORE the network call, not merely exist
    # somewhere in the function.
    assert body.index(guard.group(0)) < body.index("/api/github/publish")


def test_the_publish_button_only_replaces_the_dead_remote_slot():
    """The toolbar's remote group is one morphing button choosing among five
    mutually exclusive states (§ toolbar). Only the `!remote` branch is new
    here — the four branches for when a remote already exists must still
    read exactly as they did, so this pins the new branch's shape without
    touching (or needing to touch) the others.
    """
    source = _source()
    branch = _function_body(source, "if (!remote)")
    assert "panel-publish" in branch, (
        "the no-remote branch no longer wires up the publish action key")
    assert "Publish to GitHub" in branch, branch
    # The dead end this replaces was a button with nothing behind it; the
    # live version must not reintroduce that by disabling itself.
    assert "disabled: true" not in branch, (
        "the no-remote branch disables its own button — this is the slot "
        "that used to be a dead end and must render usable: " + branch)
    assert "disabled:" not in branch, (
        "the no-remote branch passes a `disabled` option at all, so it is "
        "either always- or conditionally-disabled rather than a plain "
        "enabled action: " + branch)

    # The branches for when a remote DOES exist are untouched: still exactly
    # the four labels this bar already spoke, still switched on the same
    # `behind`/`ahead`/`canPush` reads `!remote` sits beside.
    for label in ("Get latest", "Send", "Publish branch", "Check for updates"):
        assert label in source, (
            f"the existing remote-exists label {label!r} is gone from the "
            "toolbar — an unrelated edit touched a branch this task was "
            "not supposed to change")


def test_the_escape_hatches_never_touch_the_github_api():
    """The two escape hatches (§ publish modal's `ghHatchesNode`) exist for a
    repository this modal's gh-driven flow was never going to reach — one
    already created by hand on github.com, one pointing somewhere that is
    not GitHub at all. Both share one handler, `ghHatchConnectClick(url,
    errorContext)`, which must run only `remote_add` then `push`, the same
    two ops the toolbar's own "Publish branch" button already calls, and
    must never call `ghFetch` or any `/api/github/*` endpoint — that is what
    lets them work even when the status poll reports `gh` missing.
    """
    source = _source()
    signature = "async function ghHatchConnectClick(url, errorContext)"
    body = _function_body(source, signature)
    assert '"remote_add"' in body, (
        f"{signature} never calls the remote_add op:\n{body}")
    assert '"push"' in body, (
        f"{signature} never calls the push op:\n{body}")
    assert "ghFetch" not in body, (
        f"{signature} calls ghFetch — an escape hatch must not touch "
        f"the GitHub API at all:\n{body}")
    assert "/api/github" not in body, (
        f"{signature} references a /api/github endpoint directly:\n{body}")

    # Both hatches (the "already made it on github.com" one and the
    # "connect a different remote" one) must route through this one shared
    # handler rather than reintroducing a duplicate — each with its own
    # errorContext string, since that is the only thing that used to tell
    # the two apart.
    assert source.count("ghHatchConnectClick(") >= 2, (
        "expected at least one definition and one call site for "
        "ghHatchConnectClick, but found fewer — a duplicate hatch handler "
        "may have crept back in")
    assert "Could not connect that repository" in source
    assert "Could not connect that remote" in source


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
