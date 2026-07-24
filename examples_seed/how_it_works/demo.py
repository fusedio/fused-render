def main(peak: float = 30.0, spread: float = 15.0, bars: int = 28):
    """A tiny stand-in for 'real work'. Given a peak position and spread,
    compute a bell curve and hand the numbers back as JSON for the page to draw.
    Move the sliders -> the shape of the curve visibly changes."""
    import math
    import platform

    values = []
    for i in range(bars):
        x = i / (bars - 1) * 100          # x from 0..100 across the bars
        y = math.exp(-((x - peak) ** 2) / (2 * spread ** 2))
        values.append(round(y, 3))

    tallest = max(range(bars), key=lambda i: values[i])
    return {
        "values": values,                 # bar heights 0..1
        "peak_bar": tallest,               # which bar is the tallest
        "peak_x": round(tallest / (bars - 1) * 100),
        "python": platform.python_version(),  # proof this ran in real local Python
    }
