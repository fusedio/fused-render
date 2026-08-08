"""Gate for the `claude` template — the ONE chat mode, with the annotation /
app_state machinery (D235).

Deliberately not called "the split view" any more. It is a split for two of its
three target shapes and NOT a split for the third: an ordinary folder gets a
full-width chat with no left pane at all (D239). A gate that opens with the wrong
layout tells the next reader that the layout is what it is gating on, and it is
not — the layout is `paneURL`'s business, and this file's only question is
whether the mode is offered at all.

There is now ONE chat template for both kinds of target (D237 deleted the second,
plain chat template this one used to sit beside), so this gate no longer sorts
folders into "app folder → split chat" and "ordinary folder → the other chat". It
asks a single question of both kinds:

* **A FILE** (every key in the registry's authored-file set — source, config,
  prose, data, image assets) → allowed. This is the file-scoped chat: the left
  pane renders the file in its OWN default template and the annotation tools
  work over that, which is the whole reason this chat replaced the plain
  chat mode on file keys (D235). Nothing more is asked of a file: the
  registry already decided which extensions offer the mode, and a file needs
  neither a workspace nor a repository to be worth talking about.
* **A DIRECTORY** (the universal "/" key) → allowed for ANY directory. The old
  rule here was `<workspace>/<tag>/<project>` or a registered linked app, on the
  reasoning that an ordinary folder's left pane "would have no app entry to
  render" and its chat was the separate plain chat mode.

  The FIRST half of that is false — the plain chat mode is deleted (D237), so
  narrowing here would leave an ordinary folder with no chat at all, which is
  precisely the capability the delete was meant to preserve. That is the whole
  reason the directory branch is wide, and it is the only reason it needs.

  The SECOND half is TRUE, and conceding it is what D239 did: an ordinary folder
  really does have nothing to frame, so it gets no pane. What does not follow is
  that the chat should be hidden. D237 briefly answered the same objection by
  framing `/embed/<dir>` — fused-render's own file browser — and this docstring
  used to cite that pane as the justification for widening the branch. It is
  gone, and citing it now would rest this gate's reason on a `paneURL` branch
  that returns `null`. A folder worth talking to an agent about does not become
  less worth it because there is nothing to render beside the conversation; if
  anything the reverse, since the full width goes to the transcript.

WHY THIS GATE STILL EXISTS. Everything above reduces to "the path exists",
which the shell already knows before it calls a gate — so the gate would be
pure overhead were it not for ONE remaining refusal: a **mount-backed** path.
The bytes under the mounts dir come from a remote over FUSE, and an agent
turned loose there walks and rewrites the tree through the mount, which is the
same reason every peer gate refuses those paths. (The deleted plain chat
template shipped no gate at all and therefore *did* offer a chat over a remote
mount;
narrowing that is deliberate, not an oversight carried forward.) Deleting
`condition.py` outright was the other option and was rejected for that one
question alone — an always-true gate would be worth removing, a gate that still
says no to remote mounts is not.

The file/directory split is `os.path.isdir`, ONE stat — deliberately the same
question `app/condition.py` never has to ask, because that gate is bound to "/"
alone and this one is not.

CRITICAL: this never lists or walks the directory (`os.listdir`,
`os.scandir`, `glob`, recursion) and never resolves symlinks — the gate runs
for every directory the explorer stats, some on remote mounts, and pure path
arithmetic on the already-known path is the only I/O-free answer.
"""


def main(path: str) -> bool:
    import os
    import sys

    try:
        shared = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
        # Guarded insert: _run_condition re-execs this module on every stat.
        if shared not in sys.path:
            sys.path.insert(0, shared)
        try:
            from appenv import is_mount_backed
        except Exception:  # noqa: BLE001 — cannot tell -> refuse (CT-12)
            return False
        if is_mount_backed(path):
            return False

        # A file target is the file-scoped chat: allowed anywhere on disk. The
        # test is `isfile`, an EXISTING regular file — deliberately not
        # `not isdir`, which would also swallow every path that does not exist.
        # One stat, and "cannot tell" keeps reading as "refuse" (CT-12).
        if os.path.isfile(path):
            return True

        # Any directory. `isdir` rather than `not isfile` for the same reason:
        # a path that does not exist must read as "refuse", not as a folder.
        return os.path.isdir(path)
    except Exception:  # noqa: BLE001 — a broken gate must hide, never raise
        return False
