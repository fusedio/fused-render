"""runPython target for the first demo on the Start Here page.

Rolls dice in real local Python. Random every time -- which is the point:
a canned page couldn't do this. Stdlib only.
"""
import platform
import random


def main(count: int = 5) -> dict:
    """Roll `count` six-sided dice and return the faces as JSON."""
    faces = [random.randint(1, 6) for _ in range(count)]
    return {
        "faces": faces,
        "total": sum(faces),
        "python": platform.python_version(),  # proof this ran in real local Python
    }
