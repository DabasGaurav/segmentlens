"""Active-vs-draft impact simulation over the shared synthetic sample."""
from __future__ import annotations

from functools import lru_cache

from . import dataset
from .rules import attributes_used, canonical, explain, matches

THRESHOLDS = {
    "REACH_DROP": -0.25,        # relative reach change at or below -25%
    "REACH_EXPANSION": 0.50,    # relative reach change at or above +50%
    "HIGH_FLIP_RATE": 0.10,     # >= 10% of sampled requests change decision
    "HIGH_OVERLAP": 0.80,       # >= 80% of draft-matched requests also match another active segment
    "MISSING_DATA_EXPOSURE": 0.10,  # >= 10% of sample lacks an attribute the draft uses
}
DISCLAIMER = ("Synthetic sample. Match-rate changes are not forecasts of impressions, revenue or real reach; "
              "overlap is co-eligibility, not cannibalisation.")


@lru_cache(maxsize=512)
def _matched_indices(rule_json: str) -> frozenset[int]:
    import json
    rule = json.loads(rule_json)
    return frozenset(i for i, r in enumerate(dataset.requests()) if matches(rule, r))


def matched(rule: dict | None) -> frozenset[int]:
    if rule is None:
        return frozenset()
    return _matched_indices(canonical(rule))


def _ratio(num: int, den: int) -> float | None:
    return None if den == 0 else round(num / den, 4)


def run(active_rule: dict | None, draft_rule: dict, others: list[tuple[str, int, dict]],
        max_sample_flips: int) -> dict:
    data = dataset.requests()
    n = len(data)
    a = matched(active_rule)
    d = matched(draft_rule)
    newly = sorted(d - a)
    lost = sorted(a - d)
    active_rate = len(a) / n
    draft_rate = len(d) / n
    rel = _ratio(len(d) - len(a), len(a)) if active_rule is not None else None
    flip_rate = round((len(newly) + len(lost)) / n, 4)
    metrics = {
        "active_matched": len(a),
        "draft_matched": len(d),
        "active_match_rate": round(active_rate, 4),
        "draft_match_rate": round(draft_rate, 4),
        "pp_change": round((draft_rate - active_rate) * 100, 2),
        "relative_reach_change": rel,
        "newly_eligible": len(newly),
        "no_longer_eligible": len(lost),
        "unchanged": n - len(newly) - len(lost),
        "flip_rate": flip_rate,
    }

    overlap = []
    for seg_id, version, rule in others:
        o = matched(rule)
        co = len(d & o)
        overlap.append({"segment_id": seg_id, "active_version": version, "co_eligible": co,
                        "share_of_draft": _ratio(co, len(d))})

    warnings = []
    if len(d) == 0:
        warnings.append(_w("ZERO_MATCH", "block", "Draft matches 0 sampled requests. Check for contradictory "
                           "AND conditions or over-narrow value lists.", "draft_matched", 0, 0))
    if rel is not None and rel <= THRESHOLDS["REACH_DROP"] and len(d) > 0:
        warnings.append(_w("REACH_DROP", "warn", f"Draft reach is {rel:+.1%} vs active (threshold "
                           f"{THRESHOLDS['REACH_DROP']:+.0%}). Deals relying on this segment may under-deliver.",
                           "relative_reach_change", rel, THRESHOLDS["REACH_DROP"]))
    if rel is not None and rel >= THRESHOLDS["REACH_EXPANSION"]:
        warnings.append(_w("REACH_EXPANSION", "warn", f"Draft reach is {rel:+.1%} vs active (threshold "
                           f"{THRESHOLDS['REACH_EXPANSION']:+.0%}). Confirm buyers expect the new inventory.",
                           "relative_reach_change", rel, THRESHOLDS["REACH_EXPANSION"]))
    if active_rule is not None and flip_rate >= THRESHOLDS["HIGH_FLIP_RATE"]:
        warnings.append(_w("HIGH_FLIP_RATE", "warn", f"{flip_rate:.1%} of sampled requests change decision "
                           f"(threshold {THRESHOLDS['HIGH_FLIP_RATE']:.0%}).", "flip_rate", flip_rate,
                           THRESHOLDS["HIGH_FLIP_RATE"]))
    for o in overlap:
        if o["share_of_draft"] is not None and o["share_of_draft"] >= THRESHOLDS["HIGH_OVERLAP"]:
            warnings.append(_w("HIGH_OVERLAP", "warn", f"{o['share_of_draft']:.0%} of draft-matched requests also "
                               f"match {o['segment_id']}. Co-eligibility only: priority and pricing decide whether "
                               "this matters.", "share_of_draft", o["share_of_draft"], THRESHOLDS["HIGH_OVERLAP"]))
    used = attributes_used(draft_rule)
    missing = sum(1 for r in data if any(not r.get(attr) for attr in used))
    miss_rate = round(missing / n, 4)
    if miss_rate >= THRESHOLDS["MISSING_DATA_EXPOSURE"]:
        warnings.append(_w("MISSING_DATA_EXPOSURE", "info", f"{miss_rate:.1%} of sampled requests lack at least one "
                           f"attribute this draft uses ({', '.join(sorted(used))}); they fail closed.",
                           "missing_rate", miss_rate, THRESHOLDS["MISSING_DATA_EXPOSURE"]))

    flips = _pick(newly, lost, max_sample_flips)
    sample = []
    for idx, direction in flips:
        ctx = data[idx]
        am, _, af = explain(active_rule, ctx) if active_rule is not None else (False, [], [])
        dm, _, df = explain(draft_rule, ctx)
        sample.append({"request_id": ctx["request_id"], "direction": direction, "context": dict(ctx),
                       "active": {"matched": am, "failed": af}, "draft": {"matched": dm, "failed": df}})
    return {"metrics": metrics, "overlap": overlap, "warnings": warnings, "sample_flips": sample,
            "disclaimer": DISCLAIMER}


def _w(code, severity, message, metric, value, threshold) -> dict:
    return {"code": code, "severity": severity, "message": message, "metric": metric,
            "value": value, "threshold": threshold}


def _pick(newly: list[int], lost: list[int], k: int) -> list[tuple[int, str]]:
    """Deterministic, evenly spread sample of flips, balanced between directions."""
    if k <= 0:
        return []

    def spread(items: list[int], m: int) -> list[int]:
        if m >= len(items):
            return items
        step = len(items) / m
        return [items[int(i * step)] for i in range(m)]

    k_new = min(len(newly), max(k - len(lost), (k + 1) // 2))
    k_lost = min(len(lost), k - k_new)
    k_new = min(len(newly), k - k_lost)
    picked = [(i, "newly_eligible") for i in spread(newly, k_new)] + \
             [(i, "no_longer_eligible") for i in spread(lost, k_lost)]
    return sorted(picked)
