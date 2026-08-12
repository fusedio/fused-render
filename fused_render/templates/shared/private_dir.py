"""Adopting and creating private directories under the shared temp root.

Extracted verbatim from claude/agent.py, whose run dirs and annotation
screenshots these rules were written for — the docstrings still speak in
terms of conversations and transcripts, and the threat model they describe
is the general one: a predictable path under a world-writable temp root can
be pre-created by another local account. The map viewer's drag-and-drop
staging dir shares the same rules now.

Not a package: each backend loads this by path (the templates tree is always
staged as one unit, so ../shared/ resolves from any template folder).
"""
import os
import stat


def _within_tree(path: str, root: str) -> bool:
    """Whether `path` is the caller's own root or something under it — i.e. a
    directory the caller is responsible for, rather than the system's temp
    root."""
    root = os.path.abspath(root)
    return os.path.abspath(path) == root or \
        os.path.abspath(path).startswith(root + os.sep)


def require_private(path: str) -> None:
    """Refuse a directory in our tree that we did not make, or that anyone
    else can write to.

    The temp root is world-writable, and our path under it is *predictable* —
    `fused_render_claude-<uid>` names the victim. So another account can
    pre-create it, or `runs` inside it, before we ever run. Adopting that hands
    them the parent of every run dir, and the parent is enough: the sticky bit
    that stops one account renaming another's entry protects OUR entries in
    /tmp, but it is not inherited by a directory THEY created. They can rename
    the 0700 run dir aside the instant after `mkdir` returns and leave a
    world-readable one in its place, and the transcript, the user's message
    and every tool payload get written into it. The 0700 means nothing if the
    thing above it is theirs.

    `lstat`, not `stat`: a symlink is not a directory we own, however good its
    target looks. Raising is the right outcome — an attacker who plants the
    directory can deny us the chat, but a loud failure is not a disclosure.
    """
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise NotADirectoryError(
            "%s is not a directory (a symlink or file is in the way)" % path)
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return  # Windows: no uid model here, and its temp dir is already per-user
    if st.st_uid != geteuid():
        raise PermissionError(
            "%s belongs to uid %d, not %d — refusing to keep this conversation "
            "under a directory another account controls" % (path, st.st_uid, geteuid()))
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError(
            "%s is writable by others (mode %04o) — refusing to keep this "
            "conversation there" % (path, stat.S_IMODE(st.st_mode)))


def private_dir(path: str, tree_root: str) -> None:
    """Create `path` and any missing parents as `rwx------`.

    `tree_root` is the caller's own root under the temp dir (the parent of
    claude's `runs`, the parent of the map viewer's `drops`): every existing
    ancestor from it downwards is vouched for with `require_private` before
    anything is built on it; above it is the temp root, which belongs to the
    system.

    The run tree lives under the shared temp root, and on a typical Linux box
    that means /tmp with a default 0755 for anything created in it — while a
    run dir holds the entire conversation: `out.jsonl` is the transcript,
    `meta.json` the user's message, and `perm/*.req.json` every tool payload
    there is (commands, edited file content, web inputs). None of it should be
    readable by another local account. macOS' per-user temp root happens to
    make the exposure moot there, which is exactly why it cannot be relied on.

    Levels are created one at a time because `os.makedirs` has applied `mode`
    to the leaf only since 3.7. Existing directories are deliberately NOT
    chmod'ed: the chain starts at a directory we do not own (tightening the
    temp root would be a far worse bug than the one being fixed), and the run
    dir underneath — always freshly created here, always 0700 — is the level
    that actually contains the data.

    Parents tolerate losing the race, the leaf does not. Our root and `runs`
    are shared by every run of ours, so two templates starting their first run
    at once both find them missing and both call mkdir — and the loser used to
    abort `_start`, so the user's message simply never sent. Whoever won is
    fine, PROVIDED it is ours (`require_private`). The **leaf** stays an
    exclusive create: it is this run's private 0700 boundary, so finding one
    already there means a run-id collision or somebody else's directory, and
    quietly adopting it is the wrong answer.
    """
    path = os.path.abspath(path)
    missing = []
    head = os.path.dirname(path)
    while head and not os.path.isdir(head):
        head, tail = os.path.split(head)
        if not tail:
            break
        missing.append(tail)
    # `head` is the deepest thing that already exists. Anything from the
    # caller's own root downwards has to be vouched for before we build on it;
    # above that is the temp root, which belongs to the system.
    if _within_tree(head, tree_root):
        require_private(head)
    for tail in reversed(missing):
        head = os.path.join(head, tail)
        try:
            os.mkdir(head, 0o700)
        except FileExistsError:
            # Somebody got here first. A concurrent run of ours is fine; a
            # directory another account planted is not.
            require_private(head)
    os.mkdir(path, 0o700)
