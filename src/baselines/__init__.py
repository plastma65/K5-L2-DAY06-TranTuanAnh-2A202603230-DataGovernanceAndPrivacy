"""Group baselines (same pool / seed / output contract as the PoC)."""
from __future__ import annotations

from typing import Callable

import pandas as pd

from ..common.context import RunContext
from ..common.decision import Decision, write_decision
from ..common.review import make_review_queue, write_review_queue
from ..metrics.tracks import get_track


def run_simple_t09(ctx: RunContext, name: str, rule: Callable[[dict], tuple[str, str]]) -> None:
    """Shared wiring for T09 baselines: rule(case) -> (decision, reason_code)."""
    from ..pipeline import load_cases   # same loader as the PoC (no policy, no review)

    spec = get_track(ctx.track)
    rows = []
    for c in load_cases(ctx.input_dir):
        d, code = rule(c)
        rows.append({"case_id": c["case_id"], "decision": d, "reason_codes": code,
                     "score": {"HOLD": 2, "REWORK": 1, "GO": 0}[d], "reason": name})
    df = pd.DataFrame(rows)
    ctx.stats["n_items"] = len(df)
    q = make_review_queue(df, "case_id", "score", len(df), reason_col="reason")
    write_review_queue(q, ctx.out("review_queue.csv"))
    df.to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(Decision(status="HOLD", reason_code="BASELINE_ONLY", reason=f"{name}: never release on this",
                            owner=ctx.config.get("owner", "baseline"), scope=ctx.track,
                            evidence=[str(ctx.out(spec.pred_file))]),
                   ctx.out("decision.json"), ctx.out("decision_log.csv"))
