import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from apex.capture import capture_alpaca, decode, http_transport, replay_capture, timestamp_ns
from apex.core import Config, Refused, canonical
from apex.data import normalize, twin
from apex.decision import evaluate, quote_at
from apex.engine import run
from apex.fixtures import demo_document
from apex.forecast import predict
from apex.ledger import read_verified
from apex.shadow import observe


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def reply(payload, status=200):
    return {"status": status, "body_base64": base64.b64encode(canonical(payload).encode()).decode()}


class CaptureFixture:
    """Transport-only substitution; real decoding, capture, model and shadow reader."""
    def __init__(self):
        doc, self.start, self.end = demo_document()
        self.ns = int(self.start * 1e9)
        bars = [r for r in doc["observations"] if r["kind"] == "bar" and r["available_epoch"] <= self.start]
        self.bars = [{"t": iso(r["event_epoch"]), **{short: r[long] for short, long in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))}} for r in bars]
        q = next(r for r in doc["observations"] if r["kind"] == "quote" and r["event_epoch"] == self.start)
        self.quote = {"t": iso(q["event_epoch"]), "bp": q["bid"], "ap": q["ask"], "bs": 1, "as": 1}
        self.calls = []

    def clock(self):
        return self.ns

    def transport(self, path, params, timeout):
        self.calls.append((path, params))
        self.ns += 10_000_000  # actual provider-shaped delay on the controlled clock
        if path.endswith("quotes/latest"):
            return reply({"quotes": {"SPY": self.quote}})
        if path.endswith("bars/latest"):
            return reply({"bars": {"SPY": self.bars[-1]}})
        return reply({"bars": {"SPY": self.bars}, "next_page_token": None})

    def capture(self, out, **kwargs):
        return capture_alpaca(out, history_start="2026-09-09T13:30:00Z", config=Config(variance="ewma"),
                              feed="sip", round_lot_shares=100, transport=self.transport, clock_ns=self.clock, **kwargs)


def test_capture_real_readers_then_replay_agree(tmp_path):
    f = CaptureFixture()
    result = f.capture(tmp_path / "capture")
    assert result["status"] == "CAPTURED" and result["requests"] == 3 and result["orders"] == 0
    comparison = replay_capture(tmp_path / "capture")
    assert comparison["status"] == "AGREEMENT" and comparison["model_observations"] == 3
    assert comparison["capture_class"] == "SYNTHETIC_ACCEPTANCE"
    doc = json.loads((tmp_path / "capture" / "input.json").read_text())
    assert all(r["available_epoch"] >= f.start for r in doc["observations"])
    assert all(r["availability_basis"] == "SYNTHETIC_CLOCK" for r in doc["observations"])
    q = next(r for r in doc["observations"] if r["kind"] == "quote")
    assert q["ask_size"] == 100 and q["provider_ask_size"] == 1
    shadow = json.loads((tmp_path / "capture" / "shadow-0003.json").read_text())["result"]
    assert shadow["candidate"]["decision"] == "EXPERIMENTAL_LONG"
    assert shadow["candidate"]["authority"] == "NONE_SHADOW_ONLY"
    assert not (tmp_path / "capture" / "ledger.jsonl").exists()


def test_capture_candidate_reaches_existing_replay_engine(tmp_path):
    f = CaptureFixture()
    f.capture(tmp_path / "capture")
    source = tmp_path / "capture" / "input.json"
    cutoff = f.ns / 1e9
    run(source, tmp_path / "replay", start=cutoff, end=cutoff + 900, config=Config(variance="ewma"))
    rows = read_verified(tmp_path / "replay" / "ledger.jsonl")
    candidate = next(r["payload"] for r in rows if r["kind"] == "CANDIDATE")
    shadow = json.loads((tmp_path / "capture" / "shadow-0003.json").read_text())["result"]["candidate"]
    for field in ("decision", "quantity", "expected_net", "model_probability_net_positive"):
        assert candidate[field] == shadow[field]
    assert sum(r["kind"] == "ENTRY" for r in rows) == 1
    assert sum(r["kind"] == "EXIT" for r in rows) == 0  # no fabricated future quote


