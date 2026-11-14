"""T09 offline privacy policy engine (teaching exercise, not legal advice).

Rules live in a versioned YAML file (see policies/t09_policy_v1.yaml). Conditions use a tiny DSL
instead of eval() so rule files cannot execute code:

    {path: transfer.cross_border, op: is_true}
    {path: storage.location, op: matches, value: 'cloud'}      # case-insensitive regex on strings
    {all: [cond, ...]} | {any: [cond, ...]} | {not: cond}
    value may be {param: name} to read policy-level params (e.g. retention ceiling).

Decision precedence HOLD > REWORK > GO. A rule with decision FLAG never changes the decision; it only
adds a flag + needs_legal_review. Fail-safe paths (all => HOLD):
  - case field absent, null when not nullable, or of the wrong type (case_schema) -> EVIDENCE_MISSING
  - case market has no rules                                                       -> MARKET_NOT_COVERED
  - rule not yet effective (effective_date > as_of) -> recorded as UPCOMING note, never a violation
  - as_of earlier than the policy's own effective_date -> PolicyError (refuse to evaluate)
Load-time checks reject rule files with unknown ops, undeclared reason codes, rule paths that are not
in case_schema, non-list markets, invalid regexes and non-numeric params used in numeric comparisons.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

SEVERITY = {"GO": 0, "REWORK": 1, "HOLD": 2}
RULE_DECISIONS = set(SEVERITY) | {"FLAG"}
OPS = {"eq", "ne", "in", "not_in", "is_null", "not_null", "is_true", "not_true",
       "gt", "gte", "lt", "lte", "nonempty", "empty", "matches", "not_matches", "any_matches"}
NUMERIC_OPS = {"gt", "gte", "lt", "lte"}
REGEX_OPS = {"matches", "not_matches", "any_matches"}
SCHEMA_TYPES = {"str", "bool", "number", "dict", "list", "any"}
RULE_REQUIRED = ("id", "markets", "when", "decision", "reason_code", "source")
_MISSING = object()


class PolicyError(ValueError):
    """Rule file is malformed or used outside its validity: refuse to run rather than silently skip rules."""


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


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and not (isinstance(x, float) and math.isnan(x))


def _validate_cond(cond: Any, rid: str, known_paths: set[str], params: dict) -> None:
    if not isinstance(cond, dict) or not cond:
        raise PolicyError(f"rule {rid}: condition must be a non-empty mapping, got {cond!r}")
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list) or not cond[key]:
            raise PolicyError(f"rule {rid}: `{key}` needs a non-empty list")
        for c in cond[key]:
            _validate_cond(c, rid, known_paths, params)
        return
    if "not" in cond:
        _validate_cond(cond["not"], rid, known_paths, params)
        return
    if "path" not in cond or "op" not in cond:
        raise PolicyError(f"rule {rid}: leaf condition needs `path` and `op`: {cond!r}")
    op = cond["op"]
    if op not in OPS:
        raise PolicyError(f"rule {rid}: unknown op {op!r}; allowed {sorted(OPS)}")
    if cond["path"] not in known_paths:
        raise PolicyError(f"rule {rid}: path {cond['path']!r} is not declared in case_schema (typo?)")
    v = cond.get("value")
    if isinstance(v, dict) and set(v) == {"param"}:
        if v["param"] not in params:
            raise PolicyError(f"rule {rid}: unknown param {v['param']!r}")
        v = params[v["param"]]
    if op in NUMERIC_OPS and not _is_number(v):
        raise PolicyError(f"rule {rid}: op {op} needs a numeric value, got {v!r}")
    if op in REGEX_OPS:
        try:
            re.compile(str(v))
        except re.error as e:
            raise PolicyError(f"rule {rid}: invalid regex {v!r} ({e})") from e


def _validate_schema(schema: Any, where: Path) -> list[dict]:
    if not isinstance(schema, list) or not schema:
        raise PolicyError(f"{where}: `case_schema` must be a non-empty list")
    out = []
    for s in schema:
        if not isinstance(s, dict) or "path" not in s:
            raise PolicyError(f"{where}: case_schema entry needs `path`: {s!r}")
        if s.get("type", "any") not in SCHEMA_TYPES:
            raise PolicyError(f"{where}: case_schema {s['path']}: unknown type {s.get('type')!r}")
        out.append({"path": s["path"], "type": s.get("type", "any"), "required": bool(s.get("required", False)),
                    "nullable": bool(s.get("nullable", False)), "min": s.get("min")})
    return out


def load_policy(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"policy file not found: {p}")
    raw = p.read_bytes()
    policy = yaml.safe_load(raw) or {}
    for key in ("policy_version", "effective_date", "rules", "reason_codes", "case_schema"):
        if key not in policy:
            raise PolicyError(f"{p}: missing top-level `{key}`")
    policy["case_schema"] = _validate_schema(policy["case_schema"], p)
    known_paths = {s["path"] for s in policy["case_schema"]}
    params = policy.get("params") or {}
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
        if not isinstance(r["markets"], list) or not r["markets"] or not all(isinstance(m, str) for m in r["markets"]):
            raise PolicyError(f"rule {rid}: `markets` must be a non-empty list of strings, got {r['markets']!r}")
        if r["decision"] not in RULE_DECISIONS:
            raise PolicyError(f"rule {rid}: decision must be GO/HOLD/REWORK/FLAG, got {r['decision']!r}")
        if r["reason_code"] not in known_codes:
            raise PolicyError(f"rule {rid}: reason_code {r['reason_code']!r} not declared in reason_codes")
        if not isinstance(r["source"], dict) or not r["source"].get("doc"):
            raise PolicyError(f"rule {rid}: source.doc is mandatory (every rule must cite its source)")
        r["effective_date"] = _to_date(r.get("effective_date", policy["effective_date"]), f"rule {rid}")
        _validate_cond(r["when"], rid, known_paths, params)
    policy["effective_date"] = _to_date(policy["effective_date"], "policy")
    policy["sha256"] = hashlib.sha256(raw).hexdigest()
    policy["path"] = str(p)
    return policy


# ----------------------------------------------------------------------------- case schema check
def get_path(case: Any, path: str) -> Any:
    cur: Any = case
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _type_ok(x: Any, typ: str) -> bool:
    return {"str": lambda: isinstance(x, str), "bool": lambda: type(x) is bool, "number": lambda: _is_number(x),
            "dict": lambda: isinstance(x, dict), "list": lambda: isinstance(x, list), "any": lambda: True}[typ]()


def check_schema(case: dict, schema: list[dict]) -> tuple[list[str], list[str]]:
    """Return (missing paths, invalid 'path: why' strings). Children of a missing/invalid parent are not reported."""
    missing: list[str] = []
    invalid: list[str] = []
    broken: list[str] = []
    for s in schema:
        path = s["path"]
        if any(path.startswith(b + ".") for b in broken):
            continue
        x = get_path(case, path)
        if x is _MISSING:
            if s["required"]:
                missing.append(path)
                broken.append(path)
            continue
        if x is None:
            if not s["nullable"]:
                invalid.append(f"{path}: null")
                broken.append(path)
            continue
        if not _type_ok(x, s["type"]):
            invalid.append(f"{path}: expected {s['type']}, got {type(x).__name__}")
            broken.append(path)
            continue
        if s["min"] is not None and _is_number(x) and x < s["min"]:
            invalid.append(f"{path}: {x} < min {s['min']}")
    return missing, invalid


# ----------------------------------------------------------------------------- condition evaluation
def _resolve_value(v: Any, params: dict) -> Any:
    if isinstance(v, dict) and set(v) == {"param"}:
        return params[v["param"]]
    return v


def _rx(pattern: Any, x: Any) -> bool:
    return isinstance(x, str) and re.search(str(pattern), x, re.IGNORECASE) is not None


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
    if op == "nonempty":                     # whitespace-only notes still count as written => escalate
        return x not in (None, "", [], {})
    if op == "empty":
        return x in (None, "", [], {})
    if op == "matches":
        return _rx(v, x)
    if op == "not_matches":                  # non-strings never match an allowlist => fail-safe
        return not _rx(v, x)
    if op == "any_matches":
        return isinstance(x, list) and any(_rx(v, e) for e in x)
    if not _is_number(x):
        return False                         # schema check already flags non-numeric values as EVIDENCE_MISSING
    return {"gt": x > v, "gte": x >= v, "lt": x < v, "lte": x <= v}[op]


# ----------------------------------------------------------------------------- case evaluation
@dataclass
class CaseResult:
    case_id: str
    market: str
    decision: str
    reason_codes: list[str]
    fired: list[dict] = field(default_factory=list)       # rules that fired and are in force (HOLD/REWORK/GO)
    flags: list[dict] = field(default_factory=list)       # FLAG rules that fired (no effect on decision)
    upcoming: list[dict] = field(default_factory=list)    # rules that would fire but are not yet effective
    checklist: list[dict] = field(default_factory=list)   # one row per rule considered
    missing_fields: list[str] = field(default_factory=list)
    invalid_fields: list[str] = field(default_factory=list)
    needs_legal_review: bool = False

    @property
    def rule_ids(self) -> list[str]:
        return [r["id"] for r in self.fired]

    @property
    def flag_codes(self) -> list[str]:
        return list(dict.fromkeys(r["reason_code"] for r in self.flags))


def _markets_covered(policy: dict) -> set[str]:
    cov = set(policy.get("default_markets_covered") or [])
    for r in policy["rules"]:
        cov |= {m for m in r["markets"] if m != "*"}
    return cov


def _engine_rule(eid: str, code: str, why: str) -> dict:
    return {"id": eid, "decision": "HOLD", "reason_code": code, "note": why,
            "source": {"doc": "engine fail-safe", "ref": why}, "needs_legal_review": True}


def evaluate_case(case: dict, policy: dict, as_of: date) -> CaseResult:
    if policy["effective_date"] and as_of < policy["effective_date"]:
        raise PolicyError(f"as_of {as_of} is before policy {policy['policy_version']} effective_date "
                          f"{policy['effective_date']}: refusing to evaluate outside the policy's validity")
    cid = str(case.get("case_id", "<unknown>")) if isinstance(case, dict) else "<unknown>"
    market_raw = case.get("market") if isinstance(case, dict) else None
    market = market_raw if isinstance(market_raw, str) else ""
    params = policy.get("params") or {}
    res = CaseResult(case_id=cid, market=market, decision="GO", reason_codes=[])
    engine_flags: list[dict] = []

    res.missing_fields, res.invalid_fields = check_schema(case, policy["case_schema"])
    if res.missing_fields or res.invalid_fields:
        why = "; ".join([f"{m}: absent" for m in res.missing_fields] + res.invalid_fields)
        engine_flags.append(_engine_rule("ENGINE-EVIDENCE", "EVIDENCE_MISSING", why))
    if market not in _markets_covered(policy):
        engine_flags.append(_engine_rule("ENGINE-MARKET", "MARKET_NOT_COVERED", f"no rules for market {market_raw!r}"))

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
        if rule["decision"] == "FLAG":
            res.flags.append(rule)
            res.checklist.append(row | {"status": "FLAGGED"})
            continue
        res.fired.append(rule)
        res.checklist.append(row | {"status": "FIRED"})

    for er in engine_flags:
        res.fired.append(er)
        res.checklist.append({"case_id": cid, "rule_id": er["id"], "markets": "*", "rule_decision": "HOLD",
                              "reason_code": er["reason_code"], "source_doc": "engine fail-safe",
                              "source_ref": er["note"], "version_date": "", "effective_date": "",
                              "confidence": "high", "needs_legal_review": True,
                              "missing_fields": ";".join(res.missing_fields), "status": "FIRED"})

    if res.fired:
        ordered = sorted(res.fired, key=lambda r: -SEVERITY[r["decision"]])
        res.decision = ordered[0]["decision"]
        res.reason_codes = list(dict.fromkeys(r["reason_code"] for r in ordered))
    res.needs_legal_review = any(r.get("needs_legal_review") for r in res.fired + res.upcoming + res.flags)
    return res
