# SegmentLens: see the blast radius of a targeting change before it ships

A prototype **internal API for contextual targeting segments**, built for the Media.net APM case (Problem 3).
Its main idea: **publishing is gated by a simulation.** An AdOps user edits a segment draft, simulates it
against the active version on the same request sample, sees which ad opportunities would enter or leave
the segment and *why*, and can only publish that exact, simulated draft. Version checks are enforced, and
every warning must be explicitly acknowledged.

> All data is synthetic. Nothing here uses or claims to reflect Media.net's internal systems or data.

- **Playground:** `/` (a web UI that calls the real API, with a live request log)
- **Interactive API docs:** `/docs` (Swagger UI rendered from the canonical `API_CONTRACT.yaml`)
- **Contract:** `/api/v1/contract.yaml` (OpenAPI 3.1)

## 1. The demo in 60 seconds

1. Open `/`. You get a private sandbox and three **server-issued** identities: viewer, editor and evaluator.
2. Pick **Sports · Mobile · North America** (active v3: Sports AND mobile AND US/CA).
3. Click **Broaden: add Entertainment**, then **② Simulate vs active**.
4. Read the result: about +79% relative reach, 698 newly eligible requests, 0 lost, 44.9% co-eligible with the
   Entertainment segment, a `REACH_EXPANSION` warning, and sample flips showing the failed condition
   (`content_category in → NO_INTERSECTION` under the active version).
5. Click **Publish** without ticking the acknowledgement: the server returns `409 UNACKNOWLEDGED_WARNINGS`.
   Tick it and publish: you get v4, plus an audit entry.
6. Switch to **Viewer** and try to publish or save: you get a real `403 FORBIDDEN` from the server.
7. **Evaluate a request** as **Evaluator**: you get decisions with coarse reason codes. Asking for
   `debug trace` returns 403. As Editor, you get the per-condition trace.
8. Try **Mistake: AND instead of OR**. The simulation shows 0 matches and a `ZERO_MATCH` **block** warning,
   and publish is refused even if you acknowledge it.

## 2. API surface

| Method and path | Scope | Purpose |
|---|---|---|
| `POST /api/v1/demo/sessions` | none (rate-limited per IP) | New sandbox plus viewer, editor and evaluator tokens (2 h TTL) |
| `POST /api/v1/demo/reset` | any token | Reset the sandbox to seed data |
| `GET /api/v1/dataset` | any token | Size, seed and distributions of the synthetic sample |
| `GET /api/v1/segments` | `segments:read` | List segments, active version and draft status |
| `GET /api/v1/segments/{id}` | `segments:read` | Active definition, draft, versions, audit history |
| `PUT /api/v1/segments/{id}/draft` | `segments:write` | Create or replace the draft (`base_version` optimistic lock) |
| `POST /api/v1/segments/{id}/simulations` | `simulations:run` | Compare active vs draft on the same sample |
| `GET /api/v1/segments/{id}/simulations/{sim}` | `simulations:read` | Re-read a stored simulation |
| `POST /api/v1/segments/{id}/publish` | `segments:publish` | Atomically publish a simulated draft |
| `POST /api/v1/evaluate[?debug=true]` | `evaluate:run` (+ `evaluate:trace` for debug) | Decide one ad-request context against active segments |

