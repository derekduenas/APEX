"""The measured-feed publisher: one consistent generation, a fetched calendar, and named refusals."""
import base64
import json
from pathlib import Path

import pytest

from apex.core import Refused
from apex.core import digest
from apex.paper_feed import (exchange_session, publish, publish_generation,
                             validate_capture_document, verify_generation)

SETTINGS = {"symbol": "SPY", "feed": "sip", "round_lot_shares": 1, "history_days": 7,
            "max_pages": 1, "request_timeout": 5, "variance": "ewma", "min_free_bytes": 1}
NOW = 1789565400.0                    # 2026-09-16 09:30:00 ET, a regular session
DAY = "2026-09-16"


def body(payload):
    return {"status": 200, "body_base64": base64.b64encode(json.dumps(payload).encode()).decode()}


def market(now_ns, *, quote_age_s=1.0, bars=390, minute=None):
    """Alpaca-shaped bodies whose stamps sit just behind a receipt at now_ns."""
    start = int(now_ns // 1_000_000_000) - bars * 60
    minute = minute if minute is not None else start
    rows = [{"t": _stamp((start + i * 60) * 1_000_000_000), "o": 640.0, "h": 640.5, "l": 639.5,
             "c": 640.0 + i * 0.001, "v": 1000} for i in range(bars)]
    quote_ns = int(now_ns - quote_age_s * 1_000_000_000)
    return rows, {"t": _stamp(quote_ns), "bp": 640.10, "ap": 640.12, "bs": 200, "as": 200}


def _stamp(ns):
    import datetime
    return datetime.datetime.fromtimestamp(ns / 1e9, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def transport_for(*, calendar=None, calendar_status=None, quote_age_s=1.0, bars=390, paginate=False,
                  clock=[0]):
    calls = []

    def transport(path, params, timeout):
        calls.append(path)
        if path == "/v2/calendar":
            if calendar_status is not None:
                return {"status": calendar_status, "error": "HTTP_ERROR"}
            rows = calendar if calendar is not None else [{"date": DAY, "open": "09:30", "close": "16:00"}]
            return body(rows)
        now_ns = clock[0] or int(NOW * 1e9)
        rows, quote = market(now_ns, quote_age_s=quote_age_s, bars=bars)
        if path == "/v2/stocks/bars":
            payload = {"bars": {"SPY": rows}}
            if paginate:
                payload["next_page_token"] = "more"
            return body(payload)
        if path == "/v2/stocks/bars/latest":
            return body({"bars": {"SPY": rows[-1]}})
        if path == "/v2/stocks/quotes/latest":
            return body({"quotes": {"SPY": quote}})
        raise AssertionError(path)
    transport.calls = calls
    return transport


def clock_from(ns):
    state = {"ns": ns}

    def tick():
        state["ns"] += 1_000_000
        return state["ns"]
    return tick


# ---------------------------------------------------------------- the calendar


def test_the_session_comes_from_the_provider_calendar_not_from_the_weekday_clock():
    session, evidence = exchange_session(now=NOW, transport=transport_for())
    assert session.calendar_source == "ALPACA_TRADING_CALENDAR_V2"
    assert session.calendar_id.endswith(DAY)
    assert evidence["provider_open"] == "09:30" and evidence["provider_close"] == "16:00"
    assert evidence["early_close"] is False
    assert evidence["raw_response_sha256"] and len(evidence["raw_response_sha256"]) == 64
    assert session.close_epoch - session.open_epoch == 6.5 * 3600


def test_a_holiday_is_an_absent_row_and_is_refused_rather_than_filled_in():
    """The failure that produced 13 spurious Labor Day requests: a weekday clock cannot see a closed exchange."""
    with pytest.raises(Refused, match="EXCHANGE_CLOSED_OR_CALENDAR_MISSING_DATE:" + DAY):
        exchange_session(now=NOW, transport=transport_for(calendar=[]))


def test_an_early_close_is_carried_through_rather_than_normalized_to_sixteen_hundred():
    session, evidence = exchange_session(
        now=NOW, transport=transport_for(calendar=[{"date": DAY, "open": "09:30", "close": "13:00"}]))
    assert evidence["early_close"] is True
    assert session.close_epoch - session.open_epoch == 3.5 * 3600


@pytest.mark.parametrize("rows,expected", [
    ([{"date": DAY, "open": "09:30", "close": "16:00"}, {"date": DAY, "open": "10:00", "close": "16:00"}],
     "CALENDAR_AMBIGUOUS_FOR_DATE"),
    ([{"date": DAY, "open": "not-a-time", "close": "16:00"}], "CALENDAR_SESSION_TIMES_INVALID"),
    ([{"date": DAY, "close": "16:00"}], "CALENDAR_SESSION_TIMES_INVALID"),
    ([{"date": DAY, "open": "16:00", "close": "09:30"}], "CALENDAR_SESSION_IMPLAUSIBLE"),
])
def test_a_malformed_calendar_is_refused_by_name(rows, expected):
    with pytest.raises(Refused, match=expected):
        exchange_session(now=NOW, transport=transport_for(calendar=rows))


def test_a_calendar_request_failure_is_refused_and_costs_no_capture():
    transport = transport_for(calendar_status=503)
    with pytest.raises(Refused, match="CALENDAR_REQUEST_FAILED"):
        exchange_session(now=NOW, transport=transport)
    assert transport.calls == ["/v2/calendar"], "no market data is fetched for a session that cannot be dated"


# ---------------------------------------------------------------- publication


def pair(root, marker, *, ns=None):
    """A ready-made generation. Documents are supplied directly because nothing can fake a measured capture."""
    rows = [{"kind": "quote", "symbol": "SPY", "event_epoch": NOW - 2, "available_epoch": NOW - 1,
             "event_ns": int((NOW - 2) * 1e9), "available_ns": int((NOW - 1) * 1e9),
             "availability_basis": "MEASURED_RECEIPT", "bid": 640.10, "ask": 640.12,
             "bid_size": 200, "ask_size": 200, "raw_response_sha256": "a" * 64,
             "provider_timestamp": "2026-09-16T13:29:58Z", "feed": "sip",
             "receipt_basis": "PARENT_PROCESS_RECEIPT_UPPER_BOUND"}]
    generation_id = digest({"marker": marker, "rows": rows})
    document = {"schema": "APEX_DATA_V1", "source": "ALPACA_MEASURED_CAPTURE_PUBLISHER_V1",
                "generation_id": generation_id, "observations": rows, "normalization_rejected": [],
                "provenance": {"raw_response_sha256": ["a" * 64], "retrieved_epoch": NOW, "feed": "sip"}}
    session_document = {"schema": "APEX_PAPER_SESSION_V1", "generation_id": generation_id,
                        "session": {"open_epoch": NOW, "close_epoch": NOW + 6.5 * 3600,
                                    "calendar_id": "ALPACA_TRADING_CALENDAR_V2:" + DAY,
                                    "calendar_source": "ALPACA_TRADING_CALENDAR_V2", "known_at_epoch": NOW}}
    return document, session_document, generation_id


def test_a_generation_publishes_a_matched_pair_behind_one_visible_swap(tmp_path):
    document, session_document, generation_id = pair(tmp_path, "first")
    summary = publish_generation(tmp_path, tmp_path / "generations" / "g1", document, session_document)
    current = tmp_path / "current"
    assert current.is_symlink(), "the pair becomes visible in one step, not two"
    assert verify_generation(json.loads((current / "input.json").read_bytes()),
                             json.loads((current / "session.json").read_bytes())) == generation_id
    assert summary["generation_id"] == generation_id


def test_publishing_refuses_to_write_a_pair_that_does_not_already_match(tmp_path):
    document, session_document, _ = pair(tmp_path, "first")
    session_document["generation_id"] = "different"
    with pytest.raises(Refused, match="FEED_GENERATION_MISMATCHED_PAIR"):
        publish_generation(tmp_path, tmp_path / "generations" / "g1", document, session_document)
    assert not (tmp_path / "current").exists(), "nothing becomes visible when the pair is already wrong"


def test_a_mismatched_pair_is_refused_by_name_rather_than_consumed():
    """Atomic replacement of each file alone cannot guarantee the pair; the id is what guarantees it."""
    with pytest.raises(Refused, match="FEED_GENERATION_MISMATCHED_PAIR"):
        verify_generation({"generation_id": "a"}, {"generation_id": "b"})
    with pytest.raises(Refused, match="FEED_GENERATION_ID_MISSING"):
        verify_generation({}, {"generation_id": "b"})


def test_an_interrupted_publication_leaves_the_previous_generation_intact(tmp_path, monkeypatch):
    document, session_document, first_id = pair(tmp_path, "first")
    publish_generation(tmp_path, tmp_path / "generations" / "g1", document, session_document)
    before = (tmp_path / "current").resolve()

    later, later_session, _ = pair(tmp_path, "second")
    import apex.paper_feed as F
    real = F._publish
    calls = {"n": 0}

    def die_between_the_two_files(path, payload):
        calls["n"] += 1
        if calls["n"] == 2:                       # input.json written, session.json not yet
            raise OSError("interrupted between the halves")
        return real(path, payload)
    monkeypatch.setattr(F, "_publish", die_between_the_two_files)
    with pytest.raises(OSError):
        F.publish_generation(tmp_path, tmp_path / "generations" / "g2", later, later_session)
    assert (tmp_path / "current").resolve() == before, "a failed run must not move the pointer"
    assert verify_generation(json.loads((tmp_path / "current" / "input.json").read_bytes()),
                             json.loads((tmp_path / "current" / "session.json").read_bytes())) == first_id
    assert not list(tmp_path.glob("**/*.tmp")), "no partial file is left where a reader could find it"


def test_a_later_generation_replaces_the_pointer_and_both_halves_move_together(tmp_path):
    a, a_session, first_id = pair(tmp_path, "first")
    publish_generation(tmp_path, tmp_path / "generations" / "g1", a, a_session)
    b, b_session, second_id = pair(tmp_path, "second")
    publish_generation(tmp_path, tmp_path / "generations" / "g2", b, b_session)
    assert first_id != second_id
    assert verify_generation(json.loads((tmp_path / "current" / "input.json").read_bytes()),
                             json.loads((tmp_path / "current" / "session.json").read_bytes())) == second_id


def test_restart_recovery_reads_the_last_good_pair_without_republishing(tmp_path):
    """A restarted consumer must find a whole generation on disk, not require the publisher to run first."""
    document, session_document, generation_id = pair(tmp_path, "first")
    publish_generation(tmp_path, tmp_path / "generations" / "g1", document, session_document)
    assert verify_generation(json.loads((Path(tmp_path) / "current" / "input.json").read_bytes()),
                             json.loads((Path(tmp_path) / "current" / "session.json").read_bytes())) == generation_id
    assert json.loads((tmp_path / "publisher-status.json").read_bytes())["generation_id"] == generation_id


def test_an_injected_transport_can_never_be_published_as_a_measured_feed(tmp_path):
    """The gate that makes the split necessary, asserted rather than assumed: capture labels anything fetched
    through an injected transport SYNTHETIC_CLOCK, so no test can manufacture a measured feed."""
    with pytest.raises(Refused, match="PUBLISHER_REQUIRES_MEASURED_RECEIPTS:SYNTHETIC_CLOCK"):
        publish(tmp_path, settings=SETTINGS, now=NOW, transport=transport_for(),
                clock_ns=clock_from(int(NOW * 1e9)))
    assert not (tmp_path / "current").exists()


def capture_document(*, quotes=1, bars=3, complete=True, basis="MEASURED_RECEIPT", quote_age_s=1.0):
    """What a real capture produces, built directly so the validator can be exercised on its own.

    This is a fixture OF a measured capture, not a claim to be one: `publish` still refuses to treat anything
    fetched through an injected transport as measured, which is asserted separately.
    """
    rows = []
    for i in range(bars):
        event = int(NOW) - (bars - i) * 60
        rows.append({"kind": "bar", "symbol": "SPY", "event_epoch": float(event),
                     "available_epoch": float(event + 60), "event_ns": event * 10 ** 9,
                     "available_ns": (event + 60) * 10 ** 9, "availability_basis": basis,
                     "open": 640.0, "high": 640.5, "low": 639.5, "close": 640.2, "volume": 1000.0,
                     "raw_response_sha256": "b" * 64})
    for _ in range(quotes):
        event_ns = int((NOW - quote_age_s) * 1e9)
        rows.append({"kind": "quote", "symbol": "SPY", "event_epoch": event_ns / 1e9,
                     "available_epoch": (event_ns + 10 ** 9) / 1e9, "event_ns": event_ns,
                     "available_ns": event_ns + 10 ** 9, "availability_basis": basis,
                     "bid": 640.10, "ask": 640.12, "bid_size": 200, "ask_size": 200,
                     "raw_response_sha256": "c" * 64})
    return {"schema": "APEX_DATA_V1", "source": "ALPACA_REST_CAPTURE", "observations": rows,
            "history_pagination_exhausted": complete}


def test_incomplete_history_is_refused_instead_of_shortening_the_training_window():
    with pytest.raises(Refused, match="CAPTURE_HISTORY_INCOMPLETE"):
        validate_capture_document(capture_document(complete=False), now=NOW)


def test_a_capture_with_no_quote_is_refused_because_no_decision_could_ever_use_it():
    with pytest.raises(Refused, match="CAPTURE_CONTAINS_NO_QUOTE"):
        validate_capture_document(capture_document(quotes=0), now=NOW)


def test_an_empty_capture_is_refused_by_name():
    with pytest.raises(Refused, match="CAPTURE_RETURNED_NO_MARKET_DATA"):
        validate_capture_document(capture_document(quotes=0, bars=0), now=NOW)


def test_unmeasured_rows_are_refused_at_the_source():
    with pytest.raises(Refused, match="PUBLISHER_REQUIRES_MEASURED_RECEIPTS:SYNTHETIC_CLOCK"):
        validate_capture_document(capture_document(basis="SYNTHETIC_CLOCK"), now=NOW)


def test_a_stale_quote_is_measured_and_published_rather_than_hidden():
    """Refusing to publish would replace the reader's recorded QUOTE_STALE refusal with silence."""
    health = validate_capture_document(capture_document(quote_age_s=45.0), now=NOW)
    assert health["latest_quote_age_s"] == pytest.approx(45.0)
    assert health["quotes"] == 1 and health["bars"] == 3
    assert health["latest_bar_age_s"] == pytest.approx(60.0)


def test_a_fresh_quote_reports_its_age_for_the_commissioning_gate():
    health = validate_capture_document(capture_document(quote_age_s=1.25), now=NOW)
    assert health["latest_quote_age_s"] == pytest.approx(1.25)
    assert health["latest_quote_event_ns"] == int((NOW - 1.25) * 1e9)


def test_the_stale_quote_that_was_published_is_then_refused_by_the_decision_reader():
    """The two halves of the split, shown together: the publisher records it, the reader refuses it."""
    from apex.core import Config
    from apex.data import normalize
    from apex.decision import quote_at
    document = capture_document(quote_age_s=45.0)
    validate_capture_document(document, now=NOW)
    rows, rejected = normalize(document)
    assert rejected == []
    quote, problem = quote_at(rows, NOW, Config(symbol="SPY"))
    assert quote is None and problem == "QUOTE_STALE"
    fresh, _ = normalize(capture_document(quote_age_s=1.25))
    usable, problem = quote_at(fresh, NOW, Config(symbol="SPY"))
    assert problem is None and usable["ask"] == 640.12


# ---------------------------------------------------------------- publisher -> paper service


def published_pair(tmp_path, document):
    """Run a capture-shaped document through the publisher and hand back what the service would read."""
    generation_id = digest({"observations": document["observations"]})
    health = validate_capture_document(document, now=NOW)
    input_document = dict(document, generation_id=generation_id,
                          source="ALPACA_MEASURED_CAPTURE_PUBLISHER_V1",
                          provenance={"feed_health": health, "retrieved_epoch": NOW})
    session_document = {"schema": "APEX_PAPER_SESSION_V1", "generation_id": generation_id,
                        "session": {"open_epoch": NOW, "close_epoch": NOW + 6.5 * 3600,
                                    "calendar_id": "ALPACA_TRADING_CALENDAR_V2:" + DAY,
                                    "calendar_source": "ALPACA_TRADING_CALENDAR_V2", "known_at_epoch": NOW}}
    publish_generation(tmp_path, tmp_path / "generations" / ("g" + generation_id[:8]),
                       input_document, session_document)
    return (json.loads((tmp_path / "current" / "input.json").read_bytes()),
            json.loads((tmp_path / "current" / "session.json").read_bytes()))


def test_a_published_generation_satisfies_the_paper_services_own_live_source_gate(tmp_path):
    """The integration contract: the service's gate, not a restatement of it, decides the feed is usable."""
    from apex.data import normalize
    from apex.paper_runtime import _source_problem
    document, session_document = published_pair(tmp_path, capture_document())
    rows, rejected = normalize(document)
    assert _source_problem(document, rows, rejected, "LIVE_PAPER") is None
    assert verify_generation(document, session_document)


def test_a_generation_carrying_synthetic_rows_is_blocked_by_that_same_gate(tmp_path):
    """Belt and braces: the publisher refuses to build it, and the service refuses to consume it."""
    from apex.data import normalize
    from apex.paper_runtime import _source_problem
    synthetic = capture_document(basis="SYNTHETIC_CLOCK")
    with pytest.raises(Refused, match="PUBLISHER_REQUIRES_MEASURED_RECEIPTS"):
        validate_capture_document(synthetic, now=NOW)
    rows, rejected = normalize(synthetic)
    assert _source_problem(synthetic, rows, rejected, "LIVE_PAPER") == "BLOCKED_SYNTHETIC_DATA_IN_NON_SYNTHETIC_MODE"


def test_a_decision_snapshot_document_can_never_be_published_as_a_paper_feed(tmp_path):
    """Historical decision quotes are explicitly not execution evidence; the service blocks them by name."""
    from apex.data import normalize
    from apex.paper_runtime import _source_problem
    document = dict(capture_document(), evidence_scope="DECISION_SNAPSHOTS_ONLY")
    rows, rejected = normalize(document)
    assert _source_problem(document, rows, rejected, "LIVE_PAPER") == "BLOCKED_DECISION_SNAPSHOTS_NOT_EXECUTION_EVIDENCE"


def test_an_incomplete_collection_marker_also_blocks_the_live_gate(tmp_path):
    from apex.data import normalize
    from apex.paper_runtime import _source_problem
    document = dict(capture_document(), collection_status="INCOMPLETE")
    with pytest.raises(Refused, match="INCOMPLETE_QUOTE_COLLECTION"):
        normalize(document)
    assert _source_problem(document, [], [], "LIVE_PAPER") == "BLOCKED_INCOMPLETE_QUOTE_COLLECTION"


# ---------------------------------------------------------------- review round two


def test_the_measured_path_requires_original_nanoseconds_even_when_seconds_look_whole():
    """An integer-valued float is not a declared integer clock.

    A true stamp of ...200_000000123 nanoseconds rounds to exactly 1787578200.0, so integrality proves nothing
    about what was lost. `exact_ns` still accepts a whole-second DECLARATION, but the measured publisher path
    does not rely on that: it requires the integers the provider actually sent.
    """
    lossy_true_stamp = 1787578200_000000123
    assert float(lossy_true_stamp / 1e9).is_integer(), "an integer-valued float can still have lost digits"
    document = capture_document()
    for row in document["observations"]:
        row["event_epoch"] = float(int(row["event_epoch"]))
        row["available_epoch"] = float(int(row["available_epoch"]))
        row.pop("event_ns")
        row.pop("available_ns")
    with pytest.raises(Refused, match="MEASURED_ROWS_REQUIRE_ORIGINAL_NANOSECONDS"):
        validate_capture_document(document, now=NOW)


def test_a_swap_between_the_two_reads_cannot_split_a_generation(tmp_path):
    """The window an atomic symlink swap leaves open, closed by resolving once.

    The consumer used to resolve `current` twice, once per file. A publication landing between those two opens
    would hand it one half of each generation. This publishes a second generation in the middle of the read and
    asserts the pair still matches.
    """
    from apex import paper_feed
    a, a_session, first_id = pair(tmp_path, "first")
    publish_generation(tmp_path, tmp_path / "generations" / "g1", a, a_session)
    b, b_session, second_id = pair(tmp_path, "second")
    assert first_id != second_id

    real_read_bytes = Path.read_bytes
    swapped = {"done": False}

    def swap_after_the_first_half(self, *args, **kwargs):
        data = real_read_bytes(self, *args, **kwargs)
        if self.name == "input.json" and not swapped["done"]:
            swapped["done"] = True                      # a publication lands between the two opens
            publish_generation(tmp_path, tmp_path / "generations" / "g2", b, b_session)
        return data
    original = paper_feed.Path.read_bytes
    try:
        paper_feed.Path.read_bytes = swap_after_the_first_half
        generation, document, session_document = paper_feed.read_generation(tmp_path)
    finally:
        paper_feed.Path.read_bytes = original
    assert swapped["done"], "the test must actually have swapped mid-read"
    assert verify_generation(document, session_document) == first_id
    assert generation.name == "g1", "the pinned directory is the one that was resolved, not the newest"
    # And the newest generation is what a fresh read now sees.
    _, later, later_session = paper_feed.read_generation(tmp_path)
    assert verify_generation(later, later_session) == second_id


def test_a_failed_publication_is_recorded_so_the_previous_generation_cannot_pass_for_fresh(tmp_path):
    from apex.paper_feed import feed_state
    document, session_document, first_id = pair(tmp_path, "first")
    document["provenance"] = {"retrieved_epoch": NOW}
    publish_generation(tmp_path, tmp_path / "generations" / "g1", document, session_document,
                       extra={"status": "PUBLISHED"})
    fresh = feed_state(tmp_path, now=NOW + 5)
    assert fresh["generation_id"] == first_id and fresh["publisher_status"] == "PUBLISHED"
    assert fresh["generation_age_s"] == pytest.approx(5.0)

    # Any publication failure will do here; this one is refused for not being a measured feed.
    with pytest.raises(Refused, match="PUBLISHER_REQUIRES_MEASURED_RECEIPTS"):
        publish(tmp_path, settings=SETTINGS, now=NOW + 60,
                transport=transport_for(), clock_ns=clock_from(int((NOW + 60) * 1e9)))
    after = feed_state(tmp_path, now=NOW + 900)
    assert after["generation_id"] == first_id, "the previous generation stays readable for servicing"
    assert after["publisher_status"] == "FAILED" and after["publisher_reason"]
    assert after["generation_age_s"] == pytest.approx(900.0), "and its age says plainly that it is not fresh"


# ------------------------------------------- publisher failure through the service to the account


def test_publisher_failure_keeps_servicing_an_open_position_and_fabricates_no_fill(tmp_path):
    """The whole path, not its halves: an open obligation exists, the publisher then fails, and the account is
    serviced from a feed that has nothing new in it.

    What must hold is narrow and specific. The position is still there. Nothing is filled from a feed that
    supplied no quote. The exposure is reported rather than quietly netted away. And the feed's own age and the
    publisher's last outcome are both visible, so a generation from before the failure cannot read as current.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from decimal import Decimal
    from apex import paper_runtime as runtime
    from test_paper_runtime import fixture, once

    document, start, calendar, config = fixture()
    root = tmp_path / "account"
    once(root, document, start, calendar, config)            # submits the entry obligation
    opened = once(root, document, start + 60, calendar, config)   # the fill lands on a later tick
    account = opened["account"]
    assert account["fills"] >= 1 and account["positions"], "a real position must exist before the feed fails"
    position_before = json.loads(json.dumps(account["positions"]))
    realized_before = account["realized_net"]

    # The publisher now fails. The previous generation stays on disk, which is correct, and is recorded as old.
    feed_root = tmp_path / "feed"
    published, session_document, generation_id = pair(feed_root, "before-the-failure")
    published["provenance"] = {"retrieved_epoch": NOW}
    publish_generation(feed_root, feed_root / "generations" / "g1", published, session_document,
                       extra={"status": "PUBLISHED"})
    with pytest.raises(Refused):
        publish(feed_root, settings=SETTINGS, now=NOW + 60, transport=transport_for(),
                clock_ns=clock_from(int((NOW + 60) * 1e9)))

    from apex.paper_feed import feed_state
    state = feed_state(feed_root, now=NOW + 1800)
    assert state["publisher_status"] == "FAILED"
    assert state["generation_id"] == generation_id
    assert state["generation_age_s"] == pytest.approx(1800.0), "age is what says this is not current health"

    # Service the account with a feed that carries no usable quote, as the runtime does when input is unavailable.
    blocked = {"schema": "APEX_DATA_V1", "source": "UNAVAILABLE_LIVE_FILE_FEED",
               "status": "BLOCKED_NO_MARKET_DATA", "observations": []}
    serviced = once(root, blocked, start + 120, calendar, config)
    after = serviced["account"]
    assert after["fills"] == account["fills"], "no fill may be invented from a feed that supplied no quote"
    assert after["realized_net"] == realized_before, "and nothing may be realized either"
    # The exposure is still reported, and its identity is untouched.
    identity = ("symbol", "quantity", "cost_basis", "entry_order_id", "entry_epoch")
    assert [{k: p[k] for k in identity} for p in after["positions"]] == \
           [{k: p[k] for k in identity} for p in position_before]
    # The marks do not carry forward and are not invented: they go absent, and say why.
    held = after["positions"][0]
    assert held["mark_status"] == "STALE_QUOTE"
    assert held["bid_mark"] is None and held["market_value"] is None
    assert held["unrealized_net"] is None and held["estimated_liquidation_value"] is None
    assert position_before[0]["mark_status"] == "MARKED" and position_before[0]["bid_mark"] is not None

    # The account's own arithmetic still reconstructs independently while the feed is failing.
    verification = runtime.report(root, now=start + 120)["verification"]
    assert verification["status"] == "VALID"


def test_order_expiry_still_runs_while_the_publisher_is_failing(tmp_path):
    """An unfilled order must not sit open forever because the feed stopped; expiry is the account's own clock."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from apex import paper_runtime as runtime
    from test_paper_runtime import fixture, once

    document, start, calendar, config = fixture(quotes=False)
    root = tmp_path / "account"
    first = once(root, document, start, calendar, config)
    assert first["account"]["fills"] == 0, "no quote means no fill"
    blocked = {"schema": "APEX_DATA_V1", "source": "UNAVAILABLE_LIVE_FILE_FEED",
               "status": "BLOCKED_NO_MARKET_DATA", "observations": []}
    later = once(root, blocked, start + 900, calendar, config)
    assert later["account"]["fills"] == 0
    assert runtime.report(root, now=start + 900)["verification"]["status"] == "VALID"
