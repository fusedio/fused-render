"""Example runPython target: computes a sine wave.

Stdlib only — no '# /// script' dependency block needed for zero-dep scripts.
"""
import math

import fused


@fused.udf
def main(n: int = 80, freq: float = 1.0) -> dict:
    print(f"computing {n} points at freq={freq}")
    points = []
    for i in range(n):
        x = i / (n - 1) if n > 1 else 0.0
        y = math.sin(2 * math.pi * freq * x)
        points.append([x, y])
    return {"points": points}
