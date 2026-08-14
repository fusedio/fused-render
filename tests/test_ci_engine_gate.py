"""The `fused-engine` job's zero-skip gate must have a true premise.

That gate greps pytest's summary for "N skipped" over three test files and fails
the job if it finds any, on the stated premise that nothing in those files has
another reason to skip — so a skip can only mean the `[fused]` extra did not take
effect. A file in that list with an ENVIRONMENT-dependent skip breaks the gate
for a reason unrelated to what it polices, and the failure lands on whoever next
touches the engine.

`tests/test_server_env_install.py` skips four tests when `shutil.which("node")`
is None (the install loader's own JS is executed under node), and the job had no
`setup-node`: it passed only because the current `ubuntu-latest` image happens to
ship node. This test makes the dependency declared rather than inherited.
"""
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKFLOW = os.path.join(_ROOT, ".github", "workflows", "test.yml")


def _workflow():
    with open(_WORKFLOW, encoding="utf-8") as f:
        return f.read()


def _fused_engine_job(src):
    """The `fused-engine:` job's text, up to the next top-level job."""
    start = src.index("\n  fused-engine:")
    rest = src[start + 1:]
    m = re.search(r"\n  [a-z][a-z0-9-]*:\n", rest)
    return rest[: m.start()] if m else rest


def _policed_files(job):
    """The test files the zero-skip gate runs."""
    return re.findall(r"tests/test_[a-z_]+\.py", job)


def test_the_zero_skip_gate_provisions_what_its_files_need():
    """Every environment dependency of a policed file must be set up by the job.

    Only node is checked because node is the only non-Python tool any of those
    files reaches for; the point is that the LIST is derived from the files rather
    than remembered, so adding a node-dependent test to a policed file (or
    policing another file that has one) fails here instead of in CI months later.
    """
    src = _workflow()
    job = _fused_engine_job(src)
    files = _policed_files(job)
    assert files, "the zero-skip gate's file list moved; this test is stale"

    needs_node = []
    for rel in files:
        with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
            body = f.read()
        if 'which("node")' in body or "which('node')" in body:
            needs_node.append(rel)

    if needs_node:
        assert "actions/setup-node" in job, (
            f"{needs_node} skip when node is missing, and the zero-skip gate turns "
            "any skip into a job failure — but the job never installs node, so it "
            "passes only while the runner image happens to ship one"
        )


def test_the_gate_still_says_what_it_polices():
    """The premise is a comment, and a comment that has gone false is the bug.

    Pinned so the claim cannot drift back to "nothing in them has another reason
    to skip" while a node-dependent skip sits in one of the files.
    """
    job = _fused_engine_job(_workflow())
    assert "Nothing in them has another reason to skip" not in job, (
        "that claim is false while a policed file skips on `shutil.which(\"node\")`; "
        "say what the job provisions instead"
    )
