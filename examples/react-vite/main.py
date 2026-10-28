"""The Python half of the example: `fused.runPython("./main.py", {limit})`.

Params arrive from the page as strings; the annotation on `limit` coerces it
to an int. The return value must be JSON-native.
"""


def main(limit: int = 30) -> dict:
    limit = max(2, min(int(limit), 10_000))
    sieve = bytearray([1]) * (limit + 1)
    sieve[0] = sieve[1] = 0
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i :: i] = bytearray(len(sieve[i * i :: i]))
    return {"limit": limit, "primes": [i for i, flag in enumerate(sieve) if flag]}
