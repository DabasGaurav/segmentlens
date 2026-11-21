import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import limiter

SPORTS_BROADEN = {"all": [
    {"attr": "content_category", "op": "in", "values": ["IAB-SPORTS", "IAB-ENTERTAINMENT"]},
    {"attr": "device_type", "op": "eq", "value": "mobile"},
    {"attr": "country", "op": "in", "values": ["US", "CA"]},
]}
CONTRADICTION = {"all": [
    {"attr": "device_type", "op": "eq", "value": "mobile"},
    {"attr": "device_type", "op": "eq", "value": "desktop"},
]}


@pytest.fixture(autouse=True)
def _fresh_limits():
    limiter._hits.clear()
    yield


@pytest.fixture
def client():
    return TestClient(app)


class Session:
    def __init__(self, client):
        r = client.post("/api/v1/demo/sessions")
        assert r.status_code == 201, r.text
        self.data = r.json()
        self.c = client
        self.tok = {i["role"]: i["token"] for i in self.data["identities"]}

    def h(self, role):
        return {"Authorization": f"Bearer {self.tok[role]}"}

    def req(self, method, path, role="editor", **kw):
        return self.c.request(method, "/api/v1" + path, headers=self.h(role), **kw)

    def draft(self, rule, seg="seg_sports_mobile", base=3, role="editor"):
        return self.req("PUT", f"/segments/{seg}/draft", role, json={"base_version": base, "rule": rule})

    def simulate(self, seg="seg_sports_mobile", role="editor", **body):
        return self.req("POST", f"/segments/{seg}/simulations", role, json=body or {})


@pytest.fixture
def s(client):
    return Session(client)
