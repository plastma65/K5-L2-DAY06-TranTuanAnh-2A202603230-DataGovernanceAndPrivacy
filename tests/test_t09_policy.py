"""Group tests for T09 policy engine + pipeline (success, failure and edge cases). No ground truth needed."""
from __future__ import annotations

import copy
import json
from datetime import date

import pandas as pd
import pytest

from src.main import main
from src.pipeline import apply_review
from src.t09.policy_engine import PolicyError, evaluate_case, load_policy

POLICY = "policies/t09_policy_v1.yaml"
AS_OF = date(2026, 11, 14)

CLEAN = {
    "case_id": "T-CLEAN", "market": "VN", "purpose": "ADAS model training",
    "lawful_basis": "consent",
    "consent": {"obtained": True, "granular": True, "withdrawable": True, "record_available": True},
    "notice_given": True,
    "storage": {"raw_retained": False, "location": "on_device", "encryption_at_rest": True},
    "transfer": {"cross_border": False, "destination": None, "mechanism": None, "transfer_impact_assessment": False},
    "retention_days": 90, "dpia_done": True, "minors_possible": False,
    "anonymization": "blur_on_edge_before_upload", "ambiguity_notes": "",
}


def case(**changes) -> dict:
    c = copy.deepcopy(CLEAN)
    for k, v in changes.items():
        if "__" in k:
            a, b = k.split("__")
            c[a][b] = v
        else:
            c[k] = v
    return c


@pytest.fixture(scope="module")
def policy():
    return load_policy(POLICY)


# ---------------------------------------------------------------- success paths
def test_clean_case_goes(policy):
    r = evaluate_case(CLEAN, policy, AS_OF)
    assert r.decision == "GO" and r.reason_codes == []


def test_vn_legitimate_interest_is_hold(policy):
    r = evaluate_case(case(lawful_basis="legitimate_interest"), policy, AS_OF)
    assert r.decision == "HOLD" and "NO_LAWFUL_BASIS" in r.reason_codes and "R-VN-01" in r.rule_ids


def test_valid_consent_does_not_cure_vn_cross_border(policy):
    """Failure mode from the brief: legality concluded from consent alone."""
    r = evaluate_case(case(transfer__cross_border=True, transfer__destination="SG", transfer__mechanism="unknown"),
                      policy, AS_OF)
    assert r.decision == "HOLD" and "XBORDER_NO_IMPACT_ASSESSMENT" in r.reason_codes


def test_eu_cross_border_without_mechanism_is_hold(policy):
    r = evaluate_case(case(market="EU", transfer__cross_border=True, transfer__mechanism=None), policy, AS_OF)
    assert r.decision == "HOLD" and "XBORDER_NO_MECHANISM" in r.reason_codes


def test_precedence_hold_over_rework(policy):
    r = evaluate_case(case(retention_days=None, dpia_done=False), policy, AS_OF)
    assert r.decision == "HOLD"
    assert r.reason_codes[0] == "DPIA_MISSING" and "RETENTION_UNBOUNDED" in r.reason_codes


def test_ccpa_has_no_lawful_basis_requirement(policy):
    r = evaluate_case(case(market="US-CA", lawful_basis="n/a", consent__obtained=False), policy, AS_OF)
    assert r.decision == "GO"
    r2 = evaluate_case(case(market="US-CA", lawful_basis="n/a", notice_given=False), policy, AS_OF)
    assert r2.decision == "REWORK" and r2.reason_codes == ["NOTICE_MISSING"]


# ---------------------------------------------------------------- edge / failure paths
def test_ambiguity_note_forces_hold(policy):
    r = evaluate_case(case(ambiguity_notes="Unclear if vendor is joint controller."), policy, AS_OF)
    assert r.decision == "HOLD" and "AMBIGUOUS_NEEDS_LEGAL" in r.reason_codes


def test_missing_field_fails_safe(policy):
    c = case()
    del c["transfer"]
    r = evaluate_case(c, policy, AS_OF)
    assert r.decision == "HOLD" and "EVIDENCE_MISSING" in r.reason_codes and "transfer" in r.missing_fields


def test_unknown_market_fails_safe(policy):
    r = evaluate_case(case(market="BR"), policy, AS_OF)
    assert r.decision == "HOLD" and "MARKET_NOT_COVERED" in r.reason_codes


def test_future_rule_is_note_not_violation(policy):
    eu = case(market="EU")
    now = evaluate_case(eu, policy, AS_OF)
    assert now.decision == "GO" and [x["id"] for x in now.upcoming] == ["R-EU-AIA-01"]
    later = evaluate_case(eu, policy, date(2028, 9, 1))   # Annex I obligations from 2028-08-02
    assert later.decision == "HOLD" and "AI_ACT_HIGH_RISK_UPCOMING" in later.reason_codes


@pytest.mark.parametrize("bad_rule, msg", [
    ({"id": "X", "markets": ["*"], "when": {"path": "a", "op": "eq"}, "decision": "GO", "reason_code": "MINORS"}, "missing fields"),
    ({"id": "X", "markets": ["*"], "when": {"path": "a", "op": "regex"}, "decision": "HOLD", "reason_code": "MINORS",
      "source": {"doc": "d"}}, "unknown op"),
    ({"id": "X", "markets": ["*"], "when": {"path": "a", "op": "is_true"}, "decision": "HOLD", "reason_code": "NOT_DECLARED",
      "source": {"doc": "d"}}, "not declared"),
    ({"id": "X", "markets": ["*"], "when": {"path": "a", "op": "is_true"}, "decision": "MAYBE", "reason_code": "MINORS",
      "source": {"doc": "d"}}, "GO/HOLD/REWORK"),
])
def test_malformed_policy_is_rejected(tmp_path, bad_rule, msg):
    p = tmp_path / "bad.yaml"
    p.write_text(json.dumps({"policy_version": "t", "effective_date": "2026-01-01", "reason_codes": ["MINORS"],
                             "case_schema": [{"path": "a", "type": "any"}], "rules": [bad_rule]}), encoding="utf-8")
    with pytest.raises(PolicyError, match=msg):
        load_policy(p)


# ---------------------------------------------------------------- human review
def _scored():
    return pd.DataFrame({"case_id": ["A", "B", "C"], "decision": ["HOLD", "GO", "REWORK"],
                         "reason_codes": ["DPIA_MISSING", "", "NOTICE_MISSING"],
                         "approval_status": ["pending_legal"] * 3, "reviewer": [""] * 3, "review_note": [""] * 3})


def _reviewed(rows):
    return pd.DataFrame(rows, columns=["item_id", "reviewer", "reviewer_decision", "override_value", "review_note"])


def test_review_override_escalate_and_approve():
    out = apply_review(_scored(), _reviewed([
        ["A", "dpo", "edit", "REWORK", "DPIA filed 2026-11-10, evidence attached"],
        ["B", "dpo", "escalate", "", "need vendor contract"],
        ["C", "dpo", "approve", "", ""],
    ]))
    got = dict(zip(out.case_id, out.decision))
    assert got == {"A": "REWORK", "B": "HOLD", "C": "REWORK"}
    assert "HUMAN_OVERRIDE" in out.loc[0, "reason_codes"] and "ESCALATED_TO_DPO" in out.loc[1, "reason_codes"]
    assert list(out.approval_status) == ["overridden", "escalated", "approved"]


def test_review_override_without_note_is_refused():
    with pytest.raises(ValueError, match="review_note"):
        apply_review(_scored(), _reviewed([["A", "dpo", "edit", "GO", ""]]))


# ---------------------------------------------------------------- end-to-end (offline, fixtures)
def test_e2e_t09_run(tmp_path):
    out = tmp_path / "poc"
    assert main(["--config", "configs/lab.yaml", "--track", "t09", "--output", str(out), "--offline", "--seed", "42"]) == 0
    d = pd.read_csv(out / "decisions.csv")
    assert len(d) == 12 and d.case_id.is_unique and set(d.decision) <= {"GO", "HOLD", "REWORK"}
    for f in ("audit_checklist.csv", "review_queue.csv", "contact_sheet.html", "decision.json", "audit_log.jsonl"):
        assert (out / f).exists(), f
    assert json.loads((out / "decision.json").read_text(encoding="utf-8"))["reason_code"] == "PENDING_LEGAL_APPROVAL"
