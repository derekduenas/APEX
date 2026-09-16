"""An explicitly artificial drift market, designed to exercise execution."""
from datetime import datetime, timezone
import base64

import numpy as np

from .reused.vol_models import simulate_garch


def strategy_demo_document(*, world="persistent", seed=731):
    """Existing control worlds plus a factual, unmistakably synthetic premarket.

    The synthetic morning ends at the already generated cash-open price; it
    adds prior context, not an additional planted intraday edge or fitted seed.
    """
    from .data import session
    doc, plan = research_demo_document(world=world, seed=seed)
    starts = {}
    for row in doc["observations"]:
        if row["kind"] == "bar":
            starts.setdefault(session(row["event_epoch"]), row)
    for bar in starts.values():
        price = bar["open"]
        for minute in range(60):
            event = bar["event_epoch"] - 3600 + minute * 60
            left = price * (1 - .003 * (60 - minute) / 60)
            right = price * (1 - .003 * (59 - minute) / 60)
            doc["observations"].append({"kind": "bar", "symbol": "SPY", "event_epoch": event,
                "available_epoch": event + 60, "availability_basis": "SYNTHETIC_CLOCK", "open": left,
                "high": right + .005, "low": left - .005, "close": right, "volume": 200})
    doc["source"] = "SYNTHETIC_STRATEGY_LAB_" + world.upper() + "_NOT_MARKET_EDGE"
    return doc, plan


def research_demo_document(*, world="persistent", seed=731):
    """Twelve artificial sessions: four warmup, four development, four holdout.

    The persistent and reversal worlds contain an engineered lag relationship;
    the null has independent zero-mean innovations. None is market evidence.
    """
    from datetime import timedelta
    from .research import ResearchPlan
    if world not in ("persistent", "null", "reversal", "positive"):
        raise ValueError("Unknown synthetic research world")
    rng = np.random.default_rng(seed)
    day = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
    starts = []
    while len(starts) < 12:
        if day.weekday() < 5:
            starts.append(day.timestamp())
        day += timedelta(days=1)
    price, observations = 100., []
    for day_index, start in enumerate(starts):
        previous = 0.
        coefficient = 0. if world in ("null", "positive") else -.65 if world == "reversal" and day_index >= 8 else .75
        for minute in range(390):
            event = start + minute * 60
            old = price
            # Deliberately obvious positive control: a fixed known drift and
            # independent noise. It proves the selector can act, not market edge.
            previous = (.0003 if world == "positive" else 0.) + coefficient * previous + float(rng.normal(0, .00015 if world == "positive" else .00035))
            price *= float(np.exp(previous))
            observations.append({"kind": "bar", "symbol": "SPY", "event_epoch": event, "available_epoch": event + 60,
                                 "availability_basis": "SYNTHETIC_CLOCK", "open": old, "high": max(old, price) + .01,
                                 "low": min(old, price) - .01, "close": price, "volume": int(rng.integers(500, 2000))})
            observations.append({"kind": "quote", "symbol": "SPY", "event_epoch": event + 60, "available_epoch": event + 60,
                                 "availability_basis": "SYNTHETIC_CLOCK", "bid": price - .01, "ask": price + .01,
                                 "bid_size": 100, "ask_size": 100})
    plan = ResearchPlan(start=starts[0], development_start=starts[4], holdout_start=starts[8], end=starts[-1] + 390 * 60)
    return {"schema": "APEX_DATA_V1", "source": "SYNTHETIC_RESEARCH_" + world.upper() + "_NOT_MARKET_EDGE",
            "synthetic_world": world, "generator_seed": seed, "observations": observations}, plan


def demo_document(*, drift=0.0003, quotes=True) -> tuple[dict, float, float]:
    starts = [datetime(2026, 9, 10, 13, 30, tzinfo=timezone.utc).timestamp(),
              datetime(2026, 9, 11, 13, 30, tzinfo=timezone.utc).timestamp()]
    residuals = simulate_garch(n=480, omega=5e-9, alpha=.07, beta=.88, nu=7, seed=12)
    price, observations, index = 100.0, [], 0
    for start, length in zip(starts, (390, 90)):
        for minute in range(length):
            event = start + minute * 60
            old, price = price, price * float(np.exp(drift + residuals[index]))
            index += 1
            observations.append({"kind": "bar", "symbol": "SPY", "event_epoch": event, "available_epoch": event + 60,
                                 "availability_basis": "SYNTHETIC_CLOCK", "open": old, "high": max(old, price) + .01,
                                 "low": min(old, price) - .01, "close": price, "volume": 1000})
            if quotes:
                observations.append({"kind": "quote", "symbol": "SPY", "event_epoch": event + 60, "available_epoch": event + 60,
                                     "availability_basis": "SYNTHETIC_CLOCK", "bid": round(price - .01, 4), "ask": round(price + .01, 4),
                                     "bid_size": 100, "ask_size": 100})
    return {"schema": "APEX_DATA_V1", "source": "SYNTHETIC_POSITIVE_DRIFT_EXECUTION_FIXTURE_NOT_EDGE", "observations": observations}, starts[1] + 5 * 60, starts[1] + 65 * 60


def capture_demo(out, *, variance="garch"):
    """Run the real capture entry point with only clock and transport replaced."""
    from .capture import capture_alpaca
    from .core import Config, canonical
    document, start, _ = demo_document()
    ns = [int(start * 1e9)]
    bars = [row for row in document["observations"] if row["kind"] == "bar" and row["available_epoch"] <= start]
    raw_bars = [{"t": datetime.fromtimestamp(b["event_epoch"], timezone.utc).isoformat(),
                 **{short: b[long] for short, long in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}}
                for b in bars]
    q = next(row for row in document["observations"] if row["kind"] == "quote" and row["event_epoch"] == start)
    raw_quote = {"t": datetime.fromtimestamp(start, timezone.utc).isoformat(), "bp": q["bid"], "ap": q["ask"], "bs": 1, "as": 1}

    def transport(path, params, timeout):
        ns[0] += 10_000_000
        if path.endswith("quotes/latest"):
            payload = {"quotes": {"SPY": raw_quote}}
        elif path.endswith("bars/latest"):
            payload = {"bars": {"SPY": raw_bars[-1]}}
        else:
            payload = {"bars": {"SPY": raw_bars}, "next_page_token": None}
        return {"status": 200, "body_base64": base64.b64encode(canonical(payload).encode()).decode()}

    return capture_alpaca(out, history_start="2026-09-09T13:30:00Z", config=Config(variance=variance), feed="sip",
                          round_lot_shares=100, transport=transport, clock_ns=lambda: ns[0])
