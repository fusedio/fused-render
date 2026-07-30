def main(top: int = 8):
    """Largest files in this app's folder — a tiny live-Python demo."""
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    entries = []
    for name in sorted(os.listdir(here)):
        full = os.path.join(here, name)
        if os.path.isfile(full):
            entries.append({"name": name, "size": os.path.getsize(full)})
    entries.sort(key=lambda e: -e["size"])
    return {"entries": entries[:top], "total": len(entries)}
