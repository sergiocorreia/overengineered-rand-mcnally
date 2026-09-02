"""Project-specific page scoring hook.

Edit only the constants and, when necessary, ``classify_page``.  Automatic
scores are recommendations: a page remains ``unreviewed`` until a human
source- or page-level decision resolves it.
"""

from __future__ import annotations

from typing import Any

# Populate these with distinctive phrases after inspecting representative
# complete pages.  Empty defaults intentionally make no substantive claim.
TARGET_TERMS: tuple[str, ...] = ()
EXCLUDE_TERMS: tuple[str, ...] = ()
MIN_EMBEDDED_CHARACTERS = 80


def classify_page(text: str, context: dict[str, Any]) -> dict[str, object]:
    """Return a recommendation, score, and auditable reasons for one page."""

    normalized = " ".join(text.casefold().split())
    targets = [term for term in TARGET_TERMS if term.casefold() in normalized]
    exclusions = [term for term in EXCLUDE_TERMS if term.casefold() in normalized]
    score = float(10 * len(targets) - 10 * len(exclusions))
    if targets and not exclusions:
        recommendation = "selected"
    elif exclusions and not targets:
        recommendation = "excluded"
    else:
        recommendation = "unreviewed"
    reasons = [*(f"target:{term}" for term in targets), *(f"exclude:{term}" for term in exclusions)]
    if not reasons:
        reasons.append("no_configured_signal")
    return {"classification": recommendation, "score": score, "reasons": reasons}


def needs_locro(text: str, decision: dict[str, object], context: dict[str, Any]) -> bool:
    """Target Locro at pages whose embedded text is sparse or inconclusive."""

    return len(text.strip()) < MIN_EMBEDDED_CHARACTERS
