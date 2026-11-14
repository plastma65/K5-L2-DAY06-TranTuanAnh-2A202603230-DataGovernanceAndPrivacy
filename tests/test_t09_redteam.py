"""Red-team tests for the T09 policy engine + human-review step (teaching exercise, not legal advice).

Convention
----------
* Tests WITHOUT a marker pin behaviour that is already correct (fail-safe paths that must not regress).
* Tests marked ``xfail(strict=True, reason="RT-xx: ...")`` are open findings, documented in
  ``reports/redteam_t09.md``. When a finding is fixed the test XPASSes, strict mode turns the suite red,
  and the marker must be removed in the fixing commit ("test fail -> fix" pair).

Only synthetic cases built in this file and the dev split were used; eval cases (CASE-09..12) were not read.
"""
from __future__ import annotations

import copy
from datetime import date

import pandas as pd
import pytest
import yaml

from src.common.review import REVIEW_COLUMNS, load_review_decisions
from src.pipeline import apply_review
from src.t09.policy_engine import PolicyError, evaluate_case, load_policy

POLICY = "policies/t09_policy_v1.yaml"
AS_OF = date(2026, 11, 14)

CLEAN = {
    "case_id": "RT-CLEAN", "market": "VN", "purpose": "ADAS model training",
    "data_types": ["exterior_video"], "data_subjects": ["pedestrians", "other_drivers"],
    "lawful_basis": "consent",
    "consent": {"obtained": True, "granular": True, "withdrawable": True, "record_available": True},
    "notice_given": True,
    "storage": {"raw_retained": False, "location": "on_device", "encryption_at_rest": True},
    "transfer": {"cross_border": False, "destination": None, "mechanism": None, "transfer_impact_assessment": False},
    "retention_days": 90, "dpia_done": True, "minors_possible": False,
    "anonymization": "blur_on_edge_before_upload", "ambiguity_notes": "",
}


def case(**changes) -> dict:
    """CLEAN with overrides; ``a__b=v`` sets the nested key ``case[a][b]``."""
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


def decide(c: dict, policy: dict, as_of: date = AS_OF):
    return evaluate_case(c, policy, as_of)


