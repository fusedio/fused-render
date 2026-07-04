"""Reader backing du.html: finds the largest files under a directory. Stdlib only."""
import os


def _human_size(size: float) -> str:
    n = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def main(dir: str = ".", n: int = 20) -> dict:
    root = os.path.abspath(os.path.expanduser(dir))
    if not os.path.exists(root):
        raise FileNotFoundError(f"No such directory: {root}")
    if not os.path.isdir(root):
        raise NotADirectoryError(f"Not a directory: {root}")

    n = max(1, int(n))
    files = []
    scanned = 0
    skipped = 0

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None, followlinks=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                if os.path.islink(path):
                    skipped += 1
                    continue
                size = os.path.getsize(path)
            except OSError:
                skipped += 1
                continue
            scanned += 1
            files.append((size, path))

    files.sort(key=lambda t: t[0], reverse=True)
    top = files[:n]

    return {
        "root": root,
        "requested_n": n,
        "scanned": scanned,
        "skipped": skipped,
        "total_found": len(files),
        "files": [
            {"rank": i + 1, "path": path, "size": size, "size_human": _human_size(size)}
            for i, (size, path) in enumerate(top)
        ],
    }
