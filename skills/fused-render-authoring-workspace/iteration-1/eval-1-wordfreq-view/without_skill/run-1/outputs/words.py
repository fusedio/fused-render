"""runPython target backing words.html: word-frequency count for a text file.

Stdlib only. Words are matched case-insensitively as runs of letters/apostrophes
(e.g. "don't" counts as one word). `min_count` filters the returned rows but
does not affect `total_words` / `unique_words`, which describe the whole file.
"""
import os
import re

WORD_RE = re.compile(r"[A-Za-z']+")

# Guard against accidentally pointing this at a huge file (e.g. a log or a
# binary misidentified as text) and hanging the worker process.
MAX_BYTES = 20 * 1024 * 1024


def main(file: str, min_count: int = 1) -> dict:
    if not file:
        raise ValueError("'file' is required: an absolute path to a text file")
    if not os.path.isfile(file):
        raise FileNotFoundError(f"no such file: {file}")

    size = os.path.getsize(file)
    if size > MAX_BYTES:
        raise ValueError(
            f"file is too large to analyze ({size:,} bytes, limit {MAX_BYTES:,})"
        )

    counts: dict[str, int] = {}
    with open(file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            for word in WORD_RE.findall(line.lower()):
                counts[word] = counts.get(word, 0) + 1

    total_words = sum(counts.values())
    unique_words = len(counts)

    rows = [
        {"word": word, "count": count}
        for word, count in counts.items()
        if count >= min_count
    ]
    rows.sort(key=lambda r: (-r["count"], r["word"]))

    return {
        "rows": rows,
        "total_words": total_words,
        "unique_words": unique_words,
        "shown_words": len(rows),
    }