def _tmp_policy(tmp_path, mutate) -> str:
    """Copy of the group policy with one mutation applied (the real file is never modified)."""
    with open(POLICY, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    mutate(raw)
    p = tmp_path / "policy_mut.yaml"
    p.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(p)


# =============================================================================
# A. Behaviour that is already correct — regression guards (no xfail)
# =============================================================================
def test_sanity_clean_case_is_go(policy):
    assert decide(CLEAN, policy).decision == "GO"


@pytest.mark.parametrize("market", ["vn", "EU ", " eu", "", None, ["EU"], "BR"])
def test_market_variants_fail_safe_to_hold(policy, market):
    r = decide(case(market=market), policy)
    assert r.decision == "HOLD" and "MARKET_NOT_COVERED" in r.reason_codes


@pytest.mark.parametrize("basis", ["Consent", " consent", "CONSENT", None, "", "unknown", "n/a"])
@pytest.mark.parametrize("market", ["VN", "EU", "SG"])
def test_unrecognised_lawful_basis_is_hold(policy, market, basis):
    r = decide(case(market=market, lawful_basis=basis), policy)
    assert r.decision == "HOLD" and "NO_LAWFUL_BASIS" in r.reason_codes


@pytest.mark.parametrize("changes, code", [
    ({"dpia_done": "true"}, "DPIA_MISSING"),
    ({"dpia_done": None}, "DPIA_MISSING"),
    ({"consent__obtained": "true"}, "NO_LAWFUL_BASIS"),
    ({"consent": None}, "NO_LAWFUL_BASIS"),
    ({"consent": {}}, "NO_LAWFUL_BASIS"),
    ({"consent": "yes"}, "NO_LAWFUL_BASIS"),
])
def test_not_true_checks_fail_safe_on_non_boolean(policy, changes, code):
    """`not_true` rules treat "true"/None/garbage as NOT satisfied -> HOLD (good)."""
    r = decide(case(**changes), policy)
    assert r.decision == "HOLD" and code in r.reason_codes


def test_vn_cross_border_string_tia_is_hold(policy):
    r = decide(case(transfer__cross_border=True, transfer__destination="SG", transfer__mechanism="SCC",
                    transfer__transfer_impact_assessment="yes"), policy)
    assert r.decision == "HOLD" and "XBORDER_NO_IMPACT_ASSESSMENT" in r.reason_codes


def test_vn_mechanism_does_not_replace_impact_assessment(policy):
    """A contractual mechanism (SCC) + valid consent still needs the VN transfer impact-assessment dossier."""
    r = decide(case(transfer__cross_border=True, transfer__destination="SG", transfer__mechanism="SCC"), policy)
    assert r.decision == "HOLD" and "XBORDER_NO_IMPACT_ASSESSMENT" in r.reason_codes


def test_retention_boundary(policy):
    assert decide(case(retention_days=365), policy).decision == "GO"
    r = decide(case(retention_days=366), policy)
    assert r.decision == "REWORK" and r.reason_codes == ["RETENTION_EXCESSIVE"]
    assert "RETENTION_EXCESSIVE" in decide(case(retention_days=float("inf")), policy).reason_codes


def test_storage_null_is_not_go(policy):
    # policy 1.1: null storage block = no evidence -> HOLD EVIDENCE_MISSING, consistent with RT-02 (null transfer).
    r = decide(case(storage=None), policy)
    assert r.decision == "HOLD" and {"EVIDENCE_MISSING", "NO_ENCRYPTION"} <= set(r.reason_codes)


def test_empty_case_is_hold(policy):
    r = decide({}, policy)
    assert r.decision == "HOLD" and {"EVIDENCE_MISSING", "MARKET_NOT_COVERED"} <= set(r.reason_codes)


@pytest.mark.parametrize("notes", ["   ", ["vendor role unclear"], {"q": "joint controller?"}])
def test_ambiguity_notes_any_non_empty_shape_is_hold(policy, notes):
    r = decide(case(ambiguity_notes=notes), policy)
    assert r.decision == "HOLD" and "AMBIGUOUS_NEEDS_LEGAL" in r.reason_codes


def test_us_ca_consent_flag_alone_never_changes_outcome(policy):
    """CCPA has no lawful-basis concept: flipping consent must not turn a notice gap into GO."""
    r = decide(case(market="US-CA", lawful_basis="consent", notice_given=False), policy)
    assert r.decision == "REWORK" and r.reason_codes == ["NOTICE_MISSING"]


# ---- human review: guards that already work
def _scored():
    return pd.DataFrame({"case_id": ["A", "B", "C"], "decision": ["HOLD", "GO", "REWORK"],
                         "reason_codes": ["DPIA_MISSING", "", "NOTICE_MISSING"],
                         "approval_status": ["pending_legal"] * 3, "reviewer": [""] * 3, "review_note": [""] * 3})


def _reviewed(rows):
    return pd.DataFrame(rows, columns=["item_id", "reviewer", "reviewer_decision", "override_value", "review_note"])


def test_review_whitespace_note_is_refused():
    with pytest.raises(ValueError, match="review_note"):
        apply_review(_scored(), _reviewed([["A", "dpo", "edit", "GO", "   "]]))


def test_review_unknown_item_is_refused():
    with pytest.raises(ValueError, match="unknown case"):
        apply_review(_scored(), _reviewed([["CASE-99", "dpo", "approve", "", ""]]))


@pytest.mark.parametrize("ov", ["MAYBE", "go!", "yes", "GO;HOLD"])
def test_review_unknown_override_value_is_refused(ov):
    with pytest.raises(ValueError):
        apply_review(_scored(), _reviewed([["A", "dpo", "edit", ov, "some justification"]]))


def test_review_edit_without_override_value_is_refused():
    with pytest.raises(ValueError):
        apply_review(_scored(), _reviewed([["A", "dpo", "edit", "", "note"]]))


def test_review_unknown_reviewer_decision_is_refused():
    with pytest.raises(ValueError):
        apply_review(_scored(), _reviewed([["A", "dpo", "rubber-stamp", "GO", "note"]]))


def test_review_escalate_and_bare_reject_become_hold():
    out = apply_review(_scored(), _reviewed([["B", "dpo", "escalate", "", ""], ["C", "dpo", "reject", "", ""]]))
    assert dict(zip(out.case_id, out.decision)) == {"A": "HOLD", "B": "HOLD", "C": "HOLD"}


def test_review_csv_loader_rejects_empty_reviewer(tmp_path):
    """The pipeline path (CSV -> load_review_decisions) does enforce a named reviewer."""
    row = {c: "" for c in REVIEW_COLUMNS} | {"item_id": "A", "reviewer_decision": "edit", "override_value": "GO",
                                              "review_note": "ok"}
    p = tmp_path / "review_queue_reviewed.csv"
    pd.DataFrame([row]).to_csv(p, index=False)
    with pytest.raises(ValueError, match="reviewer"):
        load_review_decisions(p)


# =============================================================================
# B. Open findings — CRITICAL (false GO on a risky case)
# =============================================================================
@pytest.mark.parametrize("market", ["EU", "SG"])
@pytest.mark.parametrize("mechanism", ["verbal_agreement", "n/a", "None", "Unknown", "TBD", "pending", 0, False, []])
def test_rt01_unrecognised_transfer_mechanism_is_not_go(policy, market, mechanism):
    r = decide(case(market=market, transfer__cross_border=True, transfer__destination="non-adequate third country",
                    transfer__mechanism=mechanism), policy)
    assert r.decision == "HOLD" and "XBORDER_NO_MECHANISM" in r.reason_codes


@pytest.mark.parametrize("market", ["EU", "VN", "SG", "US-CA"])
@pytest.mark.parametrize("transfer", [None, {}, {"destination": "US"}, "unknown"])
def test_rt02_missing_nested_transfer_evidence_is_hold(policy, market, transfer):
    r = decide(case(market=market, transfer=transfer), policy)
    assert r.decision == "HOLD" and "EVIDENCE_MISSING" in r.reason_codes


@pytest.mark.parametrize("market", ["EU", "VN", "SG"])
@pytest.mark.parametrize("flag", ["true", "True", "yes", 1, None])
def test_rt03_non_boolean_cross_border_is_not_go(policy, market, flag):
    r = decide(case(market=market, transfer__cross_border=flag, transfer__destination="US"), policy)
    assert r.decision == "HOLD"


@pytest.mark.parametrize("market", ["EU", "VN", "SG", "US-CA"])
@pytest.mark.parametrize("flag", ["true", "yes", 1, None, "unknown"])
def test_rt04_non_boolean_minors_flag_is_hold(policy, market, flag):
    r = decide(case(market=market, minors_possible=flag), policy)
    assert r.decision == "HOLD"


def test_rt05_as_of_before_policy_effective_date_is_not_go(policy):
    risky = case(market="EU", lawful_basis="unknown", dpia_done=False, minors_possible=True,
                 transfer__cross_border=True, transfer__mechanism=None, ambiguity_notes="vendor role unclear")
    try:
        r = decide(risky, policy, date(2025, 12, 31))
    except (PolicyError, ValueError):
        return                                   # refusing to evaluate outside the policy's validity is also fine
    assert r.decision == "HOLD"


# =============================================================================
# C. Open findings — MAJOR
# =============================================================================
@pytest.mark.parametrize("value", ["9999", "", "forever", "90 days", float("nan"), [], {}])
def test_rt06_non_numeric_retention_is_not_go(policy, value):
    assert decide(case(retention_days=value), policy).decision != "GO"


@pytest.mark.parametrize("value", [-1, -365, True, False])
def test_rt07_impossible_retention_is_not_go(policy, value):
    assert decide(case(retention_days=value), policy).decision != "GO"


@pytest.mark.parametrize("changes", [
    {"storage__raw_retained": True, "storage__location": "cloud", "anonymization": "None"},
    {"storage__raw_retained": True, "storage__location": "cloud", "anonymization": "no"},
    {"storage__raw_retained": True, "storage__location": "cloud", "anonymization": []},
    {"storage__raw_retained": True, "storage__location": "Cloud", "anonymization": "none"},
    {"storage__raw_retained": True, "storage__location": "cloud_us-east", "anonymization": "none"},
    {"storage__raw_retained": "true", "storage__location": "cloud", "anonymization": "none"},
    {"storage__raw_retained": None, "storage__location": "cloud", "anonymization": "none"},
])
def test_rt08_raw_pii_in_cloud_variants_are_not_go(policy, changes):
    assert decide(case(**changes), policy).decision != "GO"


@pytest.mark.parametrize("changes", [{"consent__granular": False}, {"consent__withdrawable": False}])
def test_rt09_sg_invalid_consent_is_not_go(policy, changes):
    r = decide(case(market="SG", **changes), policy)
    assert r.decision != "GO" and "CONSENT_INVALID" in r.reason_codes


@pytest.mark.parametrize("subjects", [["children"], ["pedestrians", "minors"], ["school_children", "other_drivers"]])
def test_rt10_children_in_data_subjects_is_hold(policy, subjects):
    assert decide(case(data_subjects=subjects, minors_possible=False), policy).decision == "HOLD"


@pytest.mark.parametrize("market", ["VN", "SG", "US-CA"])
def test_rt11_sensitive_data_types_are_at_least_flagged(policy, market):
    r = decide(case(market=market, data_types=["face", "in_cabin_video", "biometric_template"],
                    data_subjects=["drivers", "passengers"]), policy)
    assert r.decision != "GO" or r.needs_legal_review


@pytest.mark.parametrize("market, transfer", [
    ("EU", {"cross_border": True, "destination": None, "mechanism": "SCC", "transfer_impact_assessment": True}),
    ("SG", {"cross_border": True, "destination": "", "mechanism": "contract", "transfer_impact_assessment": True}),
    ("EU", {"cross_border": False, "destination": "US", "mechanism": None, "transfer_impact_assessment": False}),
    ("VN", {"cross_border": False, "destination": "SG", "mechanism": "SCC", "transfer_impact_assessment": False}),
])
def test_rt12_contradictory_transfer_block_is_hold(policy, market, transfer):
    assert decide(case(market=market, transfer=transfer), policy).decision == "HOLD"


def test_rt13_misspelled_rule_path_is_detected(tmp_path, policy):
    def typo(raw):
        rule = next(r for r in raw["rules"] if r["id"] == "R-EU-02")
        rule["when"]["all"][0]["path"] = "transfer.crossborder"          # typo: cross_border
    risky = case(market="EU", transfer__cross_border=True, transfer__destination="US", transfer__mechanism=None)
    assert decide(risky, policy).decision == "HOLD"                      # sanity: real policy catches it
    try:
        mutated = load_policy(_tmp_policy(tmp_path, typo))
    except PolicyError:
        return                                                            # rejecting the rule file is the best fix
    assert decide(risky, mutated).decision == "HOLD"


@pytest.mark.xfail(strict=True, reason="RT-14: apply_review accepts a missing note (None/NaN -> 'None'/'nan' is "
                                       "truthy) and lets HOLD be overridden to GO without justification")
@pytest.mark.parametrize("note", [None, float("nan")])
def test_rt14_override_with_null_note_is_refused(note):
    with pytest.raises(ValueError):
        apply_review(_scored(), _reviewed([["A", "dpo", "edit", "GO", note]]))


@pytest.mark.xfail(strict=True, reason="RT-15: apply_review itself does not require a reviewer: HOLD->GO override is "
                                       "applied and audited with an empty/null actor")
@pytest.mark.parametrize("reviewer", ["", "   ", None])
def test_rt15_override_without_reviewer_is_refused(reviewer):
    with pytest.raises(ValueError):
        apply_review(_scored(), _reviewed([["A", reviewer, "edit", "GO", "DPIA filed, evidence attached"]]))


@pytest.mark.xfail(strict=True, reason="RT-16: conflicting review rows for one case are applied in file order; the "
                                       "last one silently wins (escalate then edit->GO ends as GO)")
def test_rt16_conflicting_reviews_for_same_case_are_refused():
    rows = [["A", "dpo", "escalate", "", "need vendor contract"],
            ["A", "intern", "edit", "GO", "looks fine"]]
    try:
        out = apply_review(_scored(), _reviewed(rows))
    except ValueError:
        return
    assert out.loc[out.case_id == "A", "decision"].iloc[0] == "HOLD"


@pytest.mark.xfail(strict=True, reason="RT-17: override_value accepts cross-track aliases; 'REJECT'/'FIX' map to "
                                       "REWORK, so a reviewer rejecting a HOLD case silently DOWNGRADES it to REWORK")
@pytest.mark.parametrize("alias", ["REJECT", "reject", "FIX", "RECAPTURE", "PASS", "ACCEPT", "RELEASE", "KEEP"])
def test_rt17_override_value_outside_go_hold_rework_is_refused(alias):
    with pytest.raises(ValueError):
        apply_review(_scored(), _reviewed([["A", "dpo", "edit", alias, "reviewer comment"]]))


# =============================================================================
# D. Open findings — MINOR (robustness of rule loading / inputs)
# =============================================================================
def test_rt18_markets_must_be_a_list(tmp_path):
    def stringify(raw):
        next(r for r in raw["rules"] if r["id"] == "R-EU-02")["markets"] = "EU"
    with pytest.raises(PolicyError):
        load_policy(_tmp_policy(tmp_path, stringify))


def test_rt19_non_numeric_param_is_rejected_cleanly(tmp_path):
    def stringify(raw):
        raw["params"]["retention_max_days"] = "365"
    try:
        mutated = load_policy(_tmp_policy(tmp_path, stringify))
    except PolicyError:
        return
    assert decide(case(retention_days=400), mutated).decision != "GO"


def test_rt20_absent_ambiguity_notes_is_evidence_missing(policy):
    c = case()
    del c["ambiguity_notes"]
    r = decide(c, policy)
    assert r.decision == "HOLD" and "EVIDENCE_MISSING" in r.reason_codes
