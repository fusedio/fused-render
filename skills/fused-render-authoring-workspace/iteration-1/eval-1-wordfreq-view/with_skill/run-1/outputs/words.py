def main(path: str = "", min_count: int = 1):
    import os
    import re

    if not path:
        return {"error": "No file path given.", "words": [], "total_unique": 0, "total_words": 0}

    if not os.path.isfile(path):
        return {"error": f"File not found: {path}", "words": [], "total_unique": 0, "total_words": 0}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return {"error": f"Could not read file: {e}", "words": [], "total_unique": 0, "total_words": 0}

    tokens = re.findall(r"[A-Za-z']+", text.lower())

    counts = {}
    for tok in tokens:
        counts[tok] = counts.get(tok, 0) + 1

    words = [{"word": w, "count": c} for w, c in counts.items() if c >= min_count]
    words.sort(key=lambda e: (-e["count"], e["word"]))

    return {
        "error": None,
        "words": words,
        "total_unique": len(counts),
        "total_words": len(tokens),
    }
