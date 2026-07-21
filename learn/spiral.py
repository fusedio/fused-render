"""Ulam prime spiral — runPython target. Stdlib only.

Walk the integers 1..n outward on a square spiral; return the grid coordinates
of the ones that are prime, so the page can paint them and the diagonal streaks
appear. The whole point of the demo: a slider changes n, and real local Python
re-sieves and re-walks the spiral every time.
"""


def main(n: int = 8000) -> dict:
    import time
    t0 = time.perf_counter()

    n = max(1, min(int(n), 60000))  # keep it snappy

    # Sieve of Eratosthenes up to n.
    sieve = bytearray([1]) * (n + 1)
    sieve[0] = 0
    if n >= 1:
        sieve[1] = 0
    i = 2
    while i * i <= n:
        if sieve[i]:
            sieve[i * i :: i] = bytearray(len(range(i * i, n + 1, i)))
        i += 1

    # Walk the spiral: East, North, West, South with run lengths 1,1,2,2,3,3,...
    dirs = ((1, 0), (0, 1), (-1, 0), (0, -1))
    x = y = 0
    num = 1
    di = 0
    step = 1
    extent = 0
    primes = []
    if sieve[1] if n >= 1 else 0:
        primes.append([0, 0])
    while num < n:
        for _ in range(2):
            dx, dy = dirs[di % 4]
            for _ in range(step):
                if num >= n:
                    break
                x += dx
                y += dy
                num += 1
                if sieve[num]:
                    primes.append([x, y])
                    a = x if x >= 0 else -x
                    b = y if y >= 0 else -y
                    if a > extent:
                        extent = a
                    if b > extent:
                        extent = b
            di += 1
        step += 1

    return {
        "primes": primes,
        "extent": extent,
        "n": n,
        "n_primes": len(primes),
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }
