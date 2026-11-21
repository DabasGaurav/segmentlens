"""Closed targeting-rule grammar: validation and deterministic evaluation.

Grammar
    Rule  := {"all": [Rule, ...]} | {"any": [Rule, ...]} | Leaf
    Leaf  := {"attr": A, "op": "eq", "value": str}
           | {"attr": A, "op": "in" | "not_in", "values": [str, ...]}

No eval, no regex from input, no dynamic queries: rules are interpreted by the small
functions below. Missing context attributes fail closed (the leaf is false).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .taxonomy import ALLOWED_VALUES, MULTI_VALUED

MAX_DEPTH = 4
MAX_LEAVES = 20
MAX_VALUES = 50
MAX_STR = 64
OPS = ("eq", "in", "not_in")
GROUPS = ("all", "any")


# --------------------------------------------------------------------------- validation
@dataclass
class ValidationResult:
    errors: list[dict] = field(default_factory=list)
    leaf_count: int = 0
    max_depth: int = 0

    @property
    def valid(self) -> bool:
        return not self.errors


def _err(res: ValidationResult, path: str, code: str, message: str) -> None:
    if len(res.errors) < 25:  # bound the error list itself
        res.errors.append({"path": path, "code": code, "message": message})


def validate_rule(rule: Any, path: str = "rule") -> ValidationResult:
    res = ValidationResult()
    _validate(rule, path, 1, res)
    if res.leaf_count > MAX_LEAVES:
        _err(res, path, "TOO_MANY_CONDITIONS", f"Max {MAX_LEAVES} conditions, got {res.leaf_count}.")
    return res


def _validate(node: Any, path: str, depth: int, res: ValidationResult) -> None:
    res.max_depth = max(res.max_depth, depth)
    if depth > MAX_DEPTH:
        _err(res, path, "MAX_DEPTH_EXCEEDED", f"Max nesting depth is {MAX_DEPTH}.")
        return
    if not isinstance(node, dict):
        _err(res, path, "INVALID_NODE", "Each rule node must be a JSON object.")
        return
    group_keys = [k for k in GROUPS if k in node]
    if group_keys:
        if len(node) != 1:
            _err(res, path, "INVALID_NODE", "A group node must contain exactly one key: 'all' or 'any'.")
            return
        key = group_keys[0]
        children = node[key]
        if not isinstance(children, list) or not children:
            _err(res, f"{path}.{key}", "INVALID_GROUP", f"'{key}' must be a non-empty array.")
            return
        if len(children) > MAX_LEAVES:
            _err(res, f"{path}.{key}", "TOO_MANY_CONDITIONS", f"Max {MAX_LEAVES} children per group.")
            return
        for i, child in enumerate(children):
            _validate(child, f"{path}.{key}[{i}]", depth + 1, res)
            if res.leaf_count > MAX_LEAVES:
                return
        return
    _validate_leaf(node, path, res)


def _validate_leaf(node: dict, path: str, res: ValidationResult) -> None:
    res.leaf_count += 1
    allowed_keys = {"attr", "op", "value", "values"}
    extra = set(node) - allowed_keys
    if extra:
        _err(res, path, "UNKNOWN_FIELD", f"Unknown field(s): {sorted(extra)}.")
        return
    attr, op = node.get("attr"), node.get("op")
    if attr not in ALLOWED_VALUES:
        _err(res, f"{path}.attr", "UNKNOWN_ATTRIBUTE",
             f"{json.dumps(attr)[:MAX_STR]} is not an allowed attribute. Allowed: {sorted(ALLOWED_VALUES)}.")
        return
    if op not in OPS:
        _err(res, f"{path}.op", "UNSUPPORTED_OPERATOR", f"{json.dumps(op)[:MAX_STR]} is not supported. Use one of {list(OPS)}.")
        return
    allowed = ALLOWED_VALUES[attr]
    if op == "eq":
        if "values" in node:
            _err(res, f"{path}.values", "INVALID_OPERAND", "'eq' takes 'value', not 'values'.")
            return
        v = node.get("value")
        if not isinstance(v, str) or not v or len(v) > MAX_STR:
            _err(res, f"{path}.value", "INVALID_TYPE", "'value' must be a non-empty string (max 64 chars).")
            return
        if v not in allowed:
            _err(res, f"{path}.value", "UNKNOWN_VALUE", f"'{v}' is not a known {attr}.")
        return
    if "value" in node:
        _err(res, f"{path}.value", "INVALID_OPERAND", f"'{op}' takes 'values', not 'value'.")
        return
    vals = node.get("values")
    if not isinstance(vals, list) or not vals:
        _err(res, f"{path}.values", "INVALID_TYPE", "'values' must be a non-empty array of strings.")
        return
    if len(vals) > MAX_VALUES:
        _err(res, f"{path}.values", "TOO_MANY_VALUES", f"Max {MAX_VALUES} values, got {len(vals)}.")
        return
    seen = set()
    for i, v in enumerate(vals):
        if not isinstance(v, str) or not v or len(v) > MAX_STR:
            _err(res, f"{path}.values[{i}]", "INVALID_TYPE", "Each value must be a non-empty string (max 64 chars).")
            return
        if v in seen:
            _err(res, f"{path}.values[{i}]", "DUPLICATE_VALUE", f"'{v}' appears more than once.")
            return
        seen.add(v)
        if v not in allowed:
            _err(res, f"{path}.values[{i}]", "UNKNOWN_VALUE", f"'{v}' is not a known {attr}.")


def canonical(rule: Any) -> str:
    return json.dumps(rule, sort_keys=True, separators=(",", ":"))


def rule_hash(rule: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(rule).encode()).hexdigest()


def attributes_used(rule: dict) -> set[str]:
    for key in GROUPS:
        if key in rule:
            out: set[str] = set()
            for c in rule[key]:
                out |= attributes_used(c)
            return out
    return {rule["attr"]}


# --------------------------------------------------------------------------- evaluation
def eval_leaf(leaf: dict, ctx: dict) -> tuple[bool, str, list[str]]:
    """Return (passed, reason_code, matched_values)."""
    attr, op = leaf["attr"], leaf["op"]
    raw = ctx.get(attr)
    if attr in MULTI_VALUED:
        present = list(raw) if raw else []
        if not present:
            return False, "MISSING_ATTRIBUTE", []
        if op == "eq":
            ok = leaf["value"] in present
            return ok, ("EQUAL" if ok else "NOT_EQUAL"), ([leaf["value"]] if ok else [])
        hits = [v for v in leaf["values"] if v in present]
        if op == "in":
            return bool(hits), ("INTERSECTS" if hits else "NO_INTERSECTION"), hits
        return (not hits), ("EXCLUDED" if hits else "NOT_EXCLUDED"), hits
    if raw is None or raw == "":
        return False, "MISSING_ATTRIBUTE", []
    if op == "eq":
        ok = raw == leaf["value"]
        return ok, ("EQUAL" if ok else "NOT_EQUAL"), ([raw] if ok else [])
    inside = raw in leaf["values"]
    if op == "in":
        return inside, ("IN_SET" if inside else "NOT_IN_SET"), ([raw] if inside else [])
    return (not inside), ("EXCLUDED" if inside else "NOT_EXCLUDED"), ([raw] if inside else [])


def matches(rule: dict, ctx: dict) -> bool:
    """Fast path used by simulation (short-circuits, no trace)."""
    if "all" in rule:
        return all(matches(c, ctx) for c in rule["all"])
    if "any" in rule:
        return any(matches(c, ctx) for c in rule["any"])
    return eval_leaf(rule, ctx)[0]


def explain(rule: dict, ctx: dict, path: str = "") -> tuple[bool, list[dict], list[dict]]:
    """Full evaluation without short-circuit.

    Returns (matched, trace, decisive_failures). `trace` lists every leaf. `decisive_failures`
    lists only the failed leaves that caused a failing group to fail (for an `any` group, all
    of its failed children; for a passing group, none).
    """
    for key in GROUPS:
        if key in rule:
            trace: list[dict] = []
            child_results = []
            for i, c in enumerate(rule[key]):
                p = f"{path}.{key}[{i}]" if path else f"{key}[{i}]"
                m, t, f = explain(c, ctx, p)
                trace.extend(t)
                child_results.append((m, f))
            matched = all(m for m, _ in child_results) if key == "all" else any(m for m, _ in child_results)
            failures: list[dict] = []
            if not matched:
                for m, f in child_results:
                    if not m:
                        failures.extend(f)
            return matched, trace, failures
    passed, reason, hits = eval_leaf(rule, ctx)
    entry = {"path": path or "rule", "attr": rule["attr"], "op": rule["op"], "passed": passed, "reason": reason}
    if hits:
        entry["matched_values"] = hits
    return passed, [entry], ([] if passed else [entry])


def reason_codes(failures: list[dict]) -> list[str]:
    out: list[str] = []
    for f in failures:
        code = f"{f['reason']}:{f['attr']}"
        if code not in out:
            out.append(code)
    return out


def describe(rule: dict) -> str:
    """Human-readable rendering used by the playground and audit log."""
    if "all" in rule:
        return "(" + " AND ".join(describe(c) for c in rule["all"]) + ")"
    if "any" in rule:
        return "(" + " OR ".join(describe(c) for c in rule["any"]) + ")"
    if rule["op"] == "eq":
        return f"{rule['attr']} = {rule['value']}"
    sym = "in" if rule["op"] == "in" else "not in"
    return f"{rule['attr']} {sym} {{{', '.join(rule['values'])}}}"
