"""The as-of-decision quote fetch: the right instants, an honest window, and no future leakage."""
import json
import base64

import pytest

from apex.core import Config, Refused
from apex.data import QUOTE_LATENCY_SECONDS, regular
from apex.decision_quotes import fetch_decision_quotes, rfc3339
from apex.research import ResearchPlan, decision_instants

PLAN = ResearchPlan(**json.load(open("docs/evidence/edge-research-001/market-plan.json")))


def recorder(bid=640.11, ask=640.13, offset=-2.0, extra=()):
    """A transport that answers every window, newest first, with one quote `offset` seconds before its end bound.

    `extra` appends further records sharing that same timestamp, so a test can present a final event-time group.
    """
    seen = []

    def transport(path, params, timeout):
        seen.append((path, params))
        from apex.data import timestamp_ns
        t = rfc3339(timestamp_ns(params["end"]) / 1e9 + offset)
        rows = [{"t": t, "bp": bid, "ap": ask, "bs": 40, "as": 240}] + [{"t": t, **e} for e in extra]
        body = json.dumps({"quotes": {"SPY": rows}}).encode()
        return {"status": 200, "body_base64": base64.b64encode(body).decode()}
    return transport, seen


def test_one_request_per_in_session_decision_instant_with_the_evaluators_own_window(tmp_path):
    transport, seen = recorder()
    summary = fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=transport)
    expected = [t for t in decision_instants(PLAN) if regular(t)]
    assert summary["requested"] == summary["answered"] == len(expected) == 195
    assert summary["decision_instants"] == len(decision_instants(PLAN))
    assert [p for _, p in seen][0]["symbols"] == "SPY"
    assert all(path == "/v2/stocks/quotes" for path, _ in seen)
    from apex.data import timestamp_ns
    for now, (_, params) in zip(expected, seen):
        # max_quote_age back, and no closer than the declared latency: exactly what quote_at will accept.
        assert timestamp_ns(params["start"]) / 1e9 == pytest.approx(now - Config().max_quote_age)
        assert timestamp_ns(params["end"]) / 1e9 == pytest.approx(now - QUOTE_LATENCY_SECONDS)
        assert params["limit"] == 50 and params["sort"] == "desc"


def test_every_fetched_quote_is_usable_by_the_evaluator_at_its_own_decision_instant(tmp_path):
    from apex.decision import quote_at
    transport, _ = recorder()
    fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=transport)
    document = json.loads((tmp_path / "q" / "quotes.json").read_text())
    from apex.data import normalize
    rows, rejected = normalize(document)
    assert rejected == []
    config = Config(symbol="SPY")
    for now in [t for t in decision_instants(PLAN) if regular(t)]:
        quote, problem = quote_at(rows, now, config)
        assert problem is None and quote["ask"] == 640.13, "the study's WAIT must stop being structurally forced"


def test_a_quote_the_provider_should_not_have_returned_is_refused_not_absorbed(tmp_path):
    """If `end` were ignored, a quote stamped after the decision would silently grant foresight."""
    transport, _ = recorder(offset=+5.0)
    with pytest.raises(Refused, match="PROVIDER_RETURNED_QUOTE_NOT_YET_AVAILABLE_AT_DECISION"):
        fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=transport)


def test_a_blocked_credential_produces_an_empty_but_accounted_document(tmp_path):
    def blocked(path, params, timeout):
        return {"status": 0, "error": "BLOCKED_EXTERNAL_CREDENTIAL"}
    summary = fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=blocked)
    assert summary["answered"] == 0 and summary["quotes"] == 0
    assert summary["reconciliation"]["failed"] == summary["requested"] == 195
    document = json.loads((tmp_path / "q" / "quotes.json").read_text())
    assert document["observations"] == []
    assert document["collection_status"] == "INCOMPLETE"
    assert "0 of 195 instants answered" in document["coverage"], "absence is stated, not implied by silence"


