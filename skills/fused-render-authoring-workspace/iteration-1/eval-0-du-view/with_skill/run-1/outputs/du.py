def main(path: str = ".", top_n: int = 20):
    import os

    root = os.path.expanduser(path)

    if not os.path.isdir(root):
        return {"error": f"not a directory: {root}", "entries": [], "total_files": 0, "total_size": 0, "path": root}

    entries = []
    total_size = 0
    scan_errors = 0

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                if os.path.islink(full):
                    continue
                size = os.path.getsize(full)
            except OSError:
                scan_errors += 1
                continue
            total_size += size
            entries.append({
                "name": name,
                "path": os.path.relpath(full, root),
                "size": size,
            })

    entries.sort(key=lambda e: -e["size"])

    top_n = max(0, top_n)
    top_entries = entries[:top_n]

    return {
        "path": root,
        "entries": top_entries,
        "total_files": len(entries),
        "total_size": total_size,
        "scan_errors": scan_errors,
    }
