"""File-feed service boundary; durable health output even when inputs are absent.

A separately commissioned collector atomically publishes measured APEX_DATA_V1
and a versioned exchange-session contract. This service opens no broker/data
network connection and never substitutes synthetic input for missing live data.
"""
from pathlib import Path
import re
import time

from .ai_planner import strict_json
from .core import Config, Refused, digest
from .runtime import _replace


def read_json(path, *, limit=128 * 1024 * 1024):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise Refused("PAPER_SERVICE_FILE_UNAVAILABLE")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise Refused("PAPER_SERVICE_FILE_TOO_LARGE")
    value = strict_json(raw)
    digest(value)
    return value


def load_service_settings(path):
    from .paper_book import PaperAccountConfig
    value = read_json(path, limit=16384)
    if not isinstance(value, dict) or set(value) != {"schema", "model", "account"}:
        raise Refused("PAPER_SERVICE_CONFIG_FIELDS_INVALID")
    if value["schema"] != "APEX_PAPER_SERVICE_V1":
        raise Refused("PAPER_SERVICE_CONFIG_SCHEMA_INVALID")
    model, account = Config(**value["model"]), PaperAccountConfig(**value["account"])
    if account.feed != "sip":
        raise Refused("PAPER_SERVICE_REQUIRES_DECLARED_SIP_FEED")
    if any(str(getattr(model, key)) != str(getattr(account, key)) for key in (
            "starting_cash", "max_notional", "commission_per_share", "minimum_commission")):
        raise Refused("PAPER_SERVICE_MODEL_ACCOUNT_COSTS_DISAGREE")
    return model, account


def tick_files(root, *, input_path=None, session_path=None, settings_path, generation_root=None):
    """One service invocation; host scheduler owns recurrence and process timeout.

    With `generation_root` the published pair is resolved ONCE and both halves are read from that pinned
    immutable directory, so a publication landing mid-read cannot hand back one half of each generation.
    """
    from .paper_runtime import PaperSession, tick
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    feed = {}
    try:
        config, account = load_service_settings(settings_path)
        if generation_root is not None:
            from .paper_feed import feed_state
            feed = feed_state(generation_root)
            if feed.get("session") is None:
                raise Refused("FEED_SESSION_UNAVAILABLE:" + str(feed.get("problem")))
            calendar = PaperSession(**feed["session"]["session"])
            document = feed["input"] if feed.get("input") is not None and feed.get("problem") is None else {
                "schema": "APEX_DATA_V1", "source": "UNAVAILABLE_LIVE_FILE_FEED",
                "status": "BLOCKED_NO_MARKET_DATA", "observations": []}
            result = tick(root, document, session=calendar, config=config,
                          account_config=account, mode="LIVE_PAPER")
            result["feed"] = {k: v for k, v in feed.items() if k not in ("input", "session")}
            _replace(root / "service-health.json", result)
            return result
        calendar = PaperSession(**read_json(session_path, limit=16384))
        try:
            document = read_json(input_path)
        except (Refused, OSError, ValueError, TypeError):
            # An empty denied source still lets the controller expose existing
            # obligations and stale marks. It supplies no usable fill quote.
            document = {"schema": "APEX_DATA_V1", "source": "UNAVAILABLE_LIVE_FILE_FEED",
                        "status": "BLOCKED_NO_MARKET_DATA", "observations": []}
        result = tick(root, document, session=calendar, config=config,
                      account_config=account, mode="LIVE_PAPER")
    except (Refused, OSError, ValueError, TypeError, KeyError) as exc:
        # File/provider exception bodies may contain private material.
        reason = str(exc) if isinstance(exc, Refused) else "PAPER_SERVICE_INPUT_INVALID"
        if not re.fullmatch(r"[A-Z0-9_:.\-]{1,160}", reason):
            reason = "PAPER_SERVICE_INPUT_INVALID"
        result = {"schema": "APEX_PAPER_SERVICE_HEALTH_V1", "status": "BLOCKED_SERVICE_INPUT",
                  "error_type": type(exc).__name__, "observed_epoch": time.time(),
                  "reason": reason,
                  "account_status": "NOT_RECOMPUTED_CHECK_RETAINED_PAPER_ACCOUNT",
                  "orders": None, "fills": None, "account_pnl": None,
                  "authority": "SIMULATED_PAPER_ONLY_NO_BROKER_OR_REAL_CAPITAL"}
    _replace(root / "service-health.json", result)
    return result


def report_text(report):
    account = report["account"]
    def amount(value):
        return "unavailable" if value is None else str(value)
    rows = ["APEX paper account — all fills are simulated", "Mode: " + report["mode"],
            "Status: " + report["status"], "As of epoch: " + str(report["as_of_epoch"]),
            "Cash: " + amount(account["cash"]),
            "Realized net P&L: " + amount(account["realized_net"]),
            "Unrealized net P&L: " + amount(account["unrealized_net"]),
            "Equity: " + amount(account["equity"]),
            "Orders: " + str(account["orders"]) + " | Fills: " + str(account["fills"]),
            "Open positions: " + str(len(account["positions"])) + " | Pending orders: " + str(len(account["pending_orders"])),
            "Accounting verification: " + report["verification"]["status"]]
    for position in account["positions"]:
        rows.append(position["symbol"] + " | shares " + str(position["quantity"]) +
                    " | net unrealized " + amount(position["unrealized_net"]) + " | " + position["mark_status"])
    return "\n".join(rows)