def test_the_declared_lot_size_must_be_explicit_and_bounded(tmp_path):
    transport, _ = recorder()
    with pytest.raises(Refused, match="EXPLICIT_QUOTE_LOT_SIZE_REQUIRED"):
        fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"),
                              transport=transport, round_lot_shares=0)


def test_merging_keeps_each_half_answerable_for_its_own_claims():
    from apex.data import merged_document
    from apex.research import input_class_of
    bars = {"schema": "APEX_DATA_V1", "source": "MASSIVE_CONNECTOR_TRANSFORMED_CSV",
            "limitation": "No quotes supplied.", "observations": []}
    quotes = {"schema": "APEX_DATA_V1", "source": "ALPACA_HISTORICAL_QUOTES_AS_OF_DECISION_INSTANTS",
              "limitation": "Availability is DECLARED", "observations": []}
    merged = merged_document([bars, quotes], retrieved_utc="2026-09-15T00:00:00Z")
    assert [c["source"] for c in merged["components"]] == [bars["source"], quotes["source"]]
    assert merged["components"][1]["limitation"] == "Availability is DECLARED"
    assert input_class_of(merged, []) == "RECORDED_RESEARCH_WITH_DECLARED_AVAILABILITY_LIMITS"


def test_a_synthetic_control_cannot_be_merged_into_a_recorded_study():
    """Otherwise the merged `source` string would read as recorded and the control would count as evidence."""
    from apex.data import merged_document
    from apex.research import input_class_of
    synthetic = {"schema": "APEX_DATA_V1", "source": "SYNTHETIC_PERSISTENT", "observations": []}
    real = {"schema": "APEX_DATA_V1", "source": "MASSIVE_CONNECTOR_TRANSFORMED_CSV", "observations": []}
    with pytest.raises(Refused, match="REFUSING_TO_MERGE_SYNTHETIC_AND_RECORDED_INPUTS"):
        merged_document([synthetic, real], retrieved_utc="x")
    # And if such a document arrives by any other route, the study names it rather than classifying it as real.
    assert input_class_of({"components": [{"source": "SYNTHETIC_PERSISTENT"}, {"source": "REAL"}]},
                          []) == "MIXED_SYNTHETIC_AND_RECORDED_INPUT"
    assert input_class_of(merged_document([synthetic], retrieved_utc="x"), []) == "SYNTHETIC_RESEARCH_CONTROL"


# ---------------------------------------------------------------- the final event-time group


def test_a_disagreeing_record_at_the_selected_time_is_carried_so_the_reader_can_refuse_it(tmp_path):
    """The reason a one-record fetch was not safe. Measured on this provider's data: 359 groups of records share
    a nanosecond stamp and 329 disagree. Keeping only one would hand the study a quote the reader would reject."""
    from apex.data import normalize
    from apex.decision import quote_at
    transport, _ = recorder(extra=({"bp": 640.05, "ap": 640.20, "bs": 100, "as": 100},))
    fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=transport)
    rows, rejected = normalize(json.loads((tmp_path / "q" / "quotes.json").read_text()))
    assert rejected == []
    now = [t for t in decision_instants(PLAN) if regular(t)][0]
    quote, problem = quote_at(rows, now, Config(symbol="SPY"))
    # `visible` drops a conflicting group outright, so the refusal surfaces under the unavailability name rather
    # than the conflict name. Recorded as the behaviour that exists: the instant IS refused, but the reason code
    # cannot distinguish "nothing was quoted" from "the quotes contradicted each other".
    assert quote is None and problem == "QUOTE_UNAVAILABLE_OR_CONFLICTING"


def test_an_agreeing_duplicate_at_the_selected_time_is_not_treated_as_a_conflict(tmp_path):
    """Thirty of the measured same-nanosecond groups agree on every market value. Those must stay usable."""
    from apex.data import normalize
    from apex.decision import quote_at
    transport, _ = recorder(extra=({"bp": 640.11, "ap": 640.13, "bs": 40, "as": 240},))
    fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=transport)
    rows, _ = normalize(json.loads((tmp_path / "q" / "quotes.json").read_text()))
    now = [t for t in decision_instants(PLAN) if regular(t)][0]
    quote, problem = quote_at(rows, now, Config(symbol="SPY"))
    assert problem is None and quote["ask"] == 640.13


def test_a_final_group_that_fills_the_page_is_flagged_rather_than_assumed_whole(tmp_path):
    from apex.decision_quotes import FINAL_GROUP_LIMIT
    transport, _ = recorder(extra=tuple({"bp": 640.11, "ap": 640.13, "bs": 40, "as": 240}
                                        for _ in range(FINAL_GROUP_LIMIT - 1)))
    summary = fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=transport)
    assert summary["collection_status"] == "INCOMPLETE"
    assert "FINAL_GROUP_POSSIBLY_TRUNCATED" in summary["reconciliation"]["incomplete_because"]


def test_earlier_records_are_dropped_because_they_cannot_change_the_readers_verdict(tmp_path):
    """The equivalence that makes the sparse document legitimate, stated as a test rather than as a hope:
    quote_at reads the latest available quote and refuses only on conflicts at an event time at or after it."""
    def transport(path, params, timeout):
        from apex.data import timestamp_ns
        end = timestamp_ns(params["end"]) / 1e9
        rows = [{"t": rfc3339(end - 2.0), "bp": 640.11, "ap": 640.13, "bs": 40, "as": 240},
                {"t": rfc3339(end - 6.0), "bp": 1.00, "ap": 2.00, "bs": 1, "as": 1},
                {"t": rfc3339(end - 6.0), "bp": 3.00, "ap": 4.00, "bs": 1, "as": 1}]  # an EARLIER conflict
        return {"status": 200, "body_base64": base64.b64encode(json.dumps({"quotes": {"SPY": rows}}).encode()).decode()}
    from apex.data import normalize
    from apex.decision import quote_at
    fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=transport)
    rows, _ = normalize(json.loads((tmp_path / "q" / "quotes.json").read_text()))
    now = [t for t in decision_instants(PLAN) if regular(t)][0]
    quote, problem = quote_at(rows, now, Config(symbol="SPY"))
    assert problem is None and quote["ask"] == 640.13, "an earlier conflict must not veto a later clean quote"


# ---------------------------------------------------------------- reconciliation and boundaries


def test_every_planned_request_lands_in_exactly_one_outcome_and_the_buckets_sum(tmp_path):
    calls = {"n": 0}

    def mixed(path, params, timeout):
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            return {"status": 0, "error": "TRANSPORT_TIMEOUT"}
        if calls["n"] % 3 == 1:
            body = json.dumps({"quotes": {"SPY": []}}).encode()          # a closed session or a quiet window
        else:
            from apex.data import timestamp_ns
            t = rfc3339(timestamp_ns(params["end"]) / 1e9 - 2.0)
            body = json.dumps({"quotes": {"SPY": [{"t": t, "bp": 640.11, "ap": 640.13, "bs": 40, "as": 240}]}}).encode()
        return {"status": 200, "body_base64": base64.b64encode(body).decode()}
    summary = fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=mixed)
    r = summary["reconciliation"]
    assert r["issued"] == r["planned_in_session_requests"] == 195
    assert r["accounted"] == r["issued"], "no request may vanish between issue and outcome"
    assert r["answered"] and r["empty_no_quotes_in_window"] and r["failed"]
    assert r["status"] == "INCOMPLETE" and "FAILED_REQUESTS" in r["incomplete_because"]
    assert json.loads((tmp_path / "q" / "quotes.json").read_text())["collection_status"] == "INCOMPLETE"


def test_an_invalid_quote_is_rejected_by_name_and_the_instant_is_accounted_not_silently_dropped(tmp_path):
    def crossed(path, params, timeout):
        from apex.data import timestamp_ns
        t = rfc3339(timestamp_ns(params["end"]) / 1e9 - 2.0)
        body = json.dumps({"quotes": {"SPY": [{"t": t, "bp": 640.20, "ap": 640.10, "bs": 40, "as": 240}]}}).encode()
        return {"status": 200, "body_base64": base64.b64encode(body).decode()}
    summary = fetch_decision_quotes(tmp_path / "q", plan=PLAN, config=Config(symbol="SPY"), transport=crossed)
    r = summary["reconciliation"]
    assert r["all_records_rejected"] == 195 and r["answered"] == 0 and r["accounted"] == r["issued"]
    first = json.loads((tmp_path / "q" / "request-0001.json").read_text())
    assert first["rejected"] == [{"input_index": 0, "reason": "INVALID_QUOTE_MARKET"}]


def test_a_quote_exactly_at_the_cutoff_is_kept_and_one_nanosecond_past_it_is_not(tmp_path):
    """Compared in INTEGER NANOSECONDS. A float epoch near 1.79e9 has about 238ns of resolution, so this
    distinction does not exist once the stamps become seconds."""
    from apex.data import QUOTE_LATENCY_NS, from_alpaca_quotes, normalize, timestamp_ns
    stamp = "2026-08-24T13:30:03.996061335Z"
    row = from_alpaca_quotes([{"t": stamp, "bp": 640.11, "ap": 640.13, "bs": 40, "as": 240}], "SPY", "r")[0]
    assert row["available_ns"] - row["event_ns"] == QUOTE_LATENCY_NS == 1_000_000_000
    accepted, rejected = normalize({"schema": "APEX_DATA_V1", "observations": [row]})
    assert len(accepted) == 1 and rejected == []
    late = dict(row, available_ns=row["available_ns"] + 1)
    late["available_epoch"] = late["available_ns"] / 1e9
    assert late["available_epoch"] == row["available_epoch"], "the seconds are identical; only the ns differ"
    _, rejected = normalize({"schema": "APEX_DATA_V1", "observations": [late]})
    assert rejected == [{"input_index": 0, "reason": "QUOTE_LATENCY_ASSUMPTION_DISAGREES"}]
    early = dict(row, available_ns=row["available_ns"] - 1)
    early["available_epoch"] = early["available_ns"] / 1e9
    _, rejected = normalize({"schema": "APEX_DATA_V1", "observations": [early]})
    assert rejected == [{"input_index": 0, "reason": "QUOTE_LATENCY_ASSUMPTION_DISAGREES"}]


def test_seconds_that_disagree_with_the_nanosecond_stamps_are_refused(tmp_path):
    """Otherwise a row could pass the exact nanosecond check while the reader, which uses seconds, saw a
    different instant entirely."""
    from apex.data import from_alpaca_quotes, normalize
    row = from_alpaca_quotes([{"t": "2026-08-24T13:30:03.996061335Z", "bp": 640.11, "ap": 640.13,
                               "bs": 40, "as": 240}], "SPY", "r")[0]
    _, rejected = normalize({"schema": "APEX_DATA_V1", "observations": [dict(row, available_epoch=row["available_epoch"] + 5)]})
    assert rejected == [{"input_index": 0, "reason": "SECONDS_DISAGREE_WITH_NANOSECOND_STAMPS"}]


def test_a_quote_claiming_the_assumed_basis_without_nanosecond_stamps_is_refused(tmp_path):
    from apex.data import from_alpaca_quotes, normalize
    row = from_alpaca_quotes([{"t": "2026-08-24T13:30:03.996061335Z", "bp": 640.11, "ap": 640.13,
                               "bs": 40, "as": 240}], "SPY", "r")[0]
    row.pop("event_ns")
    _, rejected = normalize({"schema": "APEX_DATA_V1", "observations": [row]})
    assert rejected == [{"input_index": 0, "reason": "QUOTE_LATENCY_ASSUMPTION_REQUIRES_NANOSECOND_STAMPS"}]
