"""T09 baseline (deliberately wrong): "consent obtained => GO", otherwise HOLD.

Shows why a consent-only checklist is not enough: it ignores cross-border transfer, DPIA,
consent validity, retention, notice and market-specific lawful bases.

    python -m src.main --track t09 --pipeline src.baselines.t09_consent_only --output out/baseline --run-name baseline --offline --seed 42
"""
from __future__ import annotations

from ..common.context import RunContext
from . import run_simple_t09


def consent_only(case: dict) -> tuple[str, str]:
    if (case.get("consent") or {}).get("obtained") is True:
        return "GO", ""
    return "HOLD", "NO_LAWFUL_BASIS"


def run(ctx: RunContext) -> None:
    run_simple_t09(ctx, "baseline: consent-only", consent_only)
