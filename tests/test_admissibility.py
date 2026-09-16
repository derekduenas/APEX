"""What an input may be used for, and the proof that a weaker input cannot acquire a stronger status downstream."""
import json

import pytest

from apex.admissibility import MODES, admissibility, require_mode
from apex.core import Refused
from apex.data import from_alpaca_quotes, merged_document, normalize

QUOTE = [{"t": "2026-08-24T13:30:03.996061335Z", "bp": 640.11, "ap": 640.13, "bs": 40, "as": 240}]
BAR = {"kind": "bar", "symbol": "SPY", "event_epoch": 1787578200.0, "available_epoch": 1787578260.0,
       "availability_basis": "BAR_COMPLETION_ASSUMPTION_V1", "open": 640.0, "high": 640.5, "low": 639.5,
       "close": 640.2, "volume": 1000.0}
MEASURED = dict(BAR, availability_basis="MEASURED_RECEIPT", available_epoch=1787578263.0)
SYNTHETIC = dict(BAR, availability_basis="SYNTHETIC_CLOCK")


def verdict(source, observations):
    document = {"schema": "APEX_DATA_V1", "source": source, "observations": observations}
    rows, rejected = normalize(document)
    assert rejected == [], rejected
    return document, rows, admissibility(document, rows)


def test_assumed_availability_quotes_stop_at_offline_research():
    """The new basis is an ASSUMPTION. It may inform a study; it may not stand behind an execution-bearing mode."""
    document, rows, v = verdict("ALPACA_HISTORICAL_QUOTES_AS_OF_DECISION_INSTANTS",
                                from_alpaca_quotes(QUOTE, "SPY", "r"))
    assert v["highest_admissible_mode"] == "OFFLINE_RESEARCH"
    assert v["ceiling_reason"] == "AVAILABILITY_ASSUMED_NOT_MEASURED:QUOTE_LATENCY_ASSUMPTION_V1"
    assert v["modes"]["OFFLINE_RESEARCH"] and not v["modes"]["SHADOW_OBSERVATION"]
    assert not v["modes"]["LIVE_PAPER"] and not v["modes"]["REAL_MONEY"]
    with pytest.raises(Refused, match="INPUT_NOT_ADMISSIBLE_FOR_LIVE_PAPER:AVAILABILITY_ASSUMED_NOT_MEASURED"):
        require_mode(document, rows, "LIVE_PAPER")


def test_assumed_bars_stop_there_too_so_the_rule_is_about_assumption_not_about_quotes():
    _, _, v = verdict("MASSIVE_CONNECTOR_TRANSFORMED_CSV", [BAR])
    assert v["highest_admissible_mode"] == "OFFLINE_RESEARCH"
    assert v["assumed_availability_bases"] == ["BAR_COMPLETION_ASSUMPTION_V1"]


def test_one_assumed_row_lowers_the_whole_document():
    """A ceiling that only applied when EVERY row was assumed would be no ceiling at all."""
    _, _, v = verdict("MIXED_AVAILABILITY", [MEASURED] + from_alpaca_quotes(QUOTE, "SPY", "r"))
    assert v["highest_admissible_mode"] == "OFFLINE_RESEARCH"


def test_measured_receipts_reach_shadow_and_stop_short_of_live_paper():
    document, rows, v = verdict("ALPACA_CAPTURE", [MEASURED])
    assert v["highest_admissible_mode"] == "SHADOW_OBSERVATION"
    assert not v["modes"]["LIVE_PAPER"], "clean data is not a commissioned paper trader"
    with pytest.raises(Refused, match="INPUT_NOT_ADMISSIBLE_FOR_LIVE_PAPER"):
        require_mode(document, rows, "LIVE_PAPER")


def test_a_synthetic_control_cannot_climb_above_being_a_control():
    document, rows, v = verdict("SYNTHETIC_PERSISTENT", [SYNTHETIC])
    assert v["highest_admissible_mode"] == "SYNTHETIC_CONTROL"
    for mode in ("OFFLINE_RESEARCH", "SHADOW_OBSERVATION", "LIVE_PAPER", "REAL_MONEY"):
        with pytest.raises(Refused, match="INPUT_NOT_ADMISSIBLE_FOR_" + mode):
            require_mode(document, rows, mode)