Roles: **viewer** = read segments and simulations · **editor** = viewer + draft, simulate, publish, evaluate,
trace · **evaluator** = `evaluate:run` only (it can't read definitions or traces).

### Rule language (deliberately small)
```json
{"all": [ {"attr": "content_category", "op": "in", "values": ["IAB-SPORTS"]},
          {"any": [ {"attr": "device_type", "op": "eq", "value": "mobile"},
                    {"attr": "placement_type", "op": "not_in", "values": ["display"]} ]} ]}
```
- Attributes: `content_category` (multi-valued), `country`, `device_type`, `placement_type`, `publisher_id`.
  Values are allowlisted.
- Operators: `eq`, `in`, `not_in`; groups `all` (AND) and `any` (OR). Max depth 4, max 20 conditions,
  max 50 values per list, max request body 16 KB.
- For multi-valued `content_category`: `in` = non-empty intersection, `not_in` = no intersection, `eq` = contains.
- **Missing data fails closed:** if the request lacks the attribute, the condition is false
  (`MISSING_ATTRIBUTE`), including for `not_in`. A brand-suitability exclusion should never pass on an
  unclassified page by accident.
- Rules are interpreted by a ~40-line evaluator. There's no `eval`, no regex from input and no dynamic queries.

### Simulation metrics (as implemented and tested)
| Metric | Definition |
|---|---|
| Match rate | matched requests / sample size (20,000) |
| pp change | (draft match rate − active match rate) × 100 |
| Relative reach change | (draft matched − active matched) / active matched; `null` when active matched = 0 |
| Newly eligible / no longer eligible | draft∖active and active∖draft |
| Flip rate | (newly + no longer eligible) / sample size |
| Co-eligibility with segment S | |draft ∩ S| / draft matched; `null` when draft matched = 0. **Not cannibalisation** |

Warnings: `ZERO_MATCH` (block) · `REACH_DROP` ≤ −25% (warn) · `REACH_EXPANSION` ≥ +50% (warn) ·
`HIGH_FLIP_RATE` ≥ 10% (warn) · `HIGH_OVERLAP` ≥ 80% (warn) · `MISSING_DATA_EXPOSURE` ≥ 10% (info).

### Publish rules (each one is tested)
`NO_DRAFT` → `VERSION_CONFLICT` (wrong draft or active version) → simulation must exist in this sandbox
→ `SIMULATION_STALE` (draft rule, draft revision or active version changed since the simulation) →
`BLOCKING_WARNING` → `UNACKNOWLEDGED_WARNINGS` → atomic swap under a per-sandbox lock, plus an audit entry.

## 3. Synthetic dataset
20,000 opportunities, seed `20261122`, regenerated the same way on every boot (`app/dataset.py`). I fixed the
distributions **before** writing any demo scenario: country skewed toward the US (40%); device mix mobile 58 /
desktop 32 / tablet 7 / ctv 3; placement display 55 / native 30 / video 15 (ctv is mostly video);
40 publishers with Zipf-like traffic, each with a primary vertical (70% of its pages); 60% of pages carry a
tier-2 sub-topic; 8% of pages are unclassified. Category IDs are readable stand-ins shaped like the IAB
Content Taxonomy, **not** official IAB IDs. `GET /api/v1/dataset` returns the actual marginals.

## 4. Architecture

**Prototype:** one FastAPI process holds both planes: in-memory sandboxes (per reviewer, LRU-capped at 500,
2 h TTL), the shared immutable sample, and a cache of matched sets per rule hash. A cold simulation of
20k requests × (active + draft + 3 comparison segments) takes about 70 ms. A single evaluation takes under 1 µs
per segment in-process.

**Production (proposed, not built):**
- **Control plane:** a definitions store (versioned, immutable versions, audit log), approval workflow, and
  simulation workers that replay *sampled real bid-request logs* from an analytics store. It's low QPS and
  strongly consistent.
- **Distribution:** each publish compiles the active segments into an immutable, hashed snapshot and pushes
  it to evaluators (pub/sub or object store with versioned manifests). Rollback = point at the previous snapshot.
- **Serving plane:** evaluators embedded in the ad-serving path load the snapshot into memory and decide
  in-process in microseconds. They make **no synchronous calls to the control plane** and keep serving the
  last good snapshot if the control plane is down. Traces are never returned on the serving path.

The prototype doesn't show RTB-grade throughput and doesn't claim to.

## 5. Security summary (OWASP API Top 10, 2023)
- **API1** (object-level authorisation): all lookups are scoped to the caller's sandbox, and foreign IDs return 404.
- **API2** (authentication): 256-bit random tokens, stored only as SHA-256 digests, with a TTL.
- **API3** (property-level authorisation): request models reject unknown fields (no mass assignment), and traces need `evaluate:trace`.
- **API4** (resource consumption): 16 KB bodies, no chunked bodies, grammar limits, a per-token rate limit (120/min), a simulation rate limit (30/min), a session limit per IP (20/h), a cap on sandboxes, and a cap on stored simulations.
- **API5** (function-level authorisation): a scope check on every route, enforced on the server, so the UI role switch only picks a server-issued token.
- **API6** (sensitive business flows): publish needs a fresh simulation and explicit acknowledgements.
- **API8** (misconfiguration): generic error bodies, no stack traces, security headers, a strict CSP, no CORS, and no third-party scripts (Swagger UI is vendored).
- **API9** (inventory management): versioned `/api/v1`, and one contract that contract tests check against the routes.
- **API7 and API10** (SSRF, unsafe API consumption) don't apply, because the app makes no outbound calls.

**Privacy:** contextual attributes only. No user IDs, cookies, IPs in data, or behavioural profiles.

## 6. Run locally
```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # http://localhost:8000
pytest -q                              # 71 tests
```
**Deploy:** `render.yaml` (Docker, free plan, health check `/healthz`). Any container host works:
`docker build -t segmentlens . && docker run -p 8000:8000 segmentlens`.

## 7. Limitations
- The data is synthetic, so match-rate changes aren't forecasts of impressions, revenue or real reach.
- Overlap is co-eligibility only. Deal priority, floors and pricing are needed to judge cannibalisation.
- State is in memory, and a restart reseeds everything. That's acceptable for a demo but not for production.
- The demo login is intentionally open: anyone can get a sandbox. Production would use corporate SSO with
  service identities (mTLS or workload identity) for evaluators, and four-eyes approval for publishing.
- The free hosting tier sleeps when idle, so the first request after a pause can take up to about a minute.
