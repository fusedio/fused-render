"""Tutorial runPython target: the smallest useful main(). Stdlib only."""
import platform


def main(name: str = "world") -> dict:
    print(f"greeting {name}")
    return {
        "greeting": f"Hello, {name}!",
        "python": platform.python_version(),
    }
