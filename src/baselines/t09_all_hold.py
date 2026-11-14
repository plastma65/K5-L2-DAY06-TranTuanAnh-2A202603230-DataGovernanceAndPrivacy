"""T09 trivial baseline: HOLD every case. Critical-miss = 0 by construction, but must FAIL the false-HOLD guardrail.

    python -m src.main --track t09 --pipeline src.baselines.t09_all_hold --output out/baseline_allhold --run-name baseline_allhold --offline --seed 42
"""
from __future__ import annotations

from ..common.context import RunContext
from . import run_simple_t09


def run(ctx: RunContext) -> None:
    run_simple_t09(ctx, "baseline: all-HOLD", lambda case: ("HOLD", "AMBIGUOUS_NEEDS_LEGAL"))
