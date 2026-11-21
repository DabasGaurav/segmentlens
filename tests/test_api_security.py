"""Workflow, authorisation, isolation and abuse-resistance tests (OWASP API Top 10 oriented)."""
import pytest

from conftest import CONTRADICTION, SPORTS_BROADEN, Session


def err(r):
    return r.json()["error"]["code"]


# ---------------------------------------------------------------- full workflow
def test_end_to_end_publish_changes_evaluation(s):
    ctx = {"context": {"country": "CA", "device_type": "mobile", "content_category": ["IAB-ENTERTAINMENT"]},
           "segment_ids": ["seg_sports_mobile"]}
    before = s.req("POST", "/evaluate", "evaluator", json=ctx).json()["results"][0]
    assert before["matched"] is False and before["version"] == 3
    assert before["reason_codes"] == ["NO_INTERSECTION:content_category"]

    d = s.draft(SPORTS_BROADEN).json()
    assert d["draft_version"] == 4 and d["revision"] == 1 and d["validation"]["valid"]
    sim = s.simulate().json()
    p = s.req("POST", "/segments/seg_sports_mobile/publish", json={
        "draft_version": 4, "expected_active_version": 3, "simulation_id": sim["simulation_id"],
        "acknowledge_warnings": ["REACH_EXPANSION"], "change_note": "buyer approved"})
    assert p.status_code == 200, p.text
    assert p.json()["active_version"] == 4 and p.json()["previous_version"] == 3

    after = s.req("POST", "/evaluate", "evaluator", json=ctx).json()["results"][0]
    assert after["matched"] is True and after["version"] == 4

    detail = s.req("GET", "/segments/seg_sports_mobile", "viewer").json()
    assert detail["draft"] is None and detail["active"]["acknowledged_warnings"] == ["REACH_EXPANSION"]
    assert [h["action"] for h in detail["history"]] == ["draft_saved", "simulated", "published"]


# ---------------------------------------------------------------- authentication (API2)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer"}, {"Authorization": "Basic abc"},
                                     {"Authorization": "Bearer not-a-real-token"}])
def test_missing_or_invalid_credentials(client, headers):
    r = client.get("/api/v1/segments", headers=headers)
    assert r.status_code == 401 and err(r) == "UNAUTHENTICATED"
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_tokens_not_stored_in_plaintext(s):
    from app.store import registry
    assert all(t not in registry.tokens for t in s.tok.values())


def test_expired_session_is_rejected(s):
    from datetime import timedelta
    from app.store import registry
    sbx = registry.sandboxes[s.data["sandbox_id"]]
    for d in sbx.token_digests:
        registry.tokens[d].expires_at -= timedelta(hours=3)
    assert s.req("GET", "/segments", "viewer").status_code == 401


# ---------------------------------------------------------------- function-level authorisation (API5)
@pytest.mark.parametrize("role,method,path,body,scope", [
    ("viewer", "PUT", "/segments/seg_sports_mobile/draft", {"base_version": 3, "rule": SPORTS_BROADEN}, "segments:write"),
    ("evaluator", "PUT", "/segments/seg_sports_mobile/draft", {"base_version": 3, "rule": SPORTS_BROADEN}, "segments:write"),
    ("viewer", "POST", "/segments/seg_sports_mobile/simulations", {}, "simulations:run"),
    ("viewer", "POST", "/segments/seg_sports_mobile/publish",
     {"draft_version": 4, "expected_active_version": 3, "simulation_id": "sim_X"}, "segments:publish"),
    ("evaluator", "POST", "/segments/seg_sports_mobile/publish",
     {"draft_version": 4, "expected_active_version": 3, "simulation_id": "sim_X"}, "segments:publish"),
    ("evaluator", "GET", "/segments", None, "segments:read"),
    ("viewer", "POST", "/evaluate", {"context": {}}, "evaluate:run"),
])
def test_role_cannot_exceed_scope(s, role, method, path, body, scope):
    r = s.req(method, path, role, json=body) if body is not None else s.req(method, path, role)
    assert r.status_code == 403 and err(r) == "FORBIDDEN"
    assert r.json()["error"]["details"][0]["required_scope"] == scope


def test_unauthorised_publish_leaves_state_untouched(s):
    s.draft(SPORTS_BROADEN)
    sim = s.simulate().json()
    body = {"draft_version": 4, "expected_active_version": 3, "simulation_id": sim["simulation_id"],
            "acknowledge_warnings": ["REACH_EXPANSION"]}
    assert s.req("POST", "/segments/seg_sports_mobile/publish", "viewer", json=body).status_code == 403
    assert s.req("GET", "/segments/seg_sports_mobile", "viewer").json()["active_version"] == 3