def test_history_never_backdates_retrieval_and_fresh_receipt_does_not_freshen_quote():
    f = CaptureFixture()
    before = int((f.start - 10) * 1e9)
    received = int(f.start * 1e9)
    body = canonical({"bars": {"SPY": f.bars}}).encode()
    bars, _, _ = decode(body, kind="history", symbol="SPY", feed="sip", received_ns=received, round_lot_shares=100)
    assert observe(bars, before / 1e9, Config(variance="ewma"))["health"]["visible_bars"] == 0
    f.quote["t"] = iso(f.start - 40)
    quote, _, _ = decode(canonical({"quotes": {"SPY": f.quote}}).encode(), kind="quote", symbol="SPY", feed="sip", received_ns=received, round_lot_shares=100)
    observed = observe(bars + quote, f.start, Config(variance="ewma"))
    assert observed["candidate"]["decision"] == "WAIT" and observed["candidate"]["reason"] == "QUOTE_STALE"


def test_nanosecond_future_and_partial_bar_are_named():
    f = CaptureFixture()
    f.quote["t"] = "2026-09-11T13:35:00.000000001Z"
    rows, rejected, _ = decode(canonical({"quotes": {"SPY": f.quote}}).encode(), kind="quote", symbol="SPY", feed="sip", received_ns=f.ns, round_lot_shares=100)
    assert not rows and rejected[0]["reason"] == "PROVIDER_EVENT_AFTER_RECEIPT"
    bar = {**f.bars[-1], "t": iso(f.start)}
    rows, rejected, _ = decode(canonical({"bars": {"SPY": bar}}).encode(), kind="bar", symbol="SPY", feed="sip", received_ns=f.ns, round_lot_shares=100)
    assert not rows and rejected[0]["reason"] == "PROVIDER_BAR_NOT_COMPLETE"


@pytest.mark.parametrize("mode", ["unauthorized", "malformed", "pagination", "cycle"])
def test_bounded_failures_and_pagination(tmp_path, mode):
    f = CaptureFixture()
    transport = f.transport
    def changed(path, params, timeout):
        if path != "/v2/stocks/bars":
            return transport(path, params, timeout)
        f.ns += 10_000_000
        if mode == "unauthorized":
            return {"status": 403, "error": "HTTP_ERROR"}
        if mode == "malformed":
            return reply({"not_bars": []})
        return reply({"bars": {"SPY": f.bars}, "next_page_token": "repeat"})
    f.transport = changed
    if mode == "cycle":
        with pytest.raises(Refused, match="PAGINATION_TOKEN_CYCLE"):
            f.capture(tmp_path / "capture", max_pages=3)
        assert (tmp_path / "capture" / "FAILED.json").exists()
        return
    result = f.capture(tmp_path / "capture", max_pages=1)
    assert result["history_pagination_exhausted"] is False
    assert result["requests"] == 3
    assert replay_capture(tmp_path / "capture")["checks"] == 3
    if mode == "pagination":
        assert result["request_errors"] == []
    else:
        assert result["request_errors"]


def test_raw_capture_tampering_and_collision_refuse(tmp_path):
    f = CaptureFixture()
    root = tmp_path / "capture"
    f.capture(root)
    before = (root / "COMPLETE").read_bytes()
    with pytest.raises(FileExistsError):
        f.capture(root)
    assert (root / "COMPLETE").read_bytes() == before
    path = root / "raw" / "0003.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(Refused, match="CAPTURE_BODY_CHANGED"):
        replay_capture(root)


def test_capture_clock_rewind_preserves_failure(tmp_path):
    f = CaptureFixture()
    def reversed_clock(path, params, timeout):
        f.ns -= 1
        return reply({"bars": {"SPY": f.bars}})
    f.transport = reversed_clock
    with pytest.raises(Refused, match="CAPTURE_CLOCK_REWIND"):
        f.capture(tmp_path / "capture")
    assert (tmp_path / "capture" / "FAILED.json").exists()
    assert not (tmp_path / "capture" / "COMPLETE").exists()


