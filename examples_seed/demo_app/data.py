def main(year: int = 2024):
    """Tiny REST endpoint demo — GET /call/<app dir>?route=api/data&year=2025."""
    return {"year": year, "next_year": year + 1, "app": "demo-app"}