def test_a_synthetic_clock_row_hidden_in_a_real_document_refuses_every_mode():
    """The label on the document is a claim; the observations are the evidence, and they win."""
    _, _, v = verdict("ALPACA_CAPTURE", [MEASURED, SYNTHETIC])
    assert v["highest_admissible_mode"] == "REFUSED"
    assert not any(v["modes"].values())


def test_a_mixed_merged_input_is_refused_for_every_mode():
    document = {"schema": "APEX_DATA_V1", "source": "MERGED_INPUT_DOCUMENTS",
                "components": [{"source": "SYNTHETIC_PERSISTENT"}, {"source": "MASSIVE_CONNECTOR_TRANSFORMED_CSV"}],
                "observations": [MEASURED]}
    rows, _ = normalize(document)
    v = admissibility(document, rows)
    assert v["highest_admissible_mode"] == "REFUSED"
    for mode in MODES[1:]:
        with pytest.raises(Refused, match="INPUT_NOT_ADMISSIBLE_FOR_" + mode):
            require_mode(document, rows, mode)


def test_merged_provenance_survives_normalization_and_reaches_the_ceiling_decision():
    """Point of failure this guards: normalize returns only rows, so a consumer that dropped the document would
    lose the component provenance and with it the mixed-input verdict."""
    bars = {"schema": "APEX_DATA_V1", "source": "MASSIVE_CONNECTOR_TRANSFORMED_CSV", "observations": [BAR]}
    quotes = {"schema": "APEX_DATA_V1", "source": "ALPACA_HISTORICAL_QUOTES_AS_OF_DECISION_INSTANTS",
              "observations": from_alpaca_quotes(QUOTE, "SPY", "r")}
    merged = merged_document([bars, quotes], retrieved_utc="2026-09-15T00:00:00Z")
    rows, rejected = normalize(merged)
    assert rejected == [] and len(rows) == 2
    assert [c["source"] for c in merged["components"]] == [bars["source"], quotes["source"]]
    v = admissibility(merged, rows)
    assert v["input_class"] == "RECORDED_RESEARCH_WITH_DECLARED_AVAILABILITY_LIMITS"
    assert v["highest_admissible_mode"] == "OFFLINE_RESEARCH"
    assert sorted(v["assumed_availability_bases"]) == ["BAR_COMPLETION_ASSUMPTION_V1", "QUOTE_LATENCY_ASSUMPTION_V1"]


def test_an_empty_document_is_refused_rather_than_defaulting_to_permitted():
    _, _, v = verdict("ANYTHING", [])
    assert v["highest_admissible_mode"] == "REFUSED"


def test_an_unknown_mode_is_refused_by_name():
    document, rows, _ = verdict("ALPACA_CAPTURE", [MEASURED])
    with pytest.raises(Refused, match="UNKNOWN_OPERATING_MODE"):
        require_mode(document, rows, "PRODUCTION")


# ---------------------------------------------------------------- decision quotes are not execution evidence


def test_a_decision_quote_document_carries_no_post_decision_sequence_and_says_so():
    """Point of confusion this guards: a study that reaches EXPERIMENTAL_LONG has evaluated an opportunity, not
    taken a trade. Nothing in a decision-instant document can support a fill, an exit, or realized P&L."""
    import apex.decision_quotes as DQ
    document = {"schema": "APEX_DATA_V1", "source": "ALPACA_HISTORICAL_QUOTES_AS_OF_DECISION_INSTANTS",
                "observations": from_alpaca_quotes(QUOTE, "SPY", "r")}
    rows, _ = normalize(document)
    quote = rows[0]
    # One quote per decision instant, at or before the decision. There is no observation after it by construction.
    assert all(r["available_epoch"] <= 1787578205.0 for r in rows)
    assert "establish no fill, exit, stop ordering or realized" in __import__("inspect").getsource(DQ)
    assert admissibility(document, rows)["highest_admissible_mode"] == "OFFLINE_RESEARCH"
