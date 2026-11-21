"""In-memory sandboxes, server-side identities and rate limiting.

Every reviewer gets an isolated sandbox seeded with the same segments. Tokens are random,
stored only as SHA-256 digests, carry server-held scopes, and expire with the sandbox.
"""
from __future__ import annotations

import copy
import hashlib
import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .rules import rule_hash

SANDBOX_TTL = timedelta(hours=2)
MAX_SANDBOXES = 500
MAX_SIMULATIONS_PER_SANDBOX = 20

ROLE_SCOPES = {
    "viewer": ["segments:read", "simulations:read"],
    "editor": ["segments:read", "simulations:read", "segments:write", "simulations:run",
               "segments:publish", "evaluate:run", "evaluate:trace"],
    "evaluator": ["evaluate:run"],
}


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def new_id(prefix: str, n: int = 8) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(n))


# ----------------------------------------------------------------------------- seed data
SEED_SEGMENTS = [
    {
        "id": "seg_sports_mobile", "name": "Sports · Mobile · North America",
        "description": "Contextual sports inventory on mobile in US/CA. Backs a sports-adjacent curated package.",
        "versions": [
            {"all": [{"attr": "content_category", "op": "in", "values": ["IAB-SPORTS"]},
                     {"attr": "device_type", "op": "eq", "value": "mobile"}]},
            {"all": [{"attr": "content_category", "op": "in", "values": ["IAB-SPORTS"]},
                     {"attr": "device_type", "op": "eq", "value": "mobile"},
                     {"attr": "country", "op": "eq", "value": "US"}]},
            {"all": [{"attr": "content_category", "op": "in", "values": ["IAB-SPORTS"]},
                     {"attr": "device_type", "op": "eq", "value": "mobile"},
                     {"attr": "country", "op": "in", "values": ["US", "CA"]}]},
        ],
    },
    {
        "id": "seg_entertainment_all", "name": "Entertainment · All devices",
        "description": "Broad entertainment content across devices and placements.",
        "versions": [
            {"attr": "content_category", "op": "in", "values": ["IAB-ENT-MOVIES", "IAB-ENT-TV"]},
            {"attr": "content_category", "op": "in", "values": ["IAB-ENTERTAINMENT"]},
        ],
    },
    {
        "id": "seg_finance_us_desktop", "name": "Finance · US · Desktop & tablet",
        "description": "Personal-finance and investing content for large-screen US traffic.",
        "versions": [
            {"all": [{"attr": "content_category", "op": "in", "values": ["IAB-FINANCE"]},
                     {"attr": "device_type", "op": "in", "values": ["desktop", "tablet"]},
                     {"attr": "country", "op": "eq", "value": "US"}]},
        ],
    },
    {
        "id": "seg_premium_video_safe", "name": "Premium video · Brand-suitable",
        "description": "Video placements in four English-speaking markets, excluding political news.",
        "versions": [
            {"all": [{"attr": "placement_type", "op": "eq", "value": "video"},
                     {"attr": "country", "op": "in", "values": ["US", "GB", "CA", "AU"]},
                     {"attr": "content_category", "op": "not_in", "values": ["IAB-NEWS-POLITICS"]}]},
        ],
    },
]


# ----------------------------------------------------------------------------- model
@dataclass
class Sandbox:
    id: str
    created_at: datetime
    expires_at: datetime
    segments: dict = field(default_factory=dict)       # id -> segment dict
    simulations: OrderedDict = field(default_factory=OrderedDict)
    audit: list = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    token_digests: list = field(default_factory=list)
    _audit_seq: int = 0

    def add_audit(self, actor: str, action: str, segment_id: str | None, version: int | None, details: dict) -> str:
        self._audit_seq += 1
        audit_id = f"aud_{self._audit_seq:04d}"
        self.audit.append({"audit_id": audit_id, "at": iso(now()), "actor": actor, "action": action,
                           "segment_id": segment_id, "version": version, "details": details})
        return audit_id

    def reset(self) -> None:
        self.segments = build_seed_segments(self.created_at)
        self.simulations.clear()
        self.audit.clear()
        self._audit_seq = 0


def build_seed_segments(created: datetime) -> dict:
    segs = {}
    base = created - timedelta(days=30)
    for s in SEED_SEGMENTS:
        versions = []
        for i, rule in enumerate(s["versions"], start=1):
            at = base + timedelta(days=7 * i)
            versions.append({"version": i, "rule": copy.deepcopy(rule), "rule_hash": rule_hash(rule),
                             "published_at": iso(at), "published_by": "seed@segmentlens",
                             "change_note": "Seed version", "simulation_id": None, "acknowledged_warnings": []})
        segs[s["id"]] = {"id": s["id"], "name": s["name"], "description": s["description"],
                         "versions": versions, "active_version": len(versions), "draft": None,
                         "updated_at": versions[-1]["published_at"]}
    return segs


@dataclass
class Identity:
    role: str
    principal: str
    scopes: list
    sandbox_id: str
    expires_at: datetime


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sandboxes: OrderedDict[str, Sandbox] = OrderedDict()
        self.tokens: dict[str, Identity] = {}

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create_session(self) -> dict:
        with self._lock:
            self._expire()
            while len(self.sandboxes) >= MAX_SANDBOXES:
                _, old = self.sandboxes.popitem(last=False)
                self._drop_tokens(old)
            created = now()
            sbx = Sandbox(id=new_id("sbx_", 6).lower(), created_at=created, expires_at=created + SANDBOX_TTL)
            sbx.reset()
            self.sandboxes[sbx.id] = sbx
            identities = []
            for role, scopes in ROLE_SCOPES.items():
                token = secrets.token_urlsafe(32)
                d = self.digest(token)
                principal = f"demo-{role}@{sbx.id}"
                self.tokens[d] = Identity(role, principal, list(scopes), sbx.id, sbx.expires_at)
                sbx.token_digests.append(d)
                identities.append({"role": role, "principal": principal, "token": token, "scopes": list(scopes)})
            return {"sandbox_id": sbx.id, "expires_at": iso(sbx.expires_at), "identities": identities}

    def resolve(self, token: str) -> tuple[Identity, Sandbox] | None:
        with self._lock:
            ident = self.tokens.get(self.digest(token))
            if ident is None:
                return None
            sbx = self.sandboxes.get(ident.sandbox_id)
            if sbx is None or ident.expires_at <= now():
                self.tokens.pop(self.digest(token), None)
                return None
            self.sandboxes.move_to_end(sbx.id)
            return ident, sbx

    def _drop_tokens(self, sbx: Sandbox) -> None:
        for d in sbx.token_digests:
            self.tokens.pop(d, None)

    def _expire(self) -> None:
        t = now()
        for sid in [s.id for s in self.sandboxes.values() if s.expires_at <= t]:
            self._drop_tokens(self.sandboxes.pop(sid))


class RateLimiter:
    """Sliding-window limiter keyed by an arbitrary string."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    def check(self, key: str, limit: int, window_s: float) -> float | None:
        """Return None if allowed, else seconds until retry."""
        t = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= t - window_s:
                q.popleft()
            if len(q) >= limit:
                return max(1.0, window_s - (t - q[0]))
            q.append(t)
            if len(self._hits) > 20_000:  # bound memory
                for k in [k for k, v in self._hits.items() if not v][:10_000]:
                    self._hits.pop(k, None)
            return None


registry = Registry()
limiter = RateLimiter()
