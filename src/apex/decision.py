"""Shared quote selection and experimental candidate math for replay and shadow."""
from decimal import Decimal

import numpy as np

from .core import Config, fee, finite, money
from .data import visible, event_ns, epoch_ns, duration_ns, decision_cutoff_ns


def quote_at(observations, now, config: Config, *, now_ns=None):
    quotes, conflicts = visible(observations, now=now, symbol=config.symbol, kind="quote", now_ns=now_ns)
    if not quotes:
        return None, "QUOTE_UNAVAILABLE_OR_CONFLICTING"
    quote = quotes[-1]
    if any(c.get("event_ns", epoch_ns(c["event_epoch"])) >= event_ns(quote) for c in conflicts):
        return None, "LATEST_QUOTE_CONFLICT"
    if decision_cutoff_ns(now, now_ns) - event_ns(quote) > duration_ns(config.max_quote_age):
        return None, "QUOTE_STALE"
    return quote, None


def evaluate(prediction, paths, quote, quote_problem, *, cash, position_open, config):
    """One pure evaluator. Returning a candidate grants no execution authority."""
    qty, expected, probability = 0, None, None
    price_anchor = None
    reason = quote_problem
    if position_open:
        reason = "EXISTING_POSITION_OR_OUTSTANDING_EXIT"
    elif quote:
        budget = min(cash, money(config.max_notional))
        qty = int(budget / Decimal(str(quote["ask"])))
        while qty and money(Decimal(str(quote["ask"])) * qty) + fee(qty, config) > budget:
            qty -= 1
        # CONSERVATIVE RAW-SIZE CEILING. `ask_size` is the adapter's CONVERTED size, and the conversion rests on a
        # caller-supplied multiplier. An overstated multiplier would inflate permitted quantity, which is the one
        # direction of error that can hurt. The provider's own raw number is therefore an independent ceiling:
        # whatever the unit turns out to be, one provider unit is never fewer than one share, so a multiplier
        # that is wrong can only fail to expand size here -- it can never expand it past what was displayed.
        displayed = int(quote["ask_size"])
        raw_size = quote.get("provider_ask_size", quote.get("provider_ask_lots"))
        if finite(raw_size):
            displayed = min(displayed, int(raw_size))
        qty = min(qty, displayed)
        if qty <= 0:
            reason = "WHOLE_SHARE_OR_DISPLAYED_SIZE_UNAFFORDABLE"
        else:
            # The quote reveals today's current market. A stale bar must not
            # imply a rebound to its old price. Reuse the H-minute return law
            # prospectively from this quote midpoint, with its own target.
            midpoint = (quote["ask"] + quote["bid"]) / 2
            price_anchor = {"basis": "CURRENT_QUOTE_MIDPOINT", "spot": midpoint,
                            "quote_id": quote["observation_id"], "event_epoch": quote["event_epoch"],
                            "decision_epoch": prediction["created_epoch"],
                            "target_epoch": prediction["created_epoch"] + prediction["horizon_minutes"] * 60,
                            "assumption": "Same H-minute return law rebased at decision; distinct from bar-origin forecast"}
            bids = midpoint * np.exp(paths[:, -1]) - (quote["ask"] - quote["bid"]) / 2
            net_paths = (bids - quote["ask"]) * qty - float(fee(qty, config) * 2)
            expected, probability = float(net_paths.mean()), float((net_paths > 0).mean())
            reason = "AFTER_COST_MODEL_NOT_ELIGIBLE" if expected <= 0 or probability < config.minimum_probability else None
    elif reason is None:
        reason = "QUOTE_UNAVAILABLE_OR_CONFLICTING"
    return {"decision": "WAIT" if reason else "EXPERIMENTAL_LONG", "reason": reason, "quantity": qty,
            "expected_net": expected, "model_probability_net_positive": probability, "competitor": "WAIT",
            "price_anchor": price_anchor,
            "instrument": "FUNDED_LONG_STOCK", "authority": "OFFLINE_EXPERIMENT_ONLY", "calibration": "UNCALIBRATED",
            "cost_assumption": "Current half-spread retained at exit, displayed quantity; no queue or impact model",
            "sizing": "Fixed purchase-cost ceiling including entry fee; no Kelly; stop not treated as a loss bound"}
