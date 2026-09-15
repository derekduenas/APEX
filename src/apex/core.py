"""Small shared contracts. JSON is finite and canonical; money uses Decimal."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


class Refused(ValueError):
    pass


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(obj) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Config:
    symbol: str = "SPY"
    starting_cash: str = "10000.00"
    max_notional: str = "1000.00"
    horizon_minutes: int = 15
    scan_minutes: int = 15
    training_returns: int = 780
    paths: int = 1000
    seed: int = 41
    max_quote_age: int = 15
    exit_window_seconds: int = 120
    # A research policy, never a production capital authorization.
    minimum_probability: float = 0.55
    variance: str = "garch"
    commission_per_share: str = "0.005"
    minimum_commission: str = "0.01"
    fee_basis: str = "ILLUSTRATIVE_COST_ASSUMPTION_V1"

    def __post_init__(self):
        for name in ("horizon_minutes", "scan_minutes", "training_returns", "paths", "max_quote_age", "exit_window_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise Refused("INVALID_CONFIG:" + name)
        if self.training_returns < 200 or self.paths < 100:
            raise Refused("INSUFFICIENT_DECLARED_MODEL_BUDGET")
        if self.variance not in ("garch", "ewma") or not 0.5 <= self.minimum_probability < 1:
            raise Refused("INVALID_MODEL_POLICY")
        for name in ("starting_cash", "max_notional", "commission_per_share", "minimum_commission"):
            value = Decimal(getattr(self, name))
            if not value.is_finite() or value < 0:
                raise Refused("INVALID_MONEY_CONFIG:" + name)
        if money(self.starting_cash) <= 0 or money(self.max_notional) <= 0:
            raise Refused("NONPOSITIVE_CAPITAL")

    def record(self):
        return asdict(self)


def fee(qty: int, config: Config) -> Decimal:
    return money(max(Decimal(config.minimum_commission), Decimal(config.commission_per_share) * qty))


def code_manifest() -> dict:
    root = Path(__file__).parent
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*.py"))}
