"""Reader backing log_template.html. Returns the last n lines of a log file.

Reads from the end of the file in chunks so previewing a multi-GB log does
not require loading the whole thing into memory.
"""
import os


def main(file: str, n: int = 200) -> dict:
    if n <= 0:
        n = 1

    chunk_size = 65536
    with open(file, "rb") as f:
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        pos = file_size
        newline_count = 0
        chunks = []
        # Walk backwards in chunks until we have at least n+1 newlines
        # (n+1 so a leading partial line can be safely dropped) or hit BOF.
        while pos > 0 and newline_count <= n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
        data = b"".join(reversed(chunks))

    text = data.decode("utf-8", errors="replace")
    all_lines = text.splitlines()

    # If we stopped before byte 0, the first line in `data` is almost
    # certainly a partial line continuing from before our read window —
    # drop it so we don't show a truncated line as if it were complete.
    hit_start_of_file = pos == 0
    if not hit_start_of_file and all_lines:
        all_lines = all_lines[1:]

    lines = all_lines[-n:]
    return {
        "lines": lines,
        "count": len(lines),
        "n": n,
        "file_size": file_size,
        "has_more": (not hit_start_of_file) or len(all_lines) > len(lines),
    }
