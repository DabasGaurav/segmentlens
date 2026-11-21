from app.rules import describe, explain, matches, reason_codes, rule_hash, validate_rule


def codes(rule):
    return [e["code"] for e in validate_rule(rule).errors]


# ---------------------------------------------------------------- validation
def test_valid_nested_rule():
    rule = {"all": [{"attr": "country", "op": "in", "values": ["US"]},
                    {"any": [{"attr": "device_type", "op": "eq", "value": "mobile"},
                             {"attr": "content_category", "op": "not_in", "values": ["IAB-NEWS"]}]}]}
    res = validate_rule(rule)
    assert res.valid and res.leaf_count == 3 and res.max_depth == 3


def test_unknown_attribute_rejected():
    assert codes({"attr": "user_age", "op": "eq", "value": "18-24"}) == ["UNKNOWN_ATTRIBUTE"]


def test_unknown_operator_rejected():
    assert codes({"attr": "country", "op": "regex", "value": ".*"}) == ["UNSUPPORTED_OPERATOR"]


def test_unknown_value_rejected():
    assert codes({"attr": "country", "op": "eq", "value": "ZZ"}) == ["UNKNOWN_VALUE"]


def test_wrong_types_rejected():
    assert codes({"attr": "country", "op": "in", "values": "US"}) == ["INVALID_TYPE"]
    assert codes({"attr": "country", "op": "eq", "value": 5}) == ["INVALID_TYPE"]
    assert codes({"attr": "country", "op": "in", "values": [1]}) == ["INVALID_TYPE"]
    assert codes(["not", "an", "object"]) == ["INVALID_NODE"]


def test_operand_shape_enforced():
    assert codes({"attr": "country", "op": "eq", "values": ["US"]}) == ["INVALID_OPERAND"]
    assert codes({"attr": "country", "op": "in", "value": "US"}) == ["INVALID_OPERAND"]


def test_duplicate_and_extra_fields():
    assert codes({"attr": "country", "op": "in", "values": ["US", "US"]}) == ["DUPLICATE_VALUE"]
    assert codes({"attr": "country", "op": "eq", "value": "US", "x": 1}) == ["UNKNOWN_FIELD"]
    assert codes({"all": [{"attr": "country", "op": "eq", "value": "US"}], "any": []}) == ["INVALID_NODE"]


def test_empty_group_rejected():
    assert codes({"all": []}) == ["INVALID_GROUP"]


def test_max_depth():
    leaf = {"attr": "country", "op": "eq", "value": "US"}
    ok = {"all": [{"any": [{"all": [leaf]}]}]}           # depth 4
    bad = {"all": [{"any": [{"all": [{"any": [leaf]}]}]}]}  # depth 5
    assert validate_rule(ok).valid
    assert "MAX_DEPTH_EXCEEDED" in codes(bad)


def test_max_values_and_leaves():
    assert codes({"attr": "publisher_id", "op": "in", "values": [f"pub_{i}" for i in range(101, 141)]}) == []
    too_many = {"attr": "publisher_id", "op": "in", "values": [f"v{i}" for i in range(51)]}
    assert codes(too_many) == ["TOO_MANY_VALUES"]
    leaves = {"all": [{"attr": "country", "op": "eq", "value": "US"}] * 21}
    assert "TOO_MANY_CONDITIONS" in codes(leaves)


def test_error_list_is_bounded():
    rule = {"all": [{"attr": f"bad{i}", "op": "eq", "value": "x"} for i in range(20)]}
    assert len(validate_rule(rule).errors) <= 25


# ---------------------------------------------------------------- evaluation semantics
CTX = {"country": "US", "device_type": "mobile", "content_category": ["IAB-SPORTS", "IAB-SPORTS-NFL"]}


def test_eq_in_not_in_single_valued():
    assert matches({"attr": "country", "op": "eq", "value": "US"}, CTX)
    assert not matches({"attr": "country", "op": "eq", "value": "CA"}, CTX)
    assert matches({"attr": "country", "op": "in", "values": ["CA", "US"]}, CTX)
    assert not matches({"attr": "country", "op": "not_in", "values": ["US"]}, CTX)
    assert matches({"attr": "country", "op": "not_in", "values": ["GB"]}, CTX)


def test_multi_valued_category_semantics():
    assert matches({"attr": "content_category", "op": "eq", "value": "IAB-SPORTS-NFL"}, CTX)
    assert matches({"attr": "content_category", "op": "in", "values": ["IAB-NEWS", "IAB-SPORTS"]}, CTX)
    assert not matches({"attr": "content_category", "op": "in", "values": ["IAB-NEWS"]}, CTX)
    assert not matches({"attr": "content_category", "op": "not_in", "values": ["IAB-SPORTS-NFL"]}, CTX)


def test_missing_attribute_fails_closed_even_for_not_in():
    rule = {"attr": "content_category", "op": "not_in", "values": ["IAB-NEWS-POLITICS"]}
    m, trace, failures = explain(rule, {"country": "US"})
    assert not m and failures[0]["reason"] == "MISSING_ATTRIBUTE"
    assert not matches({"attr": "placement_type", "op": "in", "values": ["video"]}, {})


def test_and_or():
    rule = {"all": [{"attr": "country", "op": "eq", "value": "US"},
                    {"any": [{"attr": "device_type", "op": "eq", "value": "desktop"},
                             {"attr": "content_category", "op": "in", "values": ["IAB-SPORTS"]}]}]}
    assert matches(rule, CTX)
    assert not matches(rule, {**CTX, "content_category": ["IAB-NEWS"]})


def test_explain_reports_only_decisive_failures():
    rule = {"all": [{"attr": "country", "op": "eq", "value": "US"},
                    {"any": [{"attr": "device_type", "op": "eq", "value": "desktop"},
                             {"attr": "device_type", "op": "eq", "value": "tablet"}]}]}
    m, trace, failures = explain(rule, CTX)
    assert not m
    assert len(trace) == 3
    assert [f["path"] for f in failures] == ["all[1].any[0]", "all[1].any[1]"]
    assert reason_codes(failures) == ["NOT_EQUAL:device_type"]
    # a passing any-group contributes no failures
    ok_rule = {"any": [{"attr": "device_type", "op": "eq", "value": "desktop"},
                       {"attr": "country", "op": "eq", "value": "US"}]}
    assert explain(ok_rule, CTX)[0] and explain(ok_rule, CTX)[2] == []


def test_fast_path_equals_explain():
    import random
    from app import dataset
    rule = {"all": [{"attr": "content_category", "op": "in", "values": ["IAB-SPORTS", "IAB-ENTERTAINMENT"]},
                    {"any": [{"attr": "device_type", "op": "eq", "value": "mobile"},
                             {"attr": "placement_type", "op": "not_in", "values": ["display"]}]}]}
    sample = random.Random(1).sample(dataset.requests(), 2000)
    assert all(matches(rule, r) == explain(rule, r)[0] for r in sample)


def test_hash_is_canonical_and_describe_readable():
    a = {"attr": "country", "op": "in", "values": ["US"]}
    b = {"values": ["US"], "op": "in", "attr": "country"}
    assert rule_hash(a) == rule_hash(b)
    assert describe({"all": [a, {"attr": "device_type", "op": "eq", "value": "mobile"}]}) == \
        "(country in {US} AND device_type = mobile)"
