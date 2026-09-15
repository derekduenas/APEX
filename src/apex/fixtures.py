"""An explicitly artificial drift market, designed to exercise execution."""
from datetime import datetime, timezone

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
