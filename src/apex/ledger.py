"""An exclusive, append-only replay ledger and a separate accounting reader.

Hashes detect changes relative to a retained head, not an adversary who can
replace every artifact. This is not a broker ledger or a signed release.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .core import Config, Refused, canonical, digest, finite


class Ledger:
    def __init__(self, path: Path):
        self.handle = path.open("x", encoding="utf-8")
        self.head, self.seq = "GENESIS", 0

    def append(self, kind: str, epoch: float, payload: dict) -> dict:
        row = {"seq": self.seq + 1, "prev_hash": self.head, "kind": kind, "epoch": epoch, "payload": payload}
        row["hash"] = digest(row)
        self.handle.write(canonical(row) + "\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.head, self.seq = row["hash"], row["seq"]
        return row

    def close(self):
        self.handle.close()


def read_verified(path: Path) -> list[dict]:
    rows, prev, last_epoch = [], "GENESIS", float("-inf")
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise Refused("LEDGER_ROW_INVALID") from exc
        if (not isinstance(row, dict) or set(row) != {"seq", "prev_hash", "kind", "epoch", "payload", "hash"}
                or type(row["seq"]) is not int or not isinstance(row["kind"], str) or not isinstance(row["payload"], dict)):
            raise Refused("LEDGER_ROW_INVALID")
        hashed = dict(row)
        supplied = hashed.pop("hash", None)
        if row.get("seq") != len(rows) + 1 or row.get("prev_hash") != prev or supplied != digest(hashed):
            raise Refused("LEDGER_HASH_CHAIN_INVALID")
        if not finite(row.get("epoch")) or row["epoch"] < last_epoch:
            raise Refused("LEDGER_CLOCK_REWIND")
        prev, last_epoch = supplied, row["epoch"]
        rows.append(row)
    if not rows or rows[0]["kind"] != "RUN_OPEN":
        raise Refused("RUN_OPEN_MISSING")
    return rows


def reconstruct(path: Path) -> dict:
    """Primary quotes + quantity + declared cost terms; never producer totals.

    All references must name earlier evidence of the right kind. This reader
    deliberately does not call the producer fee or cash calculation functions.
    """
    rows = read_verified(path)
    cfg = rows[0]["payload"]["config"]
    Config(**cfg)  # Declared cost/capital terms must pass the input contract too.
    cent = Decimal("0.01")

    def cents(value):
        return Decimal(str(value)).quantize(cent, rounding=ROUND_HALF_UP)

    def commission(quantity):
        return max(Decimal(cfg["minimum_commission"]), Decimal(cfg["commission_per_share"]) * quantity).quantize(cent, rounding=ROUND_HALF_UP)

    cash = cents(cfg["starting_cash"])
    seen, positions, closed, problems = {}, {}, [], []
    used_candidates = set()
    for row in rows:
        p, kind = row["payload"], row["kind"]
        if kind in ("ENTRY", "EXIT"):
            try:
                quote_row = seen[p["quote_ref"]]
                if quote_row["kind"] != "QUOTE":
                    raise Refused("WRONG_QUOTE_REFERENCE")
                quote = quote_row["payload"]
                if quote["symbol"] != cfg["symbol"] or not quote["available_epoch"] <= quote_row["epoch"] <= row["epoch"] or not 0 <= row["epoch"] - quote["event_epoch"] <= cfg["max_quote_age"]:
                    raise Refused("QUOTE_NOT_VISIBLE_OR_FRESH")
                if not all(finite(quote.get(k)) for k in ("bid", "ask", "bid_size", "ask_size")) or not 0 < quote["bid"] <= quote["ask"]:
                    raise Refused("INVALID_QUOTE")
                qty = p["quantity"]
                if type(qty) is not int or qty <= 0:
                    raise Refused("INVALID_QUANTITY")
                px = quote["ask"] if kind == "ENTRY" else quote["bid"]
                if quote["ask_size" if kind == "ENTRY" else "bid_size"] < qty:
                    raise Refused("INSUFFICIENT_DISPLAYED_SIZE")
                amount = cents(Decimal(str(px)) * qty)
                charge = commission(qty)
                if kind == "ENTRY":
                    candidate = seen[p["candidate_ref"]]
                    if candidate["kind"] != "CANDIDATE" or candidate["payload"]["decision"] != "EXPERIMENTAL_LONG" or candidate["payload"]["quantity"] != qty:
                        raise Refused("ENTRY_NOT_CANDIDATE_BOUND")
                    decision = candidate["payload"]
                    if (candidate["epoch"] != row["epoch"] or decision.get("quote_ref") != p["quote_ref"]
                            or decision.get("reason") is not None or not finite(decision.get("expected_net"))
                            or decision["expected_net"] <= 0 or not finite(decision.get("model_probability_net_positive"))
                            or not cfg["minimum_probability"] <= decision["model_probability_net_positive"] <= 1):
                        raise Refused("ENTRY_CANDIDATE_CONTRACT_MISMATCH")
                    if positions or amount + charge > cash or amount + charge > cents(cfg["max_notional"]):
                        raise Refused("CAPITAL_OR_POSITION_LIMIT")
                    if p["candidate_ref"] in used_candidates:
                        raise Refused("DUPLICATE_CANDIDATE_FILL")
                    used_candidates.add(p["candidate_ref"])
                    cash -= amount + charge
                    positions[row["hash"]] = {"quantity": qty, "debit": amount, "fee": charge, "epoch": row["epoch"], "candidate_ref": p["candidate_ref"]}
                    expected = {"debit": str(amount), "fee": str(charge), "cash_after": str(cash)}
                else:
                    pos = positions[p["entry_ref"]]
                    due = pos["epoch"] + cfg["horizon_minutes"] * 60
                    if qty != pos["quantity"] or not due <= row["epoch"] <= due + cfg["exit_window_seconds"]:
                        raise Refused("EXIT_CONTRACT_MISMATCH")
                    gross, net = amount - pos["debit"], amount - pos["debit"] - charge - pos["fee"]
                    cash += amount - charge
                    closed.append({"entry_ref": p["entry_ref"], "candidate_ref": pos["candidate_ref"], "gross_pnl": str(gross), "net_pnl": str(net)})
                    del positions[p["entry_ref"]]
                    expected = {"credit": str(amount), "fee": str(charge), "gross_pnl": str(gross), "net_pnl": str(net), "cash_after": str(cash)}
                for key, value in expected.items():
                    if p.get(key) != value:
                        problems.append(f"{row['seq']}:{key}:ACCOUNTING_DISAGREEMENT")
            except (KeyError, Refused, TypeError, ArithmeticError, ValueError) as exc:
                problems.append(f"{row['seq']}:INVALID_EXECUTION:{exc}")
        seen[row["hash"]] = row
    valid = not problems
    return {"status": "VALID" if valid else "MISMATCH", "head": rows[-1]["hash"],
            "cash": str(cash) if valid else None,
            "known_realized_net": str(sum((Decimal(c["net_pnl"]) for c in closed), Decimal("0.00"))) if valid else None,
            "total_net_pnl": str(sum((Decimal(c["net_pnl"]) for c in closed), Decimal("0.00"))) if valid and not positions else None,
            "open_exposure": [{"entry_ref": key, "quantity": p["quantity"], "purchase_cost": str(p["debit"])} for key, p in positions.items()],
            "closed": closed, "problems": problems}
