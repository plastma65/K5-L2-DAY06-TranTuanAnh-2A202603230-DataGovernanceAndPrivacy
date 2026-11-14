"""T09 · Multi-Market Privacy Decision Gate — group PoC pipeline.

    01 load      cases/*.json + manifest sha256 check + versioned policy YAML (policies/t09_policy_v1.yaml)
    02 process   offline rule engine (src/t09/policy_engine.py): per-case GO/HOLD/REWORK + reason codes + rule ids
    03 rank      every case goes to the legal queue (budget = all cases); riskiest / most uncertain first
    04 review    human legal approval: approve / edit (override GO|HOLD|REWORK + note) / reject / escalate
    05 decide    run-level Decision: GO only when every case is signed off, otherwise HOLD (PENDING_LEGAL_APPROVAL)

Outputs (out/<run>/): decisions.csv, audit_checklist.csv, review_queue.csv, contact_sheet.html,
audit_log.jsonl, decision.json, decision_log.csv.
Teaching exercise: the gate supports a DPO/legal review, it does not certify GDPR / Luật 91 compliance.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from .common.contact_sheet import render_contact_sheet
from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.manifest import verify_manifest
from .common.review import load_review_decisions, make_review_queue, write_review_queue
from .metrics.tracks import get_track
from .t09.policy_engine import evaluate_case, load_policy

DEFAULT_POLICY = "policies/t09_policy_v1.yaml"
DEFAULT_AS_OF = "2026-11-14"   # frozen evaluation date => deterministic effective_date handling


def _resolve_policy_path(ctx: RunContext) -> Path:
    name = str(ctx.param("policy_file", DEFAULT_POLICY))
    p = Path(name)
    if p.is_absolute() or p.exists():
        return p
    return ctx.input_dir / name          # e.g. rules/policy_example.yaml inside the fixture


# ---------------------------------------------------------------- 01 load
def load_cases(input_dir: Path) -> list[dict]:
    files = sorted((input_dir / "cases").glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no case files in {input_dir / 'cases'}")
    cases = []
    for f in files:
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"{f}: invalid JSON ({e})") from e
        c.setdefault("case_id", f.stem)
        cases.append(c)
    ids = [c["case_id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate case_id in {input_dir / 'cases'}")
    return cases


def load(ctx: RunContext) -> dict:
    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")
    policy = load_policy(_resolve_policy_path(ctx))
    as_of = date.fromisoformat(str(ctx.param("as_of", DEFAULT_AS_OF)))
    ctx.stats.update({"policy_file": policy["path"], "policy_version": policy["policy_version"],
                      "policy_sha256": policy["sha256"], "as_of": str(as_of), "n_rules": len(policy["rules"])})
    ctx.audit.log(actor="pipeline", action="policy_loaded", item_id=policy["policy_version"],
                  after={"path": policy["path"], "sha256": policy["sha256"], "as_of": str(as_of)})
    return {"cases": load_cases(ctx.input_dir), "policy": policy, "as_of": as_of}


# ---------------------------------------------------------------- 02 process
def review_priority(decision: str, n_fired: int, needs_legal: bool, ambiguous: bool, n_upcoming: int) -> float:
    """Higher = review first. HOLD > REWORK > GO, then ambiguity, legal uncertainty, number of findings."""
    sev = {"HOLD": 2, "REWORK": 1, "GO": 0}[decision]
    return float(100 * sev + 20 * ambiguous + 10 * needs_legal + 3 * n_fired + n_upcoming)


def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    rows, checklist = [], []
    for case in data["cases"]:
        r = evaluate_case(case, data["policy"], data["as_of"])
        checklist.extend(r.checklist)
        ambiguous = bool(str(case.get("ambiguity_notes") or "").strip())
        why = "; ".join(f"{x['id']}:{x['reason_code']}" for x in r.fired) or "no rule fired"
        if r.flags:
            why += " | flags: " + ", ".join(f"{x['id']}:{x['reason_code']}" for x in r.flags)
        if r.upcoming:
            why += " | upcoming: " + ", ".join(f"{x['id']} (from {x['effective_date']})" for x in r.upcoming)
        rows.append({
            "case_id": r.case_id,
            "decision": r.decision,
            "reason_codes": ";".join(r.reason_codes),
            "score": review_priority(r.decision, len(r.fired), r.needs_legal_review, ambiguous, len(r.upcoming)),
            "proposed_decision": r.decision,
            "market": r.market,
            "rule_ids": ";".join(r.rule_ids),
            "sources": " | ".join(sorted({f"{x['source'].get('doc')} {x['source'].get('ref', '')}".strip() for x in r.fired})),
            "upcoming": ";".join(f"{x['id']}@{x['effective_date']}" for x in r.upcoming),
            "flags": ";".join(r.flag_codes),
            "needs_legal_review": r.needs_legal_review,
            "missing_fields": ";".join(r.missing_fields + r.invalid_fields),
            "reason": why,
            "approval_status": "pending_legal",
            "reviewer": "",
            "review_note": "",
        })
    pd.DataFrame(checklist).to_csv(ctx.out("audit_checklist.csv"), index=False)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    """Final legal approval is required for every case, so the default budget is the whole pool."""
    budget = int(ctx.param("budget", len(scored)))
    q = make_review_queue(scored, id_col="case_id", score_col="score", budget=budget,
                          reason_col="reason", action_col="proposed_decision", slice_col="market")
    write_review_queue(q, ctx.out("review_queue.csv"))
    render_contact_sheet(q, ctx.out("contact_sheet.html"), image_col=None,
                         title=f"T09 legal approval queue (policy {ctx.stats.get('policy_version')})")
    ctx.stats["review_budget"] = budget
    return q


# ---------------------------------------------------------------- 04 review
T09_DECISIONS = ("GO", "HOLD", "REWORK")
REVIEW_DECISIONS = ("approve", "edit", "reject", "escalate", "skip")


def _text(v) -> str:
    """Cell -> stripped string; None / NaN become '' (so they never pass a 'mandatory' check)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def apply_review(scored: pd.DataFrame, reviewed: pd.DataFrame, audit=None) -> pd.DataFrame:
    """approve -> keep proposal · edit/reject + override -> override (note mandatory) ·
    reject without value / escalate -> HOLD. Every change goes to the audit log.

    Guards: named reviewer on every acted row, one acted row per case (conflicts go back to the
    reviewers, never resolved by file order), override value strictly GO/HOLD/REWORK (no aliases
    from other tracks such as REJECT->REWORK)."""
    out = scored.copy()
    known = set(out["case_id"].astype(str))
    acted = [r for r in reviewed.itertuples(index=False) if _text(r.reviewer_decision).lower() != "skip"]
    ids = [_text(r.item_id) for r in acted]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    if dups:
        raise ValueError(f"conflicting review rows for {dups}: resolve between reviewers before applying")
    for r in acted:
        cid = _text(r.item_id)
        if cid not in known:
            raise ValueError(f"review file references unknown case {cid!r}")
        dec = _text(r.reviewer_decision).lower()
        if dec not in REVIEW_DECISIONS:
            raise ValueError(f"{cid}: unknown reviewer_decision {dec!r}; allowed {REVIEW_DECISIONS}")
        reviewer, note, ov = _text(r.reviewer), _text(r.review_note), _text(r.override_value).upper()
        if not reviewer:
            raise ValueError(f"{cid}: reviewer is mandatory for a legal decision ({dec})")
        m = out["case_id"].astype(str) == cid
        before = out.loc[m, "decision"].iloc[0]
        if dec == "approve":
            after, status = before, "approved"
        elif dec in ("edit", "reject") and ov:
            if ov not in T09_DECISIONS:
                raise ValueError(f"{cid}: override_value must be one of {T09_DECISIONS}, got {ov!r}")
            if not note:
                raise ValueError(f"{cid}: override {before}->{ov} needs a review_note (legal justification)")
            after, status = ov, "overridden"
        elif dec in ("reject", "escalate"):
            after, status = "HOLD", "escalated"
        else:
            raise ValueError(f"{cid}: `edit` needs override_value GO/HOLD/REWORK")
        out.loc[m, ["decision", "approval_status", "reviewer", "review_note"]] = [after, status, reviewer, note]
        if after != before:
            codes = [c for c in str(out.loc[m, "reason_codes"].iloc[0]).split(";") if c]
            codes.append("ESCALATED_TO_DPO" if status == "escalated" else "HUMAN_OVERRIDE")
            out.loc[m, "reason_codes"] = ";".join(dict.fromkeys(codes))
        if audit:
            audit.log(actor=reviewer, action=f"legal_review:{dec}", item_id=cid, before=before, after=after,
                      reason=note, approval_status=status)
    return out


