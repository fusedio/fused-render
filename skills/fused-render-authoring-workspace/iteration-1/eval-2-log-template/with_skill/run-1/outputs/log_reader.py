"""Reader backing log_template.html.

Returns the last `n` lines of a log file without loading the whole file
into memory (reads backward from EOF in chunks), plus a total line count
for files under a size cap.
"""
import os

CHUNK_SIZE = 64 * 1024
# Above this file size, skip the full-file scan used for the exact total
# line count (it would still work, just costs a full read every call).
MAX_EXACT_COUNT_BYTES = 200 * 1024 * 1024


def _count_lines(file: str) -> int:
    count = 0
    with open(file, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            count += chunk.count(b"\n")
    return count


def _tail_lines(file: str, n: int) -> list:
    """Read the last n lines of a (possibly huge) file, seeking from EOF."""
    if n <= 0:
        return []
    with open(file, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        data = b""
        newline_count = 0
        while pos > 0 and newline_count <= n:
            read_size = min(CHUNK_SIZE, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
            newline_count = data.count(b"\n")
        text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-n:] if n < len(lines) else lines


def _level(line: str) -> str:
    """Classify a line for highlighting. ERROR wins over WARN if both present."""
    upper = line.upper()
    if "ERROR" in upper:
        return "error"
    if "WARN" in upper:
        return "warn"
    return ""


def main(file: str, n: int = 200) -> dict:
    n = max(1, int(n))
    size = os.path.getsize(file)
    counted_exact = size <= MAX_EXACT_COUNT_BYTES
    total_lines = _count_lines(file) if counted_exact else None

    tail = _tail_lines(file, n)
    if counted_exact:
        start_line = max(1, total_lines - len(tail) + 1)
    else:
        start_line = None  # unknown absolute line numbers for huge files

    lines = []
    for i, text in enumerate(tail):
        lines.append({
            "lineno": (start_line + i) if start_line is not None else None,
            "text": text,
            "level": _level(text),
        })

    return {
        "file": file,
        "n": n,
        "shown": len(lines),
        "total_lines": total_lines,
        "counted_exact": counted_exact,
        "size_bytes": size,
        "lines": lines,
    }
