"""Gate for the `claude_split` app template (bound on the universal "/"
directory key).

`main(path)` offers the split app view — project preview beside a Claude
chat — ONLY for a project folder, i.e. a directory exactly two levels below
the workspace root: ~/Documents/Fused/<tag>/<project>. Anywhere else (the
root itself, a tag folder, a nested subfolder, an unrelated directory) the
mode stays hidden.

CRITICAL: this never lists or walks the directory (`os.listdir`,
`os.scandir`, `glob`, recursion) — the gate runs for every directory the
explorer stats, some on remote mounts, and pure path arithmetic on the
already-known path is the only I/O-free answer.
"""
import os


def main(path: str) -> bool:
    root = os.path.realpath(os.path.expanduser("~/Documents/Fused"))
    target = os.path.realpath(path)
    try:
        rel = os.path.relpath(target, root)
    except ValueError:
        # Windows: different drive letters -> not under the root.
        return False
    if rel.startswith(".."):
        return False
    # Exactly <tag>/<project>: two segments, no more, no fewer.
    parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
    return len(parts) == 2