def test_client_cannot_self_assign_role(client, s):
    # There is no role parameter anywhere: extra fields are rejected rather than trusted.
    r = client.post("/api/v1/segments/seg_sports_mobile/simulations", headers=s.h("viewer"),
                    json={"role": "editor"})
    assert r.status_code in (403, 422)
    r = s.req("PUT", "/segments/seg_sports_mobile/draft", json={"base_version": 3, "rule": SPORTS_BROADEN,
                                                               "active_version": 99})
    assert r.status_code == 422 and err(r) == "VALIDATION_FAILED"  # no mass assignment (API3)


# ---------------------------------------------------------------- debug trace visibility (API3)
def test_debug_trace_requires_trace_scope(s):
    body = {"context": {"country": "US", "device_type": "mobile", "content_category": ["IAB-SPORTS"]}}
    plain = s.req("POST", "/evaluate", "evaluator", json=body).json()
    assert all("trace" not in r for r in plain["results"])
    denied = s.req("POST", "/evaluate?debug=true", "evaluator", json=body)
    assert denied.status_code == 403 and "evaluate:trace" in denied.json()["error"]["message"]
    traced = s.req("POST", "/evaluate?debug=true", "editor", json=body).json()
    assert all("trace" in r for r in traced["results"])


# ---------------------------------------------------------------- object-level isolation (API1)
def test_sandboxes_are_isolated(client, s):
    other = Session(client)
    other.draft(CONTRADICTION)
    assert s.req("GET", "/segments/seg_sports_mobile", "viewer").json()["draft"] is None
    sim = other.simulate().json()
    r = s.req("GET", f"/segments/seg_sports_mobile/simulations/{sim['simulation_id']}", "viewer")
    assert r.status_code == 404  # another sandbox's id is indistinguishable from a missing one
    r = s.req("POST", "/segments/seg_sports_mobile/publish", json={
        "draft_version": 4, "expected_active_version": 3, "simulation_id": sim["simulation_id"]})
    assert r.status_code in (404, 409)


def test_unknown_segment_is_404(s):
    assert s.req("GET", "/segments/seg_nope", "viewer").status_code == 404
    assert err(s.req("POST", "/evaluate", "evaluator", json={"context": {}, "segment_ids": ["seg_nope"]})) == "NOT_FOUND"


def test_reset_restores_seed(s):
    s.draft(SPORTS_BROADEN)
    assert s.req("POST", "/demo/reset", "viewer").status_code == 204
    assert s.req("GET", "/segments/seg_sports_mobile", "viewer").json()["draft"] is None


# ---------------------------------------------------------------- publish integrity
def _publish(s, sim_id, draft_version=4, expected=3, ack=("REACH_EXPANSION",)):
    return s.req("POST", "/segments/seg_sports_mobile/publish", json={
        "draft_version": draft_version, "expected_active_version": expected, "simulation_id": sim_id,
        "acknowledge_warnings": list(ack)})


def test_publish_without_draft(s):
    assert err(_publish(s, "sim_ABCDEFGH")) == "NO_DRAFT"


def test_simulate_without_draft(s):
    assert err(s.simulate()) == "NO_DRAFT"


def test_publish_requires_known_simulation(s):
    s.draft(SPORTS_BROADEN)
    assert _publish(s, "sim_ABCDEFGH").status_code == 404


def test_stale_simulation_rejected(s):
    s.draft(SPORTS_BROADEN)
    sim = s.simulate().json()
    s.draft(CONTRADICTION)  # draft edited after simulation
    r = _publish(s, sim["simulation_id"])
    assert r.status_code == 409 and err(r) == "SIMULATION_STALE"


def test_resaving_identical_rule_still_requires_fresh_simulation(s):
    s.draft(SPORTS_BROADEN)
    sim = s.simulate().json()
    s.draft(SPORTS_BROADEN)  # new revision, same content
    assert err(_publish(s, sim["simulation_id"])) == "SIMULATION_STALE"


def test_unacknowledged_warning_rejected(s):
    s.draft(SPORTS_BROADEN)
    sim = s.simulate().json()
    r = _publish(s, sim["simulation_id"], ack=())
    assert r.status_code == 409 and err(r) == "UNACKNOWLEDGED_WARNINGS"
    assert r.json()["error"]["details"][0]["unacknowledged"] == ["REACH_EXPANSION"]


def test_blocking_warning_cannot_be_acknowledged_away(s):
    s.draft(CONTRADICTION)
    sim = s.simulate().json()
    r = _publish(s, sim["simulation_id"], ack=("ZERO_MATCH", "REACH_DROP", "HIGH_FLIP_RATE"))
    assert r.status_code == 409 and err(r) == "BLOCKING_WARNING"


