"""The historical-quote ingestion path: a DECLARED availability assumption, not a measured receipt."""
import pytest

from apex.core import Refused
from apex.data import QUOTE_LATENCY_SECONDS, from_alpaca_quotes, normalize, timestamp_ns, visible

# One real-shaped Alpaca /v2/stocks/quotes page. Nanosecond stamps, and provider `bs`/`as` in the same unit
# the provider's trade schema calls shares.
RAW = [{"t": "2026-09-14T13:30:00.123456789Z", "bp": 640.11, "ap": 640.13, "bs": 40, "as": 240,
        "bx": "V", "ax": "V", "c": ["R"], "z": "B"},
       {"t": "2026-09-14T13:30:05Z", "bp": 640.12, "ap": 640.14, "bs": 200, "as": 80,
        "bx": "P", "ax": "V", "c": ["R"], "z": "B"}]


def doc(rows):
    return {"schema": "APEX_DATA_V1", "observations": rows}


def test_nanosecond_stamps_are_read_whole_and_there_is_only_one_parser():
    """Alpaca quotes carry 9-digit fractions, which fromisoformat silently truncates. The exactness matters at
    the ingestion boundary because two quotes in the same microsecond must still order. capture.py imports this
    same function rather than keeping a second copy that could drift from it."""
    from apex import capture
    from datetime import datetime
    assert datetime.fromisoformat("2026-09-14T13:30:00.123456789+00:00").microsecond == 123456, "789ns dropped"
    assert capture.timestamp_ns is timestamp_ns
    assert timestamp_ns("2026-09-14T13:30:00.123456789Z") == 1789392600_123456789
    assert timestamp_ns("2026-09-14T09:30:00-04:00") == 1789392600_000000000
    assert from_alpaca_quotes(RAW, "SPY", "R")[0]["event_epoch"] == pytest.approx(1789392600.1234567, abs=1e-6)


def test_a_timestamp_without_an_offset_is_refused_rather_than_read_as_local_time():
    with pytest.raises(Refused, match="TIMESTAMP_NOT_RFC3339"):
        timestamp_ns("2026-09-14T13:30:00")


def test_quotes_are_accepted_and_availability_is_the_declared_assumption():
    rows, rejected = normalize(doc(from_alpaca_quotes(RAW, "SPY", "2026-09-15T00:00:00Z")))
    assert rejected == [] and len(rows) == 2
    for row in rows:
        assert row["availability_basis"] == "QUOTE_LATENCY_ASSUMPTION_V1"
        assert row["available_epoch"] == row["event_epoch"] + QUOTE_LATENCY_SECONDS
        assert row["available_epoch"] > row["event_epoch"], "availability is never backdated to the event"


def test_sizes_are_shares_by_default_and_the_provider_value_is_retained():
    """The multiplier scales every displayed-size cap downstream, so the raw provider number stays visible."""
    rows = from_alpaca_quotes(RAW, "SPY", "R")
    assert (rows[0]["bid_size"], rows[0]["ask_size"]) == (40.0, 240.0)
    assert (rows[0]["provider_bid_size"], rows[0]["provider_ask_size"]) == (40, 240)
    lots = from_alpaca_quotes(RAW, "SPY", "R", round_lot_shares=100)
    assert lots[0]["bid_size"] == 4000.0 and lots[0]["provider_bid_size"] == 40
    assert lots[0]["round_lot_shares"] == 100


def test_a_stretched_latency_assumption_is_refused_by_name():
    row = dict(from_alpaca_quotes(RAW, "SPY", "R")[0])
    row["available_ns"] = row["event_ns"] + 5_000_000_000
    row["available_epoch"] = row["available_ns"] / 1e9
    _, rejected = normalize(doc([row]))
    assert rejected == [{"input_index": 0, "reason": "QUOTE_LATENCY_ASSUMPTION_DISAGREES"}]


def test_the_two_assumption_bases_cannot_be_swapped():
    quote = dict(from_alpaca_quotes(RAW, "SPY", "R")[0], availability_basis="BAR_COMPLETION_ASSUMPTION_V1")
    bar = {"kind": "bar", "symbol": "SPY", "event_epoch": 1789392600.0, "available_epoch": 1789738260.0,
           "availability_basis": "QUOTE_LATENCY_ASSUMPTION_V1",
           "open": 640.0, "high": 640.5, "low": 639.5, "close": 640.2, "volume": 1000.0}
    _, rejected = normalize(doc([quote, bar]))
    assert [r["reason"] for r in rejected] == ["BAR_ASSUMPTION_IS_NOT_QUOTE_AVAILABILITY",
                                               "QUOTE_ASSUMPTION_IS_NOT_BAR_AVAILABILITY"]


def test_a_quote_is_invisible_until_its_declared_latency_has_elapsed():
    rows, _ = normalize(doc(from_alpaca_quotes(RAW, "SPY", "R")))
    event = rows[0]["event_epoch"]
    assert visible(rows, now=event + 0.5, symbol="SPY", kind="quote")[0] == []
    assert len(visible(rows, now=event + QUOTE_LATENCY_SECONDS + 0.000001, symbol="SPY", kind="quote")[0]) == 1


def test_a_crossed_or_nonpositive_market_is_still_refused():
    crossed = dict(from_alpaca_quotes(RAW, "SPY", "R")[0], bid=640.20, ask=640.10)
    _, rejected = normalize(doc([crossed]))
    assert rejected == [{"input_index": 0, "reason": "INVALID_QUOTE_MARKET"}]


# ---------------------------------------------------------------- size units


def test_permitted_quantity_is_floored_at_the_raw_provider_size():
    """The multiplier is caller-supplied, and only an OVERSTATED one can hurt: it inflates permitted quantity.
    One provider unit is never fewer than one share, so the raw number is an independent ceiling and a wrong
    multiplier can only fail to expand size. Evidence and naming alone would not have closed this."""
    import numpy as np
    from apex.core import Config
    from apex.decision import evaluate
    prediction = {"created_epoch": 1787578205.0, "horizon_minutes": 5}
    paths = np.zeros((16, 1))
    config = Config(symbol="SPY", max_notional=100_000_000)
    honest = normalize(doc(from_alpaca_quotes(RAW, "SPY", "r")))[0][-1]
    inflated = normalize(doc(from_alpaca_quotes(RAW, "SPY", "r", round_lot_shares=100)))[0][-1]
    assert inflated["ask_size"] == 100 * honest["ask_size"]
    common = dict(cash=100_000_000, position_open=False, config=config)
    a = evaluate(prediction, paths, honest, None, **common)
    b = evaluate(prediction, paths, inflated, None, **common)
    assert a["quantity"] == b["quantity"] == 80, "a 100x multiplier buys no extra permitted quantity"


def test_the_floor_does_not_apply_where_no_raw_provider_size_was_recorded():
    """A fixture or a future adapter without the raw field keeps the converted size; the floor never invents one."""
    import numpy as np
    from apex.core import Config
    from apex.decision import evaluate
    quote = normalize(doc(from_alpaca_quotes(RAW, "SPY", "r", round_lot_shares=100)))[0][-1]
    quote.pop("provider_ask_size")
    result = evaluate({"created_epoch": 1.0, "horizon_minutes": 5}, np.zeros((16, 1)), quote, None,
                      cash=100_000_000, position_open=False, config=Config(symbol="SPY", max_notional=100_000_000))
    assert result["quantity"] == 8000
