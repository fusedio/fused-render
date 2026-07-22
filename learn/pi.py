"""Estimate pi by throwing darts — a runPython target. Stdlib only.

Throw n random darts into the unit square and count how many land inside the
quarter circle of radius 1. The fraction inside is pi/4, so pi ~= 4 * inside/n.
The whole point of the demo: a slider changes n, and real local Python throws
the darts and recomputes the estimate every time.
"""


def main(n: int = 200) -> dict:
    import random
    import time

    t0 = time.perf_counter()
    n = max(10, min(int(n), 200000))

    # Fixed seed so the same n always gives the same picture (stable scrubbing).
    rng = random.Random(42)

    inside = 0
    draw_cap = 2500  # only send this many points to the page
    step = max(1, n // draw_cap)
    pts = []
    for i in range(n):
        x = rng.random()
        y = rng.random()
        hit = (x * x + y * y) <= 1.0
        if hit:
            inside += 1
        if i % step == 0 and len(pts) < draw_cap:
            pts.append([round(x, 4), round(y, 4), 1 if hit else 0])

    return {
        "points": pts,  # sampled darts to draw: [x, y, inside]
        "n": n,  # total darts thrown
        "inside": inside,  # how many landed in the circle
        "pi": round(4.0 * inside / n, 5),  # the estimate
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }
