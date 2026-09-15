"""An explicitly artificial drift market, designed to exercise execution."""
from datetime import datetime, timezone
import base64

import numpy as np

from .reused.vol_models import simulate_garch


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
