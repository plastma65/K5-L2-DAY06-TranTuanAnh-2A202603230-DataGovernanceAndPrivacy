"""T09 offline privacy policy engine (teaching exercise, not legal advice).

Rules live in a versioned YAML file (see policies/t09_policy_v1.yaml). Conditions use a tiny DSL
instead of eval() so rule files cannot execute code:

    {path: transfer.cross_border, op: is_true}
    {all: [cond, ...]} | {any: [cond, ...]} | {not: cond}
    value may be {param: name} to read policy-level params (e.g. retention ceiling).

Decision precedence HOLD > REWORK > GO. Fail-safe paths (all => HOLD):
  - case misses a required field          -> EVIDENCE_MISSING
  - case market has no rules              -> MARKET_NOT_COVERED
  - rule not yet effective (effective_date > as_of) -> recorded as UPCOMING note, never a violation
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

SEVERITY = {"GO": 0, "REWORK": 1, "HOLD": 2}
OPS = {"eq", "ne", "in", "not_in", "is_null", "not_null", "is_true", "not_true",
       "gt", "gte", "lt", "lte", "nonempty", "empty"}
RULE_REQUIRED = ("id", "markets", "when", "decision", "reason_code", "source")
_MISSING = object()


class PolicyError(ValueError):
    """Rule file is malformed: refuse to run rather than silently skip rules."""


# ----------------------------------------------------------------------------- loading / validation
def _to_date(v: Any, what: str) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError as e:
        raise PolicyError(f"{what}: invalid date {v!r}") from e


def _validate_cond(cond: Any, rid: str) -> None:
    if not isinstance(cond, dict) or not cond:
        raise PolicyError(f"rule {rid}: condition must be a non-empty mapping, got {cond!r}")
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list) or not cond[key]:
            raise PolicyError(f"rule {rid}: `{key}` needs a non-empty list")
        for c in cond[key]:
            _validate_cond(c, rid)
        return
    if "not" in cond:
        _validate_cond(cond["not"], rid)
        return
    if "path" not in cond or "op" not in cond:
        raise PolicyError(f"rule {rid}: leaf condition needs `path` and `op`: {cond!r}")
    if cond["op"] not in OPS:
        raise PolicyError(f"rule {rid}: unknown op {cond['op']!r}; allowed {sorted(OPS)}")


def load_policy(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"policy file not found: {p}")
    raw = p.read_bytes()
    policy = yaml.safe_load(raw) or {}
    for key in ("policy_version", "effective_date", "rules", "reason_codes"):
        if key not in policy:
            raise PolicyError(f"{p}: missing top-level `{key}`")
    known_codes = set(policy["reason_codes"])
    seen: set[str] = set()
    for r in policy["rules"]:
        rid = r.get("id", "<no id>")
        miss = [k for k in RULE_REQUIRED if k not in r]
        if miss:
            raise PolicyError(f"rule {rid}: missing fields {miss}")
        if rid in seen:
            raise PolicyError(f"duplicate rule id {rid}")
        seen.add(rid)
        if r["decision"] not in SEVERITY:
            raise PolicyError(f"rule {rid}: decision must be GO/HOLD/REWORK, got {r['decision']!r}")
        if r["reason_code"] not in known_codes:
            raise PolicyError(f"rule {rid}: reason_code {r['reason_code']!r} not declared in reason_codes")
        if not isinstance(r["source"], dict) or not r["source"].get("doc"):
            raise PolicyError(f"rule {rid}: source.doc is mandatory (every rule must cite its source)")
        r["effective_date"] = _to_date(r.get("effective_date", policy["effective_date"]), f"rule {rid}")
        _validate_cond(r["when"], rid)
    policy["effective_date"] = _to_date(policy["effective_date"], "policy")
    policy["sha256"] = hashlib.sha256(raw).hexdigest()
    policy["path"] = str(p)
    return policy


# ----------------------------------------------------------------------------- condition evaluation
def get_path(case: dict, path: str) -> Any:
    cur: Any = case
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _resolve_value(v: Any, params: dict) -> Any:
    if isinstance(v, dict) and set(v) == {"param"}:
        if v["param"] not in params:
            raise PolicyError(f"condition references unknown param {v['param']!r}")
        return params[v["param"]]
    return v


def eval_cond(cond: dict, case: dict, params: dict, missing: set[str]) -> bool:
    if "all" in cond:
        return all(eval_cond(c, case, params, missing) for c in cond["all"])
    if "any" in cond:
        return any(eval_cond(c, case, params, missing) for c in cond["any"])
    if "not" in cond:
        return not eval_cond(cond["not"], case, params, missing)
    raw = get_path(case, cond["path"])
    if raw is _MISSING:
        missing.add(cond["path"])
    x = None if raw is _MISSING else raw
    op, v = cond["op"], _resolve_value(cond.get("value"), params)
    if op == "eq":
        return x == v
    if op == "ne":
        return x != v
    if op == "in":
        return x in (v or [])
    if op == "not_in":
        return x not in (v or [])
    if op == "is_null":
        return x is None
    if op == "not_null":
        return x is not None
    if op == "is_true":
        return x is True
    if op == "not_true":
        return x is not True
    if op == "nonempty":
        return x not in (None, "", [], {})
    if op == "empty":
        return x in (None, "", [], {})
    if x is None or not isinstance(x, (int, float)):
        return False                       # numeric comparison on null never fires (null handled by is_null rules)
    return {"gt": x > v, "gte": x >= v, "lt": x < v, "lte": x <= v}[op]


# ----------------------------------------------------------------------------- case evaluation
@dataclass
class CaseResult:
    case_id: str
    market: str
    decision: str
    reason_codes: list[str]
    fired: list[dict] = field(default_factory=list)       # rules that fired and are in force
    upcoming: list[dict] = field(default_factory=list)    # rules that would fire but are not yet effective
    checklist: list[dict] = field(default_factory=list)   # one row per rule considered
    missing_fields: list[str] = field(default_factory=list)
    needs_legal_review: bool = False

    @property
    def rule_ids(self) -> list[str]:
        return [r["id"] for r in self.fired]


def _markets_covered(policy: dict) -> set[str]:
    cov = set(policy.get("default_markets_covered") or [])
    for r in policy["rules"]:
        cov |= {m for m in r["markets"] if m != "*"}
    return cov


def evaluate_case(case: dict, policy: dict, as_of: date) -> CaseResult:
    cid = str(case.get("case_id", "<unknown>"))
    market = str(case.get("market", ""))
    params = policy.get("params") or {}
    res = CaseResult(case_id=cid, market=market, decision="GO", reason_codes=[])
    engine_flags: list[tuple[str, str, str]] = []

    absent = [f for f in policy.get("required_case_fields", []) if f not in case]
    if absent:
        res.missing_fields = absent
        engine_flags.append(("ENGINE-EVIDENCE", "EVIDENCE_MISSING", f"required fields absent: {absent}"))
    if market not in _markets_covered(policy):
        engine_flags.append(("ENGINE-MARKET", "MARKET_NOT_COVERED", f"no rules for market {market!r}"))

    for rule in policy["rules"]:
        row = {"case_id": cid, "rule_id": rule["id"], "markets": ",".join(rule["markets"]),
               "rule_decision": rule["decision"], "reason_code": rule["reason_code"],
               "source_doc": rule["source"].get("doc", ""), "source_ref": rule["source"].get("ref", ""),
               "version_date": str(rule.get("version_date", "")), "effective_date": str(rule["effective_date"]),
               "confidence": rule.get("confidence", ""), "needs_legal_review": bool(rule.get("needs_legal_review", False)),
               "missing_fields": ""}
        if "*" not in rule["markets"] and market not in rule["markets"]:
            res.checklist.append(row | {"status": "NOT_APPLICABLE"})
            continue
        missing: set[str] = set()
        hit = eval_cond(rule["when"], case, params, missing)
        row["missing_fields"] = ";".join(sorted(missing))
        if not hit:
            res.checklist.append(row | {"status": "PASSED"})
            continue
        if rule["effective_date"] and rule["effective_date"] > as_of:
            res.upcoming.append(rule)
            res.checklist.append(row | {"status": "UPCOMING"})
            continue
        res.fired.append(rule)
        res.checklist.append(row | {"status": "FIRED"})

    for eid, code, why in engine_flags:
        res.fired.append({"id": eid, "decision": "HOLD", "reason_code": code, "note": why,
                          "source": {"doc": "engine fail-safe"}, "needs_legal_review": True})
        res.checklist.append({"case_id": cid, "rule_id": eid, "markets": "*", "rule_decision": "HOLD",
                              "reason_code": code, "source_doc": "engine fail-safe", "source_ref": why,
                              "version_date": "", "effective_date": "", "confidence": "high",
                              "needs_legal_review": True, "missing_fields": ";".join(res.missing_fields),
                              "status": "FIRED"})

    if res.fired:
        ordered = sorted(res.fired, key=lambda r: -SEVERITY[r["decision"]])
        res.decision = ordered[0]["decision"]
        codes: list[str] = []
        for r in ordered:
            if r["reason_code"] not in codes:
                codes.append(r["reason_code"])
        res.reason_codes = codes
    res.needs_legal_review = any(r.get("needs_legal_review") for r in res.fired + res.upcoming)
    return res
