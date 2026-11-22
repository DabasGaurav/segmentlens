# TEST_REPORT.md

**Result: 71 / 71 automated tests pass** (`pytest -q`, 2.6 s) with the pinned versions in `requirements-dev.txt`
(FastAPI 0.115.6, Pydantic 2.10.4, Python 3.13 locally; the container uses Python 3.12).
There's one warning, a third-party deprecation notice inside Starlette's TestClient (`anyio.abc.BlockingPortal`), which doesn't affect the app.

## Coverage by area

| Area | File | Tests | What is proven |
|---|---|---|---|
| Rule grammar and semantics | `tests/test_rules.py` | 18 | Allowlisted attributes, operators and values; types; operand shapes; duplicates; unknown fields; depth ≤ 4; ≤ 20 conditions; ≤ 50 values; bounded error list; eq / in / not_in for single- and multi-valued attributes; **fail-closed on missing data, including `not_in`**; AND/OR; only decisive failures are reported; the fast path equals the traced path on 2,000 requests; canonical hashing |
| Simulation correctness | `tests/test_simulation.py` | 9 | Deterministic seeded dataset; **every metric re-computed by brute force and compared to the API**; identical draft gives 0 flips; broadening gives `REACH_EXPANSION` and an exact overlap count; narrowing gives `REACH_DROP`; contradiction gives `ZERO_MATCH` (block); flips are explained on the side that didn't match; zero denominators return `null`; repeated runs are identical |
| Workflow, security, abuse | `tests/test_api_security.py` | 41 | End-to-end edit, simulate, publish and evaluate, with evaluation flipping from v3 to v4; missing, malformed or invalid tokens (401); tokens never stored in plaintext; expired sessions; **7 role × action combinations refused with 403 and the required scope**; refused publish leaves state unchanged; no self-assigned role or mass assignment; debug trace needs `evaluate:trace`; **cross-sandbox isolation (404)**; reset; `NO_DRAFT`; unknown simulation; `SIMULATION_STALE` (rule changed, and identical re-save); `UNACKNOWLEDGED_WARNINGS`; `BLOCKING_WARNING` can't be acknowledged away; `VERSION_CONFLICT` (active, draft, replay, stale base); **8 concurrent publishes, exactly 1 wins**; validation error shape and paths; malformed JSON; 413 oversized; 411 chunked; excessive nesting; context validation; simulation rate limit (429 + `Retry-After`); session rate limit; stored-simulation cap; sandbox cap; security headers; no stack traces; no CORS |
| Contract conformance | `tests/test_contract.py` | 3 | `API_CONTRACT.yaml` is valid OpenAPI 3.1; **app routes and contract operations are identical sets**; real 200/201/401/403/404/409/413/422 responses validate against the contract schemas |

## Manual and browser checks
| Check | Result |
|---|---|
| Playground in headless Chromium: load, preset, simulate, publish without acknowledgement (409 shown), acknowledge and publish (v4), evaluate | Pass. The only console error is the intended 409 |
| Swagger UI at `/docs` renders all 10 operations from the contract (vendored assets, no CDN) | Pass |
| Production start command (`uvicorn … --proxy-headers`) with pinned dependencies: `/healthz`, `/`, `/docs` and `/api/v1/contract.yaml` return 200 | Pass |
| Performance (in-process) | About 0.64 µs per segment evaluation; cold simulation about 70 ms (20k requests, 5 rules) |

## Live deployment check (https://segmentlens.onrender.com, 22 Nov 2026)
Render built the Docker image (free plan, deploy took 33 s), and the service is Live. I ran the whole flow against production from a browser:
new session 201 · list of 4 seed segments · no token 401 · viewer draft 403 · editor draft 201 · simulation 201 with
**exactly the same metrics as local** (880 → 1,578 matched, +79.32%, 698 newly eligible, flip rate 3.49%) · publish without
acknowledgement 409 `UNACKNOWLEDGED_WARNINGS` · viewer publish 403 · publish 200 → v4 · replay 409 · evaluator now matches v4 ·
evaluator debug 403 · oversized body 413 · unknown attribute 422 `UNKNOWN_ATTRIBUTE` · `/docs`, `/api/v1/contract.yaml`, `/healthz` 200 ·
security headers present. I also checked the playground and Swagger UI visually.

## Not tested or unresolved
- **Load testing** of the deployed instance wasn't done. The free tier is sized for a handful of reviewers.
- **Rate limiting behind a proxy** trusts the left-most `X-Forwarded-For` value when `TRUST_PROXY=1`, which a
  client can spoof. That's acceptable for a demo, and production would use the edge's verified client IP.
- **Accessibility:** the playground uses semantic controls but hasn't been audited.