def test_version_conflicts(s):
    s.draft(SPORTS_BROADEN)
    sim = s.simulate().json()
    assert err(_publish(s, sim["simulation_id"], expected=2)) == "VERSION_CONFLICT"
    assert err(_publish(s, sim["simulation_id"], draft_version=7)) == "VERSION_CONFLICT"
    assert _publish(s, sim["simulation_id"]).status_code == 200
    # replaying the same publish now conflicts instead of double-publishing
    assert _publish(s, sim["simulation_id"]).status_code == 409
    # a draft based on the old active version conflicts
    r = s.draft(SPORTS_BROADEN, base=3)
    assert r.status_code == 409 and err(r) == "VERSION_CONFLICT"


def test_concurrent_publish_only_one_wins(s):
    from concurrent.futures import ThreadPoolExecutor
    s.draft(SPORTS_BROADEN)
    sim = s.simulate().json()
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: _publish(s, sim["simulation_id"]).status_code, range(8)))
    assert codes.count(200) == 1 and codes.count(409) == 7
    detail = s.req("GET", "/segments/seg_sports_mobile", "viewer").json()
    assert detail["active_version"] == 4 and len(detail["versions"]) == 4


# ---------------------------------------------------------------- input validation and limits (API4)
def test_validation_error_shape(s):
    r = s.draft({"all": [{"attr": "user_age", "op": "eq", "value": "18-24"},
                         {"attr": "country", "op": "in", "values": [f"X{i}" for i in range(64)]}]})
    assert r.status_code == 422
    e = r.json()["error"]
    assert e["code"] == "VALIDATION_FAILED" and e["request_id"].startswith("rq_")
    assert {d["code"] for d in e["details"]} == {"UNKNOWN_ATTRIBUTE", "TOO_MANY_VALUES"}
    assert e["details"][0]["path"] == "rule.all[0].attr"


def test_malformed_json(s):
    r = s.c.put("/api/v1/segments/seg_sports_mobile/draft", headers={**s.h("editor"), "Content-Type": "application/json"},
                content=b'{"base_version": 3, "rule": {')
    assert r.status_code == 422 and err(r) == "VALIDATION_FAILED"


def test_oversized_payload(s):
    big = {"base_version": 3, "rule": {"attr": "country", "op": "in", "values": ["US"]}, "change_note": "x" * 20000}
    r = s.req("PUT", "/segments/seg_sports_mobile/draft", json=big)
    assert r.status_code == 413 and err(r) == "PAYLOAD_TOO_LARGE"


def test_chunked_body_rejected(s):
    def gen():
        yield b'{"context": {}}'
    r = s.c.post("/api/v1/evaluate", headers={**s.h("evaluator"), "Content-Type": "application/json"}, content=gen())
    assert r.status_code == 411


def test_excessive_nesting_via_api(s):
    leaf = {"attr": "country", "op": "eq", "value": "US"}
    deep = leaf
    for i in range(6):
        deep = {"all" if i % 2 else "any": [deep]}
    r = s.draft(deep)
    assert r.status_code == 422 and "MAX_DEPTH_EXCEEDED" in {d["code"] for d in r.json()["error"]["details"]}


def test_evaluate_context_validation(s):
    for ctx in [{"country": "usa"}, {"device_type": "fridge"}, {"user_id": "u1"},
                {"content_category": ["IAB-SPORTS"] * 11}]:
        r = s.req("POST", "/evaluate", "evaluator", json={"context": ctx})
        assert r.status_code == 422, ctx


def test_simulation_rate_limit(s):
    s.draft(SPORTS_BROADEN)
    codes = [s.simulate(max_sample_flips=0).status_code for _ in range(31)]
    assert codes[:30] == [201] * 30 and codes[30] == 429
    assert s.simulate().headers.get("Retry-After")


def test_session_creation_rate_limit(client):
    codes = [client.post("/api/v1/demo/sessions").status_code for _ in range(21)]
    assert codes[-1] == 429 and codes.count(201) == 20


def test_stored_simulations_bounded(s):
    from app.store import MAX_SIMULATIONS_PER_SANDBOX, registry
    s.draft(SPORTS_BROADEN)
    for _ in range(25):
        s.simulate(max_sample_flips=0)
    assert len(registry.sandboxes[s.data["sandbox_id"]].simulations) == MAX_SIMULATIONS_PER_SANDBOX


def test_sandbox_count_bounded(monkeypatch, client):
    from app import store
    monkeypatch.setattr(store, "MAX_SANDBOXES", 3)
    monkeypatch.setattr("app.main.LIMITS", {**__import__("app.main").main.LIMITS, "session_per_ip": (1000, 3600)})
    for _ in range(6):
        client.post("/api/v1/demo/sessions")
    assert len(store.registry.sandboxes) <= 3


# ---------------------------------------------------------------- misconfiguration (API8)
def test_security_headers_and_no_stack_traces(client):
    r = client.get("/api/v1/segments")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Request-Id"].startswith("rq_")
    assert "Traceback" not in r.text
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_no_cors_by_default(client):
    r = client.options("/api/v1/segments", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
