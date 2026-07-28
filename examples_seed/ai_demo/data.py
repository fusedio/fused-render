"""Synthesized daily sales for the AI analyst demo (sandbox-ai-demo/analyst.html).

Deterministic per `seed` so a bookmarked URL reproduces the exact dataset the
question was asked about. Pure stdlib — no imports outside random/math/datetime.
"""
import math
import random
from datetime import date, timedelta

REGIONS = ["North", "South", "East", "West"]
PRODUCTS = ["Widget", "Gadget", "Doohickey"]


def main(seed: int = 7, days: int = 30):
    days = max(7, min(days, 120))
    rng = random.Random(seed)
    # Per-region/product base rates so aggregates have real structure the
    # model can find (a best region, a declining product, a weekend dip).
    base = {(r, p): rng.uniform(40, 220) for r in REGIONS for p in PRODUCTS}
    trend = {p: rng.uniform(-0.9, 1.1) for p in PRODUCTS}  # units/day drift
    start = date(2026, 6, 1)
    rows = []
    for d in range(days):
        day = start + timedelta(days=d)
        weekend = 0.55 if day.weekday() >= 5 else 1.0
        for r in REGIONS:
            for p in PRODUCTS:
                units = base[(r, p)] * weekend + trend[p] * d
                units *= 1 + 0.12 * math.sin(d / 4.5 + hash(r) % 7)
                units = max(0, round(units + rng.gauss(0, 8)))
                price = {"Widget": 12.5, "Gadget": 34.0, "Doohickey": 8.75}[p]
                rows.append({
                    "date": day.isoformat(),
                    "region": r,
                    "product": p,
                    "units": units,
                    "revenue": round(units * price, 2),
                })

    # Aggregates the page renders directly, and the compact payload the AI
    # prompt carries (full row set would blow the token budget for nothing).
    by_region = {}
    by_product = {}
    by_day = {}
    for row in rows:
        by_region[row["region"]] = round(by_region.get(row["region"], 0) + row["revenue"], 2)
        by_product[row["product"]] = round(by_product.get(row["product"], 0) + row["revenue"], 2)
        by_day[row["date"]] = round(by_day.get(row["date"], 0) + row["revenue"], 2)

    return {
        "days": days,
        "start": start.isoformat(),
        "total_revenue": round(sum(by_day.values()), 2),
        "total_units": sum(r["units"] for r in rows),
        "by_region": by_region,
        "by_product": by_product,
        "by_day": [{"date": k, "revenue": v} for k, v in sorted(by_day.items())],
        "sample_rows": rows[:12],
        "row_count": len(rows),
    }