def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame) -> pd.DataFrame:
    reviewed_path = ctx.output_dir / "review_queue_reviewed.csv"
    if not reviewed_path.exists():
        ctx.stats["n_reviewed"] = 0
        return scored
    reviewed = load_review_decisions(reviewed_path)
    ctx.stats["n_reviewed"] = len(reviewed)
    return apply_review(scored, reviewed, ctx.audit)


# ---------------------------------------------------------------- 05 decide
def decide(ctx: RunContext, final: pd.DataFrame) -> Decision:
    counts = final["decision"].value_counts().to_dict()
    pending = final[final["approval_status"] == "pending_legal"]["case_id"].tolist()
    owner = ctx.config.get("owner", "TODO-owner")
    evidence = [str(ctx.out(n)) for n in ("decisions.csv", "audit_checklist.csv", "review_queue.csv", "audit_log.jsonl")]
    metrics = {"n_cases": len(final), "by_decision": counts, "n_pending_legal": len(pending),
               "policy_version": ctx.stats.get("policy_version"), "policy_sha256": ctx.stats.get("policy_sha256")}
    if pending:
        return Decision(status="HOLD", reason_code="PENDING_LEGAL_APPROVAL",
                        reason=f"{len(pending)}/{len(final)} cases still need DPO/legal sign-off: {', '.join(pending)}",
                        owner=owner, scope=ctx.track, evidence=evidence, metrics=metrics)
    return Decision(status="GO", reason_code="ALL_CASES_SIGNED_OFF",
                    reason=f"All {len(final)} case decisions signed off by legal reviewer(s); per-case results in decisions.csv",
                    owner=owner, scope=ctx.track, evidence=evidence, metrics=metrics)


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    if spec.track != "t09":
        raise NotImplementedError(f"group pipeline implements t09 only; {spec.track} ({spec.title}) is not implemented")
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue)
    final[spec.pred_columns + [c for c in final.columns if c not in spec.pred_columns]].to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(decide(ctx, final), ctx.out("decision.json"), ctx.out("decision_log.csv"))
