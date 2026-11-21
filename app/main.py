"""SegmentLens: internal targeting-segment API with simulation-gated publishing."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import dataset, simulate
from .rules import describe, explain, reason_codes, rule_hash, validate_rule
from .store import MAX_SIMULATIONS_PER_SANDBOX, Identity, Sandbox, iso, limiter, new_id, now, registry

ROOT = Path(__file__).resolve().parent
CONTRACT = ROOT.parent / "API_CONTRACT.yaml"
MAX_BODY = 16 * 1024
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"
LIMITS = {"per_token": (120, 60.0), "simulate": (30, 60.0), "session_per_ip": (20, 3600.0)}

app = FastAPI(title="SegmentLens", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


# ----------------------------------------------------------------------------- errors
class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: list | None = None,
                 headers: dict | None = None):
        self.status, self.code, self.message = status, code, message
        self.details, self.headers = details or [], headers or {}


def _err_body(request: Request, code: str, message: str, details: list | None = None) -> dict:
    body = {"code": code, "message": message, "request_id": getattr(request.state, "request_id", "rq_unknown")}
    if details:
        body["details"] = details
    return {"error": body}


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError):
    return JSONResponse(_err_body(request, exc.code, exc.message, exc.details), exc.status, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    details = []
    for e in exc.errors()[:25]:
        loc = [str(p) for p in e.get("loc", []) if p != "body"]
        details.append({"path": ".".join(loc) or "body", "code": e.get("type", "invalid").upper(),
                        "message": e.get("msg", "Invalid value.")})
    return JSONResponse(_err_body(request, "VALIDATION_FAILED", "Request failed validation.", details), 422)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
    return JSONResponse(_err_body(request, code, str(exc.detail)), exc.status_code)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):  # never leak stack traces
    return JSONResponse(_err_body(request, "INTERNAL", "Unexpected error."), 500)


# ----------------------------------------------------------------------------- middleware
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"),
}


@app.middleware("http")
async def _envelope(request: Request, call_next):
    request.state.request_id = new_id("rq_", 10).lower()
    if request.method in ("POST", "PUT", "PATCH"):
        if "chunked" in request.headers.get("transfer-encoding", "").lower():
            resp = JSONResponse(_err_body(request, "LENGTH_REQUIRED", "Chunked bodies are not accepted."), 411)
            return _decorate(resp, request)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            length = MAX_BODY + 1
        if length > MAX_BODY:
            resp = JSONResponse(_err_body(request, "PAYLOAD_TOO_LARGE", f"Body exceeds {MAX_BODY} bytes."), 413)
            return _decorate(resp, request)
    return _decorate(await call_next(request), request)


def _decorate(resp: Response, request: Request) -> Response:
    resp.headers["X-Request-Id"] = request.state.request_id
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def client_ip(request: Request) -> str:
    if TRUST_PROXY and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ----------------------------------------------------------------------------- auth
class Caller:
    def __init__(self, ident: Identity, sbx: Sandbox):
        self.ident, self.sbx = ident, sbx

    def has(self, scope: str) -> bool:
        return scope in self.ident.scopes


def authenticate(request: Request) -> Caller:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "UNAUTHENTICATED", "Missing bearer token. Create one with POST /api/v1/demo/sessions.",
                       headers={"WWW-Authenticate": "Bearer"})
    resolved = registry.resolve(token.strip())
    if resolved is None:
        raise ApiError(401, "UNAUTHENTICATED", "Invalid or expired token.", headers={"WWW-Authenticate": "Bearer"})
    ident, sbx = resolved
    retry = limiter.check(f"tok:{registry.digest(token.strip())}", *LIMITS["per_token"])
    if retry:
        raise ApiError(429, "RATE_LIMITED", "Too many requests for this token.",
                       headers={"Retry-After": str(int(retry))})
    return Caller(ident, sbx)


def require(scope: str):
    def dep(caller: Caller = Depends(authenticate)) -> Caller:
        if not caller.has(scope):
            raise ApiError(403, "FORBIDDEN", f"Token scope does not include '{scope}'.",
                           [{"required_scope": scope, "identity": caller.ident.principal}])
        return caller
    return dep


# ----------------------------------------------------------------------------- schemas
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftIn(Strict):
    base_version: int | None
    rule: dict[str, Any]
    change_note: str | None = Field(default=None, max_length=280)


class SimulationIn(Strict):
    compare_with: list[str] | None = Field(default=None, max_length=5)
    max_sample_flips: int = Field(default=10, ge=0, le=20)


class PublishIn(Strict):
    draft_version: int
    expected_active_version: int | None
    simulation_id: str = Field(max_length=64)
    acknowledge_warnings: list[str] = Field(default_factory=list, max_length=10)
    change_note: str | None = Field(default=None, max_length=280)


class AdContext(Strict):
    request_id: str | None = Field(default=None, max_length=64)
    publisher_id: str | None = Field(default=None, max_length=64)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    device_type: Literal["mobile", "desktop", "tablet", "ctv"] | None = None
    placement_type: Literal["display", "native", "video"] | None = None
    content_category: list[str] | None = Field(default=None, max_length=10)


class EvaluateIn(Strict):
    context: AdContext
    segment_ids: list[str] | None = Field(default=None, max_length=50)


# ----------------------------------------------------------------------------- helpers
def get_segment(sbx: Sandbox, segment_id: str) -> dict:
    seg = sbx.segments.get(segment_id)
    if seg is None:  # other sandboxes' ids are indistinguishable from nonexistent ones
        raise ApiError(404, "NOT_FOUND", f"Segment '{segment_id}' not found.")
    return seg


def active_of(seg: dict) -> dict | None:
    v = seg["active_version"]
    return None if v is None else seg["versions"][v - 1]


def summary(seg: dict) -> dict:
    d = seg["draft"]
    return {"id": seg["id"], "name": seg["name"], "description": seg["description"],
            "active_version": seg["active_version"], "has_draft": d is not None,
            "draft_version": d["draft_version"] if d else None, "updated_at": seg["updated_at"]}


def with_readable(version_or_draft: dict | None) -> dict | None:
    if version_or_draft is None:
        return None
    out = dict(version_or_draft)
    out["rule_text"] = describe(out["rule"])
    return out


# ----------------------------------------------------------------------------- routes: demo
@app.post("/api/v1/demo/sessions", status_code=201, tags=["demo"])
def create_session(request: Request):
    retry = limiter.check(f"ip:{client_ip(request)}", *LIMITS["session_per_ip"])
    if retry:
        raise ApiError(429, "RATE_LIMITED", "Too many demo sessions from this address. Reuse your existing session.",
                       headers={"Retry-After": str(int(retry))})
    return registry.create_session()


@app.post("/api/v1/demo/reset", status_code=204, tags=["demo"])
def reset(caller: Caller = Depends(authenticate)):
    with caller.sbx.lock:
        caller.sbx.reset()
        caller.sbx.add_audit(caller.ident.principal, "reset", None, None, {})
    return Response(status_code=204)


@app.get("/api/v1/dataset", tags=["simulation"])
def get_dataset(caller: Caller = Depends(authenticate)):
    return dataset.describe()


# ----------------------------------------------------------------------------- routes: segments
@app.get("/api/v1/segments", tags=["segments"])
def list_segments(caller: Caller = Depends(require("segments:read"))):
    with caller.sbx.lock:
        return {"items": [summary(s) for s in caller.sbx.segments.values()]}


@app.get("/api/v1/segments/{segment_id}", tags=["segments"])
def segment_detail(segment_id: str, caller: Caller = Depends(require("segments:read"))):
    with caller.sbx.lock:
        seg = get_segment(caller.sbx, segment_id)
        out = summary(seg)
        out["active"] = with_readable(active_of(seg))
        out["draft"] = with_readable(seg["draft"])
        out["history"] = [a for a in caller.sbx.audit if a["segment_id"] == segment_id][-20:]
        out["versions"] = [{"version": v["version"], "rule_text": describe(v["rule"]),
                            "published_at": v["published_at"], "published_by": v["published_by"],
                            "change_note": v["change_note"]} for v in seg["versions"]]
        return out


@app.put("/api/v1/segments/{segment_id}/draft", status_code=201, tags=["segments"])
def put_draft(segment_id: str, body: DraftIn, caller: Caller = Depends(require("segments:write"))):
    res = validate_rule(body.rule)
    if not res.valid:
        raise ApiError(422, "VALIDATION_FAILED", "Rule failed validation.", res.errors)
    sbx = caller.sbx
    with sbx.lock:
        seg = get_segment(sbx, segment_id)
        if body.base_version != seg["active_version"]:
            raise ApiError(409, "VERSION_CONFLICT",
                           f"Active version is {seg['active_version']}, draft was based on {body.base_version}.",
                           [{"base_version": body.base_version, "current_active_version": seg["active_version"]}])
        prev = seg["draft"]
        ts = iso(now())
        draft = {"segment_id": segment_id, "draft_version": len(seg["versions"]) + 1,
                 "base_version": seg["active_version"], "revision": (prev["revision"] + 1) if prev else 1,
                 "rule": body.rule, "rule_hash": rule_hash(body.rule), "status": "draft",
                 "validation": {"valid": True, "leaf_count": res.leaf_count, "max_depth": res.max_depth},
                 "change_note": body.change_note, "updated_by": caller.ident.principal, "updated_at": ts}
        seg["draft"] = draft
        seg["updated_at"] = ts
        sbx.add_audit(caller.ident.principal, "draft_saved", segment_id, draft["draft_version"],
                      {"revision": draft["revision"], "rule_hash": draft["rule_hash"],
                       "rule_text": describe(body.rule)})
        return with_readable(draft)


@app.post("/api/v1/segments/{segment_id}/simulations", status_code=201, tags=["simulation"])
def run_simulation(segment_id: str, request: Request, body: SimulationIn | None = None,
                   caller: Caller = Depends(require("simulations:run"))):
    body = body or SimulationIn()
    retry = limiter.check(f"sim:{caller.ident.principal}", *LIMITS["simulate"])
    if retry:
        raise ApiError(429, "RATE_LIMITED", "Too many simulations; wait a moment.",
                       headers={"Retry-After": str(int(retry))})
    sbx = caller.sbx
    with sbx.lock:
        seg = get_segment(sbx, segment_id)
        draft = seg["draft"]
        if draft is None:
            raise ApiError(409, "NO_DRAFT", "This segment has no draft to simulate. Save a draft first.")
        active = active_of(seg)
        if body.compare_with is None:
            others_ids = [sid for sid in sbx.segments if sid != segment_id]
        else:
            others_ids = body.compare_with
            missing = [sid for sid in others_ids if sid not in sbx.segments or sid == segment_id]
            if missing:
                raise ApiError(422, "VALIDATION_FAILED", "Unknown comparison segment(s).",
                               [{"path": "compare_with", "code": "UNKNOWN_SEGMENT", "message": m} for m in missing])
        others = []
        for sid in others_ids:
            o = sbx.segments[sid]
            if o["active_version"] is not None:
                others.append((sid, o["active_version"], active_of(o)["rule"]))
        result = simulate.run(active["rule"] if active else None, draft["rule"], others, body.max_sample_flips)
        sim = {"simulation_id": new_id("sim_"), "segment_id": segment_id,
               "active_version": seg["active_version"], "draft_version": draft["draft_version"],
               "draft_revision": draft["revision"], "draft_rule_hash": draft["rule_hash"],
               "dataset": {k: v for k, v in dataset.describe().items() if k != "distributions"},
               **result, "created_at": iso(now()), "created_by": caller.ident.principal}
        sbx.simulations[sim["simulation_id"]] = sim
        while len(sbx.simulations) > MAX_SIMULATIONS_PER_SANDBOX:
            sbx.simulations.popitem(last=False)
        sbx.add_audit(caller.ident.principal, "simulated", segment_id, draft["draft_version"],
                      {"simulation_id": sim["simulation_id"], "warnings": [w["code"] for w in sim["warnings"]],
                       "relative_reach_change": sim["metrics"]["relative_reach_change"]})
        return sim


@app.get("/api/v1/segments/{segment_id}/simulations/{simulation_id}", tags=["simulation"])
def get_simulation(segment_id: str, simulation_id: str, caller: Caller = Depends(require("simulations:read"))):
    with caller.sbx.lock:
        get_segment(caller.sbx, segment_id)
        sim = caller.sbx.simulations.get(simulation_id)
        if sim is None or sim["segment_id"] != segment_id:
            raise ApiError(404, "NOT_FOUND", f"Simulation '{simulation_id}' not found.")
        return sim


@app.post("/api/v1/segments/{segment_id}/publish", tags=["segments"])
def publish(segment_id: str, body: PublishIn, caller: Caller = Depends(require("segments:publish"))):
    sbx = caller.sbx
    with sbx.lock:  # check-and-swap is atomic within the sandbox
        seg = get_segment(sbx, segment_id)
        draft = seg["draft"]
        if draft is None:
            raise ApiError(409, "NO_DRAFT", "Nothing to publish: this segment has no draft.")
        if body.draft_version != draft["draft_version"]:
            raise ApiError(409, "VERSION_CONFLICT",
                           f"Current draft is version {draft['draft_version']}, not {body.draft_version}.",
                           [{"requested_draft_version": body.draft_version,
                             "current_draft_version": draft["draft_version"]}])
        if body.expected_active_version != seg["active_version"]:
            raise ApiError(409, "VERSION_CONFLICT",
                           f"Active version is {seg['active_version']}, expected {body.expected_active_version}. "
                           "Re-simulate against the current version.",
                           [{"expected_active_version": body.expected_active_version,
                             "current_active_version": seg["active_version"]}])
        sim = sbx.simulations.get(body.simulation_id)
        if sim is None or sim["segment_id"] != segment_id:
            raise ApiError(404, "NOT_FOUND", f"Simulation '{body.simulation_id}' not found for this segment.")
        if sim["draft_rule_hash"] != draft["rule_hash"] or sim["draft_revision"] != draft["revision"] \
                or sim["active_version"] != seg["active_version"]:
            raise ApiError(409, "SIMULATION_STALE",
                           "The draft or active version changed after this simulation. Run a new simulation.",
                           [{"simulation_id": sim["simulation_id"], "simulated_rule_hash": sim["draft_rule_hash"],
                             "current_rule_hash": draft["rule_hash"]}])
        blocking = [w["code"] for w in sim["warnings"] if w["severity"] == "block"]
        if blocking:
            raise ApiError(409, "BLOCKING_WARNING", "This draft has a blocking warning and cannot be published.",
                           [{"warnings": blocking}])
        needs_ack = sorted({w["code"] for w in sim["warnings"] if w["severity"] == "warn"})
        missing = [c for c in needs_ack if c not in body.acknowledge_warnings]
        if missing:
            raise ApiError(409, "UNACKNOWLEDGED_WARNINGS",
                           "Acknowledge each warn-level warning to publish.", [{"unacknowledged": missing}])
        if not validate_rule(draft["rule"]).valid:  # defence in depth
            raise ApiError(422, "VALIDATION_FAILED", "Stored draft no longer validates.")
        ts = iso(now())
        version = {"version": draft["draft_version"], "rule": draft["rule"], "rule_hash": draft["rule_hash"],
                   "published_at": ts, "published_by": caller.ident.principal,
                   "change_note": body.change_note or draft["change_note"], "simulation_id": sim["simulation_id"],
                   "acknowledged_warnings": needs_ack}
        previous = seg["active_version"]
        seg["versions"].append(version)
        seg["active_version"] = version["version"]
        seg["draft"] = None
        seg["updated_at"] = ts
        audit_id = sbx.add_audit(caller.ident.principal, "published", segment_id, version["version"],
                                 {"previous_version": previous, "simulation_id": sim["simulation_id"],
                                  "acknowledged_warnings": needs_ack, "change_note": version["change_note"],
                                  "rule_text": describe(version["rule"])})
        return {"segment_id": segment_id, "active_version": version["version"], "previous_version": previous,
                "rule_hash": version["rule_hash"], "published_by": version["published_by"],
                "published_at": ts, "audit_id": audit_id}


# ----------------------------------------------------------------------------- routes: evaluation
@app.post("/api/v1/evaluate", tags=["evaluation"])
def evaluate(body: EvaluateIn, debug: bool = Query(default=False),
             caller: Caller = Depends(require("evaluate:run"))):
    if debug and not caller.has("evaluate:trace"):
        raise ApiError(403, "FORBIDDEN", "Debug traces require scope 'evaluate:trace'.",
                       [{"required_scope": "evaluate:trace", "identity": caller.ident.principal}])
    ctx = body.context.model_dump(exclude_none=True)
    sbx = caller.sbx
    with sbx.lock:
        ids = body.segment_ids if body.segment_ids is not None else list(sbx.segments)
        unknown = [i for i in ids if i not in sbx.segments]
        if unknown:
            raise ApiError(404, "NOT_FOUND", "Unknown segment id(s).", [{"segment_ids": unknown[:10]}])
        targets = [(i, active_of(sbx.segments[i])) for i in ids]
    results = []
    for seg_id, active in targets:
        if active is None:
            continue
        matched, trace, failures = explain(active["rule"], ctx)
        r: dict = {"segment_id": seg_id, "version": active["version"], "matched": matched}
        if not matched:
            r["reason_codes"] = reason_codes(failures)
        if debug:
            r["trace"] = trace
        results.append(r)
    return {"request_id": ctx.get("request_id"), "results": results, "evaluated_at": iso(now())}


# ----------------------------------------------------------------------------- docs + playground
@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/api/v1/contract.yaml", include_in_schema=False)
def contract():
    return FileResponse(CONTRACT, media_type="application/yaml")


DOCS_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>SegmentLens API docs</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/vendor/swagger-ui.css"></head>
<body><div id="ui"></div><script src="/static/vendor/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:'/api/v1/contract.yaml',dom_id:'#ui',persistAuthorization:false,
tryItOutEnabled:true});</script></body></html>"""


@app.get("/docs", include_in_schema=False)
def docs():
    return HTMLResponse(DOCS_HTML)


@app.get("/", include_in_schema=False)
def playground():
    return FileResponse(ROOT / "static" / "index.html", media_type="text/html")
