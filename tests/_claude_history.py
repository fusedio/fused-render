"""Builds a throwaway Claude Code file-history store for the tests that read one
(test_file_history.py, test_annotate_revert.py).

Kept in a non-test module so both suites import from a neutral home instead of
one test module reaching into the other's namespace — same reason as
_mount_safe_helpers.py.

The store this fakes is NOT ours; it is Claude Code's, and the helper under test
is a strictly read-only consumer of it. So the one thing this fixture must never
do is point at the real one: every builder takes an explicit root and the
`claude_home` fixture redirects `CLAUDE_CONFIG_DIR` at a tmp_path, because a
test that leaked would be writing into the user's live edit history — the exact
data the feature exists to protect.

Layout reproduced, verified against a real session (see SPEC §33):

    <config>/file-history/<sessionId>/<sha256(abspath)[:16]>@v<N>
    <config>/projects/<cwd with / -> ->/<sessionId>.jsonl

Each `@vN` is a FULL COPY of the file's content at a checkpoint, not a diff, so
a version is written here by just writing the bytes. Version numbers restart per
session, which is why `session=` is a required argument everywhere below: two
sessions holding a `@v2` for the same path is the normal case, not an edge one.
"""
import hashlib
import json
import os

import pytest


def path_hash(abs_path):
    """The store's filename key. Duplicated from the helper under test on
    purpose: a test that imported the derivation it is asserting would pass for
    any derivation at all."""
    return hashlib.sha256(os.path.abspath(abs_path).encode()).hexdigest()[:16]


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Point CLAUDE_CONFIG_DIR at a throwaway dir and return it.

    The dir deliberately starts EMPTY — not even `file-history/` — so the
    default state every test inherits is the "no store at all" degradation path
    rather than a happy one.
    """
    root = tmp_path / "claude-config"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    return root


def write_version(root, session, target, content, mtime=None):
    """Add one checkpoint: `<root>/file-history/<session>/<hash>@v<N>`.

    `mtime` is settable because ORDER in the merged timeline comes from the
    backup file's mtime, never from N (N restarts per session), so any test
    about ordering has to be able to state times explicitly rather than hope
    the filesystem's clock cooperates.
    """
    d = os.path.join(str(root), "file-history", session)
    os.makedirs(d, exist_ok=True)
    version = 1 + max(
        [int(n.split("@v")[1])
         for n in os.listdir(d)
         if n.startswith(path_hash(target) + "@v") and n.split("@v")[1].isdigit()],
        default=0)
    p = os.path.join(d, "%s@v%d" % (path_hash(target), version))
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def write_transcript(root, session, cwd, records):
    """Write the session's `.jsonl` transcript.

    Only ever OPTIONAL enrichment for the helper: real transcripts reach 5 MB+,
    so nothing on the render path may depend on one being present, parseable, or
    small. Tests use it for the one fact the filesystem cannot carry — a
    checkpoint whose `backupFileName` is null, i.e. "the file did not exist
    yet".
    """
    d = os.path.join(str(root), "projects", cwd.replace(os.sep, "-").replace("/", "-"))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, session + ".jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) if isinstance(r, (dict, list)) else str(r))
            fh.write("\n")
    return p


def delta_record(tracking_path, backup_name, version, when,
                 real_parent_dir=""):
    """A `file-history-delta` record. `backup_name=None` renders the null-backup
    shape — the file did not exist at that checkpoint."""
    backup = None if backup_name is None else {
        "backupFileName": backup_name,
        "version": version,
        "backupTime": when,
        "realParentDir": real_parent_dir,
    }
    if backup_name is None:
        backup = {"backupFileName": None, "version": version,
                  "backupTime": when, "realParentDir": real_parent_dir}
    return {
        "type": "file-history-delta",
        "messageId": "m-" + str(version),
        "snapshotMessageId": "s-" + str(version),
        "trackingPath": tracking_path,
        "backup": backup,
        "timestamp": when,
    }
