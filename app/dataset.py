"""Seeded synthetic ad-opportunity sample.

Distributions were fixed before any demo scenario was written and are deliberately plain:
  * country: global-SSP-like skew toward US (weights below), 1% missing
  * device_type: mobile 58 / desktop 32 / tablet 7 / ctv 3, 2% missing
  * placement_type: display 55 / native 30 / video 15; ctv requests are video 90% of the time
  * publisher_id: 40 publishers, Zipf-like traffic share (weight 1/rank^0.8)
  * content_category: each publisher has a primary vertical; a page is in that vertical 70% of
    the time, otherwise a uniformly random other vertical; 60% of pages also carry a tier-2
    sub-topic; 10% carry a second tier-1 vertical; 8% are unclassified (missing)
The same list object is used for every active-vs-draft comparison, so both versions always see
identical traffic. Nothing here is real Media.net data.
"""
from __future__ import annotations

import random
from collections import Counter
from functools import lru_cache

from .taxonomy import COUNTRIES, PUBLISHERS, TIER1

DATASET_ID = "synthetic-v1"
SEED = 20261122
SIZE = 20_000

COUNTRY_W = [0.40, 0.12, 0.10, 0.07, 0.06, 0.05, 0.05, 0.05, 0.04, 0.03, 0.03]
DEVICE = (["mobile", "desktop", "tablet", "ctv"], [0.58, 0.32, 0.07, 0.03])
PLACEMENT = (["display", "native", "video"], [0.55, 0.30, 0.15])
MISSING = {"country": 0.01, "device_type": 0.02, "content_category": 0.08}


@lru_cache(maxsize=1)
def requests() -> tuple[dict, ...]:
    rng = random.Random(SEED)
    verticals = list(TIER1)
    pub_w = [1 / (rank ** 0.8) for rank in range(1, len(PUBLISHERS) + 1)]
    primary = {p: verticals[i % len(verticals)] for i, p in enumerate(PUBLISHERS)}
    out = []
    for i in range(SIZE):
        pub = rng.choices(PUBLISHERS, pub_w)[0]
        ctx: dict = {"request_id": f"req_{i:06d}", "publisher_id": pub}
        if rng.random() >= MISSING["country"]:
            ctx["country"] = rng.choices(COUNTRIES, COUNTRY_W)[0]
        device = rng.choices(*DEVICE)[0] if rng.random() >= MISSING["device_type"] else None
        if device:
            ctx["device_type"] = device
        if device == "ctv":
            ctx["placement_type"] = "video" if rng.random() < 0.9 else rng.choice(["display", "native"])
        else:
            ctx["placement_type"] = rng.choices(*PLACEMENT)[0]
        if rng.random() >= MISSING["content_category"]:
            t1 = primary[pub] if rng.random() < 0.7 else rng.choice([v for v in verticals if v != primary[pub]])
            cats = [t1]
            if rng.random() < 0.6:
                cats.append(rng.choice(TIER1[t1]))
            if rng.random() < 0.1:
                cats.append(rng.choice([v for v in verticals if v != t1]))
            ctx["content_category"] = cats
        out.append(ctx)
    return tuple(out)


@lru_cache(maxsize=1)
def describe() -> dict:
    data = requests()
    n = len(data)

    def share(counter: Counter) -> dict:
        return {k: round(v / n, 4) for k, v in counter.most_common()}

    cat_t1 = Counter()
    for r in data:
        for c in r.get("content_category", []):
            if c in TIER1:
                cat_t1[c] += 1
    return {
        "id": DATASET_ID,
        "seed": SEED,
        "size": n,
        "synthetic": True,
        "distributions": {
            "country": share(Counter(r.get("country", "(missing)") for r in data)),
            "device_type": share(Counter(r.get("device_type", "(missing)") for r in data)),
            "placement_type": share(Counter(r["placement_type"] for r in data)),
            "content_category_tier1_presence": share(cat_t1),
            "content_category_missing": round(sum(1 for r in data if "content_category" not in r) / n, 4),
            "publishers": len(PUBLISHERS),
            "method": "Seeded pseudo-random generation; see README 'Synthetic dataset'. Not real traffic.",
        },
    }
