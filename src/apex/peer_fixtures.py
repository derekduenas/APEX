"""Declared artificial peer-shock controls, never observed market evidence."""
from datetime import datetime, timedelta, timezone

import numpy as np

from .research import ResearchPlan


def peer_demo_document(*, world="catchup", seed=9041):
    """Twelve sessions; continuation deliberately reverses the planted holdout.

    Market and sector have contemporaneous shocks. The target initially lags,
    then starts recovering before the scan and finishes afterwards. Null removes
    the post-scan recovery. Continuation reverses it in the four holdout sessions.
    All three worlds share innovations; no seed or rule is selected by results.
    """
    if world not in ("catchup", "null", "continuation"):
        raise ValueError("Unknown synthetic peer world")
    rng = np.random.default_rng(seed)
    day = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
    starts = []
    while len(starts) < 12:
        if day.weekday() < 5:
            starts.append(day.timestamp())
        day += timedelta(days=1)
    observations, prices = [], {"AAPL": 100., "SPY": 400., "XLK": 180.}
    for day_index, start in enumerate(starts):
        market_base = rng.normal(0, .00018, 390)
        sector_base = .5 * market_base + rng.normal(0, .00015, 390)
        target = .8 * market_base + .7 * sector_base + rng.normal(0, .00004, 390)
        market, sector = market_base.copy(), sector_base.copy()
        for scan in range(150, 361, 30):
            amplitude = float(rng.uniform(.0008, .0014))
            market[scan-6:scan-1] += amplitude
            sector[scan-6:scan-1] += amplitude * .8
            target[scan-6:scan-1] += amplitude * .15
            target[scan-1] += amplitude * .35  # known recovery before decision
            response = 0. if world == "null" else -1. if world == "continuation" and day_index >= 8 else 1.
            target[scan:scan+15] += response * amplitude * 4.5 / 15
        for symbol, increments in (("AAPL", target), ("SPY", market), ("XLK", sector)):
            for minute, increment in enumerate(increments):
                event = start + minute * 60
                old = prices[symbol]
                prices[symbol] *= float(np.exp(increment))
                price = prices[symbol]
                observations.append({"kind": "bar", "symbol": symbol, "event_epoch": event,
                    "available_epoch": event + 60, "availability_basis": "SYNTHETIC_CLOCK",
                    "open": old, "high": max(old, price) + .01, "low": min(old, price) - .01,
                    "close": price, "volume": 1000})
    plan = ResearchPlan(start=starts[0], development_start=starts[4], holdout_start=starts[8],
        end=starts[-1] + 390 * 60, minimum_training_labels=10)
    return {"schema": "APEX_DATA_V1", "source": "SYNTHETIC_PEER_" + world.upper() + "_NOT_MARKET_EDGE",
            "synthetic_world": world, "generator_seed": seed,
            "control_design": "Fixed positive peer shocks; delayed target response planted, absent, or reversed in holdout",
            "observations": observations}, plan
