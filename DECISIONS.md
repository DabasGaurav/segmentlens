# DECISIONS.md: Media.net APM case, Problem 3 (internal API for targeting segments)

Status: **Gate 1 approved by Gaurav (22 Nov, 00:53 IST), with standing approval for all later gates.** Implemented as described; deltas are in section 13.
Inputs never received: the official PDF, and the part of the master instructions after "Privacy … Auditability". The brief requirements were taken from Gaurav's message.
Brief received: 22 Nov 2026, 00:47 IST. Window: 36 to 48 h, so the target is to submit by **23 Nov, 12:00 IST** with a buffer until 24 Nov, 00:47 IST.

Labels used throughout: **[V]** verified Media.net fact (with source) · **[I]** general AdTech industry practice · **[H]** hypothesis about Media.net's internal workflow (not verified) · **[P]** our proposed design.

---

## 0. What we know about Media.net (sourced, kept short on purpose)

| Fact | Source |
|---|---|
| [V] Describes itself as "a leading global sell-side platform (SSP) at the intersection of publishers, advertisers, and users" | [media.net/aboutus](https://www.media.net/aboutus/) |
| [V] "A leader in contextual advertising technology"; "combines advanced contextual targeting, first-party audience activation with unique ad formats" | [Symitri press release, Jan 2025](https://www.media.net/press/media-net-and-symitri-partnership) |
| [V] Contextual tech "matches user-intent to advertisers ... without using cookies or audience data" | [Unify page (c. 2021)](https://www.media.net/unify/) |
| [V] Advertiser products include a **Curated Marketplace** (audience data plus SearchSignals layered on "vetted premium publisher inventory"), a **Deals Desk** for Deal IDs, **SearchSignals** and **ContextGraph** (search-intent data), and Inventory Insights | [media.net/advertiser](https://www.media.net/advertiser) |
| [V] Publisher side: Unify (header bidding, Google Ad Manager integration, keyword-targeting yield optimisation) | [Unify page](https://www.media.net/unify/) |
| [I] OpenRTB carries content categories on `site.content.cat` and `site.cat`, with `cattax` naming the taxonomy (IAB Content Taxonomy). Device type is an AdCOM enum. Deals arrive in `imp.pmp.deals` | IAB Tech Lab OpenRTB 2.6 / AdCOM 1.0 |
| [H] Internal AdOps or Deals Desk staff maintain the contextual segments that curated deals and packages are built on, and edit them by hand | Not verified. Stated as an assumption in the deck |

What we do **not** claim: Media.net's internal tools, its segment-management process, error rates, or how incidents happen today.

---

## 1. Three wedges compared

**A. Contextual Segment Impact Simulator.** Before publishing a segment change, test the draft against a sample of ad opportunities: reach before and after, flips, overlap, warnings, explanations.

**B. Privacy-aware contextual fallback.** When audience data can't be used, evaluate a segment on contextual signals only.

**C. Shadow-mode version comparison.** Evaluate the same request against the active and draft versions and show which decisions flip.

Scores are 1 to 5 (5 = best). I fixed the criteria before scoring.

| Criterion | A | B | C |
|---|---|---|---|
| Relevance to Media.net (contextual-first SSP, curated deals) | 5 | 3 (Media.net is already contextual-first, so a "fallback" fixes a gap they mostly don't have) | 4 |
| Strength of the internal user problem | 5 (a one-clause edit can silently shrink or widen what a live deal can buy) | 3 | 3 (a mechanism, not a user outcome) |
| Creativity and differentiation from CRUD | 4 | 2 | 3 |
| Measurable business value | 4 (rollbacks, under-delivery incidents, time to publish) | 2 (needs legal and revenue data we can't model) | 3 |
| API-specific product sense | 5 (simulation becomes a contract step that gates publish) | 3 | 4 |
| Technical feasibility in 36 h | 4 | 4 | 5 |
| Security and privacy risk | 4 (contextual and synthetic only) | 2 (easy to over-claim that "no consent means lawful") | 4 |
| Demonstrability | 5 | 2 (hard to show value without real consent signals) | 3 |
| Fits in 6 pages | 5 | 3 | 4 |
| **Total / 45** | **41** | **24** | **33** |

### Recommendation: A, with C as its engine, and one change to your preference

I agree with A plus C. I'd challenge it in two ways that make it sharper rather than replacing it:

1. **Anchor the segment to what it's for.** At an SSP with a Curated Marketplace and a Deals Desk [V], a contextual segment matters because buyers' deals and packages depend on it [H]. So the failure we pitch is specific: *"I widened 'Sports, mobile' and it quietly started selling into entertainment inventory the buyer never agreed to,"* or *"I tightened it and a live deal's avails dropped 40%."* A generic "segment registry" pitch loses to this.
2. **Make the simulation a precondition of publishing, not just a report.** Publishing has to reference a simulation that ran on *exactly this draft version*. If the draft changed after the simulation, publish returns `409 SIMULATION_STALE`. Warnings above a threshold need an explicit acknowledgement plus a reason, which goes in the audit log. That turns "nice dashboard" into an API design decision, and it's the line we lead with.

**Why not B:** we fold B's useful part into A as privacy by design. The attribute allowlist is contextual only, with no user identifiers, so we don't make legal claims we can't back.

---

## 2. Internal user persona [H]

**Programmatic AdOps / Deals Desk specialist** (primary). They set up and maintain the contextual segments behind curated deals for agency and DSP buyers. They're fluent in IAB categories and deal IDs but don't write code. They get blamed when a deal under-delivers or a buyer complains about placements. Today [H], they can't see what a rule edit does until delivery reports come back hours or days later.

Secondary users: ad-serving engineers (they call `/evaluate` and need deterministic, versioned definitions), targeting PMs (audit trail and change history), analytics (reach-change data).

## 3. Problem statement

> When an AdOps specialist edits a contextual targeting segment that live deals depend on, they can't see before publishing which ad opportunities will enter or leave it. A one-clause change can silently cut eligible inventory (deal under-delivery) or widen it into inventory the buyer didn't intend (trust and brand-suitability risk), and the mistake only shows up after delivery data arrives.

## 4. Value proposition

**SegmentLens** (working name; optional): *see the blast radius of a targeting change before it ships.* Simulate the draft against the active version on the same request sample, see exactly which decisions flip and why, and publish only a version that has been simulated, atomically and with version checks.

## 5. MVP scope

**In the MVP (one end-to-end flow, done well):**
1. Five contextual attributes: `content_category` (synthetic IDs modelled on IAB Content Taxonomy, multi-valued), `country` (ISO 3166-1 alpha-2), `device_type` (`mobile|desktop|tablet|ctv`), `placement_type` (`display|native|video`), `publisher_id`.
2. A small rule language: `all` (AND), `any` (OR), and leaves with `eq`, `in`, `not_in`. Max depth 4, max 20 leaves, max 50 values per list, body 16 KB. Validation rejects unknown attributes and operators, wrong types, and values not in the allowlist.
3. A draft version per segment, with optimistic locking.
4. Deterministic evaluation with per-condition traces. Missing attributes **fail closed** (the condition is false, reason `MISSING_ATTRIBUTE`).
5. **Simulation:** active vs draft on a fixed, seeded synthetic dataset (20,000 opportunities). Returns reach, match rates, pp change, relative change, flips in each direction, flip rate, overlap with other active segments, warnings, and up to 20 explained sample flips.
6. **Simulation-gated publish:** requires `simulation_id` and `expected_active_version`, is atomic per segment, and records an audit entry.
7. Three server-issued sandbox identities (viewer, editor, evaluator). Each reviewer gets an **isolated sandbox** that can be reset.
8. A web playground plus Swagger UI on a public URL.

**Nice-to-have (only after the core is deployed and stable):** rollback to the previous version, a redundant-condition detector ("this clause never changes the outcome"), paginated flip explorer, comparison of two drafts, CSV export of flips.

**Out of scope, and why:** real Media.net data or integrations (not available, and inventing them would be dishonest); auction, bidding, revenue forecasting (needs pricing and priority data, so we name them as production data requirements); behavioural audiences and identity graphs (privacy risk and not needed for the problem); ML and natural-language rules (distract from the core and hurt determinism); full IAM, multi-tenant, multi-region, four-eyes approval workflow (described for production, not built).

## 6. Endpoints (6 core + 2 support)

| # | Method and path | Who | Purpose |
|---|---|---|---|
| 1 | `POST /api/v1/demo/sessions` | public, rate-limited | Issues a sandbox and an opaque token for one of the three demo identities |
| 2 | `GET /api/v1/segments` and `GET /api/v1/segments/{id}` | viewer, editor | List segments; detail with the active definition, draft and version history |
| 3 | `PUT /api/v1/segments/{id}/draft` | editor | Create or replace the draft (`If-Match`-style `base_version`) |
| 4 | `POST /api/v1/segments/{id}/simulations` | editor (viewer can read results) | Active vs draft on the sample; stores the result |
| 5 | `POST /api/v1/segments/{id}/publish` | editor | Publishes the simulated draft atomically |
| 6 | `POST /api/v1/evaluate` | evaluator, editor | Evaluates one context against active segments. `debug=true` (trace) needs the `evaluate:trace` scope (editor only) |
| s | `GET /api/v1/dataset` | any | Describes the synthetic sample: size, seed, distributions |
| s | `POST /api/v1/demo/reset` | the session's owner | Resets the sandbox to seed data |

Creating a brand-new segment (`POST /api/v1/segments`) is a nice-to-have. The core story is editing a live segment.

## 7. Data model

- **Segment**: `id`, `name`, `description`, `active_version` (int or null), `draft` (or null), `updated_at`.
- **SegmentVersion** (immutable once published): `segment_id`, `version`, `rule`, `rule_hash` (sha256 of canonical JSON), `published_at`, `published_by`, `change_note`, `acknowledged_warnings[]`, `simulation_id`.
- **Draft**: `segment_id`, `draft_version` (next version number), `base_version`, `rule`, `rule_hash`, `revision`, `updated_by`, `updated_at`.
- **Rule**: `{"all":[...]}` | `{"any":[...]}` | `{"attr","op","value"|"values"}`.
- **AdContext**: `request_id`, `publisher_id`, `country`, `device_type`, `placement_type`, `content_category[]`. Any of these can be missing.
- **Simulation**: `id`, `segment_id`, `active_version`, `draft_version`, `draft_rule_hash`, `dataset{id,seed,size}`, `metrics`, `overlap[]`, `warnings[]`, `sample_flips[]`, `created_at`, `created_by`.
- **AuditEntry**: `at`, `actor`, `action`, `segment_id`, `version`, `details`.

**Metric definitions** (enforced in code and tests):
- Match rate = matched / sample size (each sample request is evaluated once per version).
- pp change = draft match rate − active match rate.
- Relative reach change = (draft matched − active matched) / active matched; `null` when active matched = 0.
- Flip rate = (newly eligible + no longer eligible) / sample size.
- Overlap with segment S = |draft ∩ S| / |draft matched|; `null` when draft matched = 0. Labelled "co-eligibility", **not** cannibalisation.

**Warnings** (thresholds are configurable defaults; severity `block` stops publish, `warn` needs an acknowledgement): `ZERO_MATCH` (block), `REACH_DROP` (relative change ≤ −25%, warn), `REACH_EXPANSION` (≥ +50%, warn), `HIGH_FLIP_RATE` (≥ 10% of sample, warn), `HIGH_OVERLAP` (≥ 80% with an active segment, warn), `MISSING_DATA_EXPOSURE` (≥ 10% of the sample is missing an attribute the draft uses, info).

## 8. Example JSON (all 11)

Shared IDs: segment `seg_sports_mobile`, active v3, draft v4, simulation `sim_01J9Z4K2`, sandbox `sbx_7f3a`.

**(1) Create draft**: `PUT /api/v1/segments/seg_sports_mobile/draft`
```json
{
  "base_version": 3,
  "rule": {
    "all": [
      {"attr": "content_category", "op": "in", "values": ["IAB-SPORTS", "IAB-ENTERTAINMENT"]},
      {"attr": "device_type", "op": "eq", "value": "mobile"},
      {"attr": "country", "op": "in", "values": ["US", "CA"]}
    ]
  },
  "change_note": "Add entertainment for Q4 sports-adjacent package"
}
```
**(2) Create response**: `201 Created`
```json
{
  "segment_id": "seg_sports_mobile",
  "draft_version": 4,
  "base_version": 3,
  "revision": 1,
  "rule_hash": "sha256:9c1e…a07b",
  "status": "draft",
  "validation": {"valid": true, "leaf_count": 3, "max_depth": 2},
  "updated_by": "demo-editor@sbx_7f3a",
  "updated_at": "2026-11-22T09:14:03Z"
}
```
**(3) Simulate request**: `POST /api/v1/segments/seg_sports_mobile/simulations`
```json
{"compare_with": ["seg_entertainment_all"], "max_sample_flips": 5}
```
**(4) Simulate response**: `201 Created` (numbers are illustrative; the real ones are computed)
```json
{
  "simulation_id": "sim_01J9Z4K2",
  "segment_id": "seg_sports_mobile",
  "active_version": 3,
  "draft_version": 4,
  "draft_rule_hash": "sha256:9c1e…a07b",
  "dataset": {"id": "synthetic-v1", "seed": 20261122, "size": 20000, "synthetic": true},
  "metrics": {
    "active_matched": 1180, "draft_matched": 2310,
    "active_match_rate": 0.0590, "draft_match_rate": 0.1155,
    "pp_change": 5.65, "relative_reach_change": 0.9576,
    "newly_eligible": 1130, "no_longer_eligible": 0, "unchanged": 18870,
    "flip_rate": 0.0565
  },
  "overlap": [{"segment_id": "seg_entertainment_all", "active_version": 2, "co_eligible": 1130, "share_of_draft": 0.4892}],
  "warnings": [
    {"code": "REACH_EXPANSION", "severity": "warn", "message": "Draft reach is +95.8% vs active (threshold +50%).", "metric": "relative_reach_change", "value": 0.9576, "threshold": 0.5}
  ],
  "sample_flips": [
    {"request_id": "req_004117", "direction": "newly_eligible",
     "context": {"publisher_id": "pub_102", "country": "US", "device_type": "mobile", "placement_type": "native", "content_category": ["IAB-ENTERTAINMENT", "IAB-ENT-MOVIES"]},
     "active": {"matched": false, "failed": [{"path": "all[0]", "attr": "content_category", "op": "in", "reason": "NO_INTERSECTION"}]},
     "draft": {"matched": true, "failed": []}}
  ],
  "disclaimer": "Synthetic sample. Match-rate changes are not forecasts of impressions, revenue or real reach.",
  "created_at": "2026-11-22T09:14:21Z"
}
```
**(5) Evaluate request**: `POST /api/v1/evaluate?debug=true`
```json
{
  "context": {"request_id": "req_live_1", "publisher_id": "pub_102", "country": "US", "device_type": "mobile", "placement_type": "display", "content_category": ["IAB-SPORTS", "IAB-SPORTS-SOCCER"]},
  "segment_ids": ["seg_sports_mobile"]
}
```
**(6) Match response**: `200 OK`
```json
{
  "request_id": "req_live_1",
  "results": [{
    "segment_id": "seg_sports_mobile", "version": 4, "matched": true,
    "trace": [
      {"path": "all[0]", "attr": "content_category", "op": "in", "passed": true, "reason": "INTERSECTS", "matched_values": ["IAB-SPORTS"]},
      {"path": "all[1]", "attr": "device_type", "op": "eq", "passed": true, "reason": "EQUAL"},
      {"path": "all[2]", "attr": "country", "op": "in", "passed": true, "reason": "IN_SET"}
    ]
  }],
  "evaluated_at": "2026-11-22T09:20:00Z"
}
```
**(7) No-match response**: evaluator token, so no trace (only reason codes)
```json
{
  "request_id": "req_live_2",
  "results": [{"segment_id": "seg_sports_mobile", "version": 4, "matched": false, "reason_codes": ["NOT_EQUAL:device_type", "MISSING_ATTRIBUTE:country"]}],
  "evaluated_at": "2026-11-22T09:20:02Z"
}
```
**(8) Publish request and response**: `POST /api/v1/segments/seg_sports_mobile/publish`
```json
{"draft_version": 4, "expected_active_version": 3, "simulation_id": "sim_01J9Z4K2",
 "acknowledge_warnings": ["REACH_EXPANSION"], "change_note": "Buyer approved entertainment adjacency"}
```
```json
{"segment_id": "seg_sports_mobile", "active_version": 4, "previous_version": 3, "rule_hash": "sha256:9c1e…a07b",
 "published_by": "demo-editor@sbx_7f3a", "published_at": "2026-11-22T09:16:40Z", "audit_id": "aud_0007"}
```
**(9) Validation error**: `422` (all errors in one response use this shape)
```json
{"error": {"code": "VALIDATION_FAILED", "message": "Rule failed validation.", "request_id": "rq_8d1f",
  "details": [
    {"path": "rule.all[1].attr", "code": "UNKNOWN_ATTRIBUTE", "message": "'user_age' is not an allowed attribute."},
    {"path": "rule.all[0].values", "code": "TOO_MANY_VALUES", "message": "Max 50 values, got 64."}
  ]}}
```
**(10) Authorisation error**: `403` (or `401 UNAUTHENTICATED` when the token is missing or invalid)
```json
{"error": {"code": "FORBIDDEN", "message": "Token scope does not include 'segments:publish'.", "request_id": "rq_8d20",
  "details": [{"required_scope": "segments:publish", "identity": "demo-viewer@sbx_7f3a"}]}}
```
**(11) Version conflict**: `409`
```json
{"error": {"code": "VERSION_CONFLICT", "message": "Active version is 4, expected 3. Re-simulate against the current version.", "request_id": "rq_8d21",
  "details": [{"expected_active_version": 3, "current_active_version": 4}]}}
```
The sibling code `SIMULATION_STALE` (`409`) is returned when the draft's `rule_hash` changed after the simulation, and `UNACKNOWLEDGED_WARNINGS` (`409`) when a warn-level warning wasn't acknowledged. `BLOCKING_WARNING` (`409`) is returned for `ZERO_MATCH`.

## 9. Security (mapped to OWASP API Security Top 10, 2023)

- **Identity:** `POST /demo/sessions` returns an opaque 256-bit random token per role, stored server-side with a 2 h TTL. Roles live on the server, so changing a UI dropdown only asks for a different server-issued identity, and every 401 or 403 the playground shows is the real backend response.
- **Scopes (API5, function-level authorisation):** viewer = `segments:read simulations:read`. editor = viewer + `segments:write simulations:run segments:publish evaluate:run evaluate:trace`. evaluator = `evaluate:run` only (it can't read rule definitions or traces). Each route declares its scope.
- **Object level (API1):** every lookup is scoped to the caller's sandbox, so another sandbox's IDs return `404`, never `403` (no existence leak).
- **Property level (API3):** request models forbid extra fields (no mass assignment of `active_version` or `status`). Response models are explicit, and traces are added only with `evaluate:trace`.
- **Resource limits (API4, API6):** 16 KB body cap, depth, leaf and value caps, a fixed sample size, rate limits per token (60 req/min) and per IP for session creation (10/h), at most 500 live sandboxes (LRU eviction), at most 20 simulations stored per sandbox.
- **Rule safety:** a closed grammar parsed into typed models and interpreted by a ~50-line evaluator. No `eval`, no dynamic queries, no regex from user input.
- **Misconfiguration (API8, API9):** a generic error body without stack traces, security headers, CORS limited to our own origin, a versioned `/api/v1`, and the OpenAPI spec served from the running code as the single inventory. API7 (SSRF) and API10 (unsafe consumption) don't apply because the API makes no outbound calls.
- **Privacy:** contextual attributes only, synthetic data, no user IDs, IPs logged only as rate-limit keys. Production controls (approved sources, attribute allowlist governance, consent signals such as TCF / GPP passthrough, audit, retention) go in the deck.

## 10. Stack

Python 3.12 · FastAPI + Pydantic v2 (validation plus generated OpenAPI and Swagger UI) · an in-memory store per sandbox with a per-segment lock for atomic publish (the demo is stateless across restarts by design: everything is reseeded) · NumPy-free pure Python evaluation (20k × 2 evaluations take about 100 ms) · a playground written as one static page of HTML and JS served by the same app · pytest + httpx tests · Docker · hosting on Render or Hugging Face Spaces (see the ask below).

**Control plane vs serving plane (in the deck, not built):** a production control plane (Postgres, versioned definitions, approval workflow, simulation against sampled real bid-request logs in an analytics store) publishes **compiled, immutable snapshots** to a distribution channel. Serving-plane evaluators hold the snapshot in memory and evaluate in the bid path in microseconds, with no synchronous call to the control plane. In the prototype, `/evaluate` runs in the same app and we say so plainly. It doesn't show RTB-grade performance.

## 11. Assumptions and risks

| # | Assumption or risk | Mitigation |
|---|---|---|
| 1 | [H] AdOps hand-edits segments that deals depend on, and the mistakes cost money | Labelled as a hypothesis. The deck's metrics are framed as what we'd measure to confirm it |
| 2 | The synthetic sample isn't real traffic (seasonality, publisher mix) | A documented, neutral distribution, fixed before building the demo scenarios. A disclaimer in every response. Production uses sampled real logs |
| 3 | Overlap gets read as cannibalisation | Labelled "co-eligibility". Deal priority, pricing and floors are listed as production data needs |
| 4 | Media.net may already have this tool internally | Positioned as a proposal and a contract, not a gap claim |
| 5 | Free hosting goes to sleep (cold start) | Keep-alive pings during the review window, or a paid tier for about $7 if you prefer |
| 6 | Reviewers interfere with each other | A sandbox per session plus reset |
| 7 | Time: 36 h | Order of work: thin vertical slice deployed first (Gate 2), nice-to-haves only after Gate 3 |

## 12. Proposed timeline (IST)

Gate 1 now → **Gate 2** (deployed vertical slice): 22 Nov about 14:00 → **Gate 3** (feature-complete and tested): 22 Nov about 22:00 → **Gate 4** (deck plus final link): 23 Nov about 09:00 → submit by 23 Nov 12:00.

## 13. Implementation deltas vs the Gate 1 contract
- `POST /demo/sessions` takes no body. It always creates a new sandbox and returns all three identities at once, so the UI role switch swaps server-issued tokens.
- Added `GET /segments/{id}/simulations/{sim}` to re-read a stored simulation (a viewer can inspect results without running them).
- Chunked request bodies are rejected with `411 LENGTH_REQUIRED`, so the 16 KB cap can't be bypassed.
- `SIMULATION_STALE` also fires when an identical rule is re-saved (a new draft revision). A simulation is bound to one revision, not just the content.
- Evaluate returns 404 for unknown `segment_ids` rather than silently ignoring them.
- Swagger UI is vendored (no CDN), so the CSP allows only `'self'`.

## 14. Success metrics (for the deck)
- **North star: safe-change rate.** The share of segment publishes that were simulated and not rolled back or hot-fixed within 7 days.
- **Business:** segment-change-caused deal under-delivery or brand-suitability incidents per month; mean time to detect a bad change (from days of delivery data down to before publishing); 7-day rollback rate.
- **User (AdOps):** median time from draft to publish; simulations per publish; warning acknowledgement (override) rate, where > 80% means the thresholds are noisy; a self-reported confidence score.
- **Trust or calibration:** after publishing, the correlation between simulated and realised change in matched volume (needs real logs).
- **Guardrails:** serving-path p99 evaluation latency unchanged; snapshot propagation time; no rise in publish lead time beyond agreed SLAs.

## 15. Rollout and experimentation (for the deck)
0. **Backtest (weeks 0–2):** replay the last N months of segment changes against sampled logged requests, and check whether the simulator would have flagged the changes that later caused incidents (recall on past incident tickets).
1. **Shadow (weeks 2–4):** auto-simulate every publish in the existing tool and show it to nobody. Measure predicted vs realised change, and tune the thresholds.
2. **Advisory (weeks 4–8):** one team (e.g. one region's Deals Desk) sees results and warnings, but nothing is blocked.
3. **Gated:** simulation is required to publish. Only `ZERO_MATCH` blocks, and the warn thresholds come from phases 1–2.
- **Experiment design:** you can't randomise per request (segments are shared state), so randomise at the level of **segment changes or specialists**: advisory simulation vs today's flow, then compare 7-day rollback and incident rates. Volume is low, so use a stepped-wedge rollout across teams and pre-register the minimum effect. Kill or rethink if the override rate stays above 80% or lead time regresses.

## Rejected options log
- B as the main wedge (reasons in section 1).
- A generic segment CRUD registry (doesn't differentiate).
- A general `not` operator and numeric ranges (`not_in` covers exclusions, and none of the attributes are numeric).
- JWTs (opaque server-side tokens are easier to revoke and enough for the demo).
- A shared global demo state (reviewers would interfere with each other).