def test_worker_refuses_order_endpoint_before_authentication():
    env = {**os.environ, "PYTHONPATH": "src", "APCA_API_KEY_ID": "SENTINEL_KEY_DO_NOT_LOG", "APCA_API_SECRET_KEY": "SENTINEL_SECRET_DO_NOT_LOG"}
    result = subprocess.run([sys.executable, "-m", "apex.http_worker"],
                            input=canonical({"path": "/v2/orders", "params": {}, "timeout": 1}),
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0 and "READ_ONLY_ENDPOINT_NOT_ALLOWED" in result.stderr
    assert "SENTINEL" not in result.stdout + result.stderr


def test_real_transport_worker_missing_credentials_and_no_secret_output(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    response = http_transport("/v2/stocks/bars/latest", {"symbols": "SPY", "feed": "sip"}, 3)
    assert response == {"status": 0, "error": "BLOCKED_EXTERNAL_CREDENTIAL"}


def test_actual_subprocess_deadline_kills_hung_worker(monkeypatch, tmp_path):
    # Substitute only the child launch command. Exercise the real subprocess
    # timeout/kill/reap implementation, not an injected TimeoutExpired exception.
    real_run = subprocess.run
    marker = tmp_path / "child-finished"
    entered = tmp_path / "child-entered"
    def launch(_args, **kwargs):
        code = "import time,pathlib,os; pathlib.Path(" + repr(str(entered)) + ").write_text(str(os.getpid())); time.sleep(2); pathlib.Path(" + repr(str(marker)) + ").write_text('finished')"
        return real_run([sys.executable, "-c", code], **kwargs)
    monkeypatch.setattr(subprocess, "run", launch)
    start = time.monotonic()
    response = http_transport("/v2/stocks/bars/latest", {}, .3)
    assert response["error"] == "WALL_CLOCK_DEADLINE_EXCEEDED"
    assert time.monotonic() - start < 1.5 and entered.exists() and not marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(entered.read_text()), 0)


def test_cli_missing_credentials_leaves_reviewable_capture(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")}
    env["PYTHONPATH"] = "src"
    history = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat()
    command = [sys.executable, "-m", "apex.cli", "capture-alpaca", "--out", str(tmp_path / "capture"),
               "--history-start", history, "--feed", "sip", "--round-lot-shares", "100"]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 3
    assert json.loads(result.stdout)["status"] == "BLOCKED_NO_MARKET_DATA"
    assert replay_capture(tmp_path / "capture")["status"] == "REFUSAL_PARITY_ONLY"


# ---------------------------------------------------------------- transport failure classification


def test_transport_failures_are_classified_from_a_fixed_vocabulary_without_exception_text():
    """A misconfigured local trust store used to report the same TRANSPORT_ERROR as an outage, which cost real
    diagnosis time. The class is reported; the exception text is not, because it carries hosts, paths and
    server strings. TLS verification is never weakened to make a request succeed."""
    import socket
    import ssl
    from urllib.error import URLError
    from apex.http_worker import TRANSPORT_ERRORS, transport_error_class
    secret = "SENTINEL-host.example.internal-/private/path"
    cases = [(URLError(ssl.SSLCertVerificationError(secret)), "TLS_CERTIFICATE_ERROR"),
             (URLError(socket.gaierror(8, secret)), "DNS_RESOLUTION_ERROR"),
             (URLError(ConnectionRefusedError(61, secret)), "CONNECTION_REFUSED"),
             (TimeoutError(secret), "TRANSPORT_TIMEOUT"),
             (URLError(socket.timeout(secret)), "TRANSPORT_TIMEOUT"),
             (OSError(secret), "TRANSPORT_ERROR"),
             (URLError(secret), "TRANSPORT_ERROR")]
    for exc, expected in cases:
        name = transport_error_class(exc)
        assert name == expected, (exc, name)
        assert name in TRANSPORT_ERRORS
        assert "SENTINEL" not in name and secret not in name


def test_the_worker_never_disables_certificate_verification():
    import pathlib
    source = pathlib.Path("src/apex/http_worker.py").read_text()
    for forbidden in ("_create_unverified_context", "CERT_NONE", "check_hostname = False", "verify=False"):
        assert forbidden not in source


def test_a_classification_loop_cannot_hang_on_a_self_referential_cause():
    from urllib.error import URLError
    from apex.http_worker import transport_error_class
    exc = URLError("outer")
    exc.reason = exc
    assert transport_error_class(exc) == "TRANSPORT_ERROR"
