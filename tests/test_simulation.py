"""Simulation metrics are recomputed independently here (brute force) and compared to the API."""
from app import dataset
from app.rules import matches
from app.store import SEED_SEGMENTS
from conftest import CONTRADICTION, SPORTS_BROADEN

ACTIVE_SPORTS = SEED_SEGMENTS[0]["versions"][2]


def brute(active, draft):
    data = dataset.requests()
    a = {i for i, r in enumerate(data) if matches(active, r)}
    d = {i for i, r in enumerate(data) if matches(draft, r)}
    return a, d, len(data)


def test_dataset_is_deterministic_and_documented():
    data = dataset.requests()
    assert len(data) == dataset.SIZE == 20_000
    assert data[0]["request_id"] == "req_000000"
    dataset.requests.cache_clear()
    assert dataset.requests() == data  # same seed, same sample
    d = dataset.describe()
    assert d["synthetic"] is True and d["seed"] == 20261122
    assert 0.05 < d["distributions"]["content_category_missing"] < 0.11


def test_metrics_match_brute_force(s):
    assert s.draft(SPORTS_BROADEN).status_code == 201
    r = s.simulate(max_sample_flips=20)
    assert r.status_code == 201
    m = r.json()["metrics"]
    a, d, n = brute(ACTIVE_SPORTS, SPORTS_BROADEN)
    assert m["active_matched"] == len(a) and m["draft_matched"] == len(d)
    assert m["newly_eligible"] == len(d - a) and m["no_longer_eligible"] == len(a - d)
    assert m["unchanged"] == n - len(d - a) - len(a - d)
    assert m["active_match_rate"] == round(len(a) / n, 4)
    assert m["draft_match_rate"] == round(len(d) / n, 4)
    assert m["pp_change"] == round((len(d) / n - len(a) / n) * 100, 2)
    assert m["relative_reach_change"] == round((len(d) - len(a)) / len(a), 4)
    assert m["flip_rate"] == round((len(d ^ a)) / n, 4)


def test_identical_draft_has_no_flips(s):
    s.draft(ACTIVE_SPORTS)
    m = s.simulate().json()["metrics"]
    assert m["newly_eligible"] == m["no_longer_eligible"] == 0
    assert m["pp_change"] == 0 and m["relative_reach_change"] == 0 and m["flip_rate"] == 0


def test_broaden_raises_expansion_warning_and_overlap(s):
    s.draft(SPORTS_BROADEN)
    sim = s.simulate().json()
    codes = {w["code"]: w for w in sim["warnings"]}
    assert codes["REACH_EXPANSION"]["severity"] == "warn"
    ent = next(o for o in sim["overlap"] if o["segment_id"] == "seg_entertainment_all")
    a, d, _ = brute(ACTIVE_SPORTS, SPORTS_BROADEN)
    ent_rule = SEED_SEGMENTS[1]["versions"][-1]
    e = {i for i, r in enumerate(dataset.requests()) if matches(ent_rule, r)}
    assert ent["co_eligible"] == len(d & e)
    assert ent["share_of_draft"] == round(len(d & e) / len(d), 4)
    assert "not cannibalisation" in sim["disclaimer"]


def test_narrowing_raises_reach_drop(s):
    narrow = {"all": [{"attr": "content_category", "op": "in", "values": ["IAB-SPORTS-SOCCER"]},
                      {"attr": "device_type", "op": "eq", "value": "mobile"},
                      {"attr": "country", "op": "in", "values": ["US", "CA"]}]}
    s.draft(narrow)
    sim = s.simulate().json()
    assert sim["metrics"]["no_longer_eligible"] > 0 and sim["metrics"]["newly_eligible"] == 0
    assert "REACH_DROP" in [w["code"] for w in sim["warnings"]]
    assert all(f["direction"] == "no_longer_eligible" for f in sim["sample_flips"])


def test_zero_match_blocks(s):
    s.draft(CONTRADICTION)
    sim = s.simulate().json()
    assert sim["metrics"]["draft_matched"] == 0
    z = [w for w in sim["warnings"] if w["code"] == "ZERO_MATCH"]
    assert z and z[0]["severity"] == "block"
    assert sim["metrics"]["relative_reach_change"] == -1.0


def test_sample_flips_are_explained_and_bounded(s):
    s.draft(SPORTS_BROADEN)
    sim = s.simulate(max_sample_flips=5).json()
    assert 0 < len(sim["sample_flips"]) <= 5
    for f in sim["sample_flips"]:
        assert f["active"]["matched"] != f["draft"]["matched"]
        loser = f["active"] if f["direction"] == "newly_eligible" else f["draft"]
        assert loser["failed"], "the side that did not match must say why"
    assert s.simulate(max_sample_flips=21).status_code == 422


def test_simulation_is_deterministic(s):
    s.draft(SPORTS_BROADEN)
    one, two = s.simulate().json(), s.simulate().json()
    assert one["metrics"] == two["metrics"] and one["sample_flips"] == two["sample_flips"]


def test_zero_denominator_relative_change_is_null():
    from app import simulate
    out = simulate.run(CONTRADICTION, SPORTS_BROADEN, [], 0)
    assert out["metrics"]["active_matched"] == 0 and out["metrics"]["relative_reach_change"] is None
    out2 = simulate.run(None, CONTRADICTION, [("x", 1, SPORTS_BROADEN)], 0)
    assert out2["overlap"][0]["share_of_draft"] is None
