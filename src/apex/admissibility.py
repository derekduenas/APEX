"""Explicit evidence categories for research; commissioning remains in paper_runtime.

A synthetic control is a distinct category, not a weaker version of recorded market evidence.
This function cannot authorize live execution; the existing paper runtime owns its independent source gate.
"""
from __future__ import annotations

from .core import Refused
from .data import collection_problem

# Recognized categories; eligibility is explicit rather than inherited by ordinal rank.
MODES = ("REFUSED", "SYNTHETIC_CONTROL", "OFFLINE_RESEARCH", "SHADOW_OBSERVATION", "LIVE_PAPER", "REAL_MONEY")
ASSUMED_BASES = {"BAR_COMPLETION_ASSUMPTION_V1", "QUOTE_LATENCY_ASSUMPTION_V1"}


def admissibility(document: dict, observations: list) -> dict:
    """Return the highest mode this input may support, and the named reason it stops there."""
    from .research import input_class_of
    input_class = input_class_of(document, observations)
    bases = sorted({r["availability_basis"] for r in observations})
    assumed = sorted(set(bases) & ASSUMED_BASES)
    if collection_problem(document):
        ceiling, reason = "REFUSED", collection_problem(document)
    elif not observations:
        ceiling, reason = "REFUSED", "NO_OBSERVATIONS_OR_UNRECOGNIZED_AVAILABILITY_BASIS"
    elif input_class == "MIXED_SYNTHETIC_AND_RECORDED_INPUT":
        ceiling, reason = "REFUSED", "MIXED_SYNTHETIC_AND_RECORDED_INPUT_IS_NEITHER_CONTROL_NOR_EVIDENCE"
    elif input_class == "SYNTHETIC_RESEARCH_CONTROL" or "SYNTHETIC_CLOCK" in bases:
        ceiling, reason = "SYNTHETIC_CONTROL", "SYNTHETIC_CLOCK_OBSERVATIONS_ARE_A_CONTROL_NOT_MARKET_EVIDENCE"
    elif assumed:
        ceiling, reason = "OFFLINE_RESEARCH", "AVAILABILITY_ASSUMED_NOT_MEASURED:" + ",".join(assumed)
    elif bases == ["MEASURED_RECEIPT"]:
        ceiling, reason = "SHADOW_OBSERVATION", "MEASURED_RECEIPTS_PRESENT_BUT_EXECUTION_IS_SEPARATELY_COMMISSIONED"
    else:
        ceiling, reason = "REFUSED", "NO_OBSERVATIONS_OR_UNRECOGNIZED_AVAILABILITY_BASIS"
    return {"input_class": input_class, "availability_bases": bases, "assumed_availability_bases": assumed,
            "highest_admissible_mode": ceiling, "ceiling_reason": reason,
            "modes": {m: m in {
                "REFUSED": set(), "SYNTHETIC_CONTROL": {"SYNTHETIC_CONTROL"},
                "OFFLINE_RESEARCH": {"OFFLINE_RESEARCH"},
                "SHADOW_OBSERVATION": {"OFFLINE_RESEARCH", "SHADOW_OBSERVATION"},
            }[ceiling] for m in MODES[1:]},
            "note": "A ceiling states what the data cannot disqualify. It grants no execution authority."}


def require_mode(document: dict, observations: list, mode: str) -> dict:
    """Refuse by name when an input is asked to support more than it can."""
    if mode not in MODES[1:]:
        raise Refused("UNKNOWN_OPERATING_MODE")
    verdict = admissibility(document, observations)
    if not verdict["modes"][mode]:
        raise Refused("INPUT_NOT_ADMISSIBLE_FOR_" + mode + ":" + verdict["ceiling_reason"])
    return verdict
