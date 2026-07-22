# hello.py — the whole contract between a page and Python.
# A function named main(); whatever it returns comes back to the page as JSON.
def main(name: str = "world"):
    import sys

    return {
        "greeting": f"Hello, {name}!",
        "python": ".".join(str(v) for v in sys.version_info[:3]),
    }
