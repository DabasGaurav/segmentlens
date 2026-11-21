"""Contract tests: the running app and API_CONTRACT.yaml must agree.

1. The contract is a valid OpenAPI 3.1 document.
2. Every /api/v1 route in the app is in the contract, and vice versa.
3. Real responses (success and error) validate against the contract's response schemas.
"""
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from app.main import app
from conftest import CONTRADICTION, SPORTS_BROADEN

SPEC = yaml.safe_load((Path(__file__).resolve().parents[1] / "API_CONTRACT.yaml").read_text())
RESOURCE = Resource.from_contents({**SPEC, "$schema": "https://json-schema.org/draft/2020-12/schema"})
REGISTRY = Registry().with_resource("urn:contract", RESOURCE)


def check(path, method, status, body):
    op = SPEC["paths"][path][method]
    resp = op["responses"][str(status)]
    if "$ref" in resp:
        resp = SPEC["components"]["responses"][resp["$ref"].split("/")[-1]]
    if "content" not in resp:
        assert body in (None, b"", ""), "contract declares no body"
        return
    schema = resp["content"]["application/json"]["schema"]
    ref = schema.get("$ref", None)
    target = {"$ref": "urn:contract" + ref} if ref else {"$ref": "urn:contract#/paths/" +
                                                         path.replace("/", "~1") + f"/{method}/responses/{status}/content/application~1json/schema"}
    errors = sorted(Draft202012Validator(target, registry=REGISTRY).iter_errors(body), key=str)
    assert not errors, f"{method.upper()} {path} {status}: {errors[0].message} at {list(errors[0].path)}"


def test_contract_is_valid_openapi():
    from openapi_spec_validator import validate
    validate(SPEC)


def test_routes_match_contract():
    app_ops = set()
    for r in app.routes:
        if getattr(r, "path", "").startswith("/api/v1") and r.path != "/api/v1/contract.yaml":
            for m in r.methods - {"HEAD", "OPTIONS"}:
                app_ops.add((r.path.removeprefix("/api/v1"), m.lower()))
    spec_ops = {(p, m) for p, item in SPEC["paths"].items() for m in item if m in ("get", "post", "put", "delete", "patch")}
    assert app_ops == spec_ops


def test_responses_validate_against_contract(s):
    P = "/segments/{segment_id}"
    check("/demo/sessions", "post", 201, s.data)
    check("/dataset", "get", 200, s.req("GET", "/dataset", "viewer").json())
    check("/segments", "get", 200, s.req("GET", "/segments", "viewer").json())
    check(P, "get", 200, s.req("GET", "/segments/seg_sports_mobile", "viewer").json())

    r = s.draft(SPORTS_BROADEN); check(P + "/draft", "put", 201, r.json())
    sim = s.simulate().json(); check(P + "/simulations", "post", 201, sim)
    g = s.req("GET", f"/segments/seg_sports_mobile/simulations/{sim['simulation_id']}", "viewer")
    check(P + "/simulations/{simulation_id}", "get", 200, g.json())
    check(P, "get", 200, s.req("GET", "/segments/seg_sports_mobile", "viewer").json())  # with draft + history

    pub = s.req("POST", "/segments/seg_sports_mobile/publish", json={
        "draft_version": 4, "expected_active_version": 3, "simulation_id": sim["simulation_id"],
        "acknowledge_warnings": ["REACH_EXPANSION"]})
    check(P + "/publish", "post", 200, pub.json())

    ev = s.req("POST", "/evaluate?debug=true", json={"context": {"country": "US", "device_type": "mobile"}})
    check("/evaluate", "post", 200, ev.json())
    ev2 = s.req("POST", "/evaluate", "evaluator", json={"context": {"country": "US"}})
    check("/evaluate", "post", 200, ev2.json())

    # errors
    check("/segments", "get", 401, s.c.get("/api/v1/segments").json())
    check(P + "/publish", "post", 403, s.req("POST", "/segments/seg_sports_mobile/publish", "viewer",
                                             json={"draft_version": 5, "expected_active_version": 4,
                                                   "simulation_id": "sim_X"}).json())
    check(P, "get", 404, s.req("GET", "/segments/seg_missing", "viewer").json())
    check(P + "/draft", "put", 422, s.draft({"attr": "user_age", "op": "eq", "value": "x"}, base=4).json())
    check(P + "/draft", "put", 409, s.draft(CONTRADICTION, base=3).json())
    check("/evaluate", "post", 413, s.req("POST", "/evaluate", json={"context": {"request_id": "x" * 20000}}).json())
    assert s.req("POST", "/demo/reset", "viewer").status_code == 204
