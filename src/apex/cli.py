"""Offline research and explicit read-only capture. Import never makes requests."""
import argparse
import json
from datetime import datetime
from pathlib import Path

from .core import Config, Refused, canonical
from .capture import capture_alpaca, replay_capture
from .data import from_massive_csv
from .engine import run
from .fixtures import capture_demo, demo_document
from .verification import verify_run


def epoch(text):
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError("Timestamp requires an explicit timezone")
    return dt.timestamp()


def _director_proposal(path):
    from .ai_planner import MAX_PROPOSAL_BYTES, strict_json
    if not path.is_file() or path.is_symlink():
        raise Refused("DIRECTOR_PROPOSAL_FILE_INVALID")
    with path.open("rb") as handle:
        raw = handle.read(MAX_PROPOSAL_BYTES + 1)
    if len(raw) > MAX_PROPOSAL_BYTES:
        raise Refused("DIRECTOR_PROPOSAL_BYTES_EXCEEDED")
    return strict_json(raw)


def main():
    parser = argparse.ArgumentParser(description="APEX research replay and read-only capture; no broker execution")
    subs = parser.add_subparsers(dest="command", required=True)
    demo = subs.add_parser("demo")
    demo.add_argument("--out", type=Path, required=True)
    demo.add_argument("--variance", choices=("garch", "ewma"), default="garch")
    capture_control = subs.add_parser("demo-capture", help="Synthetic acceptance through the actual capture and shadow path")
    capture_control.add_argument("--out", type=Path, required=True)
    capture_control.add_argument("--variance", choices=("garch", "ewma"), default="garch")
    replay = subs.add_parser("replay")
    replay.add_argument("--input", type=Path, required=True)
    replay.add_argument("--out", type=Path, required=True)
    replay.add_argument("--start", type=epoch, required=True)
    replay.add_argument("--end", type=epoch, required=True)
    replay.add_argument("--symbol", default="SPY")
    replay.add_argument("--variance", choices=("garch", "ewma"), default="garch")
    verify = subs.add_parser("verify")
    verify.add_argument("--run", type=Path, required=True)
    convert = subs.add_parser("import-massive")
    convert.add_argument("--csv", type=Path, required=True)
    convert.add_argument("--out", type=Path, required=True)
    convert.add_argument("--retrieved-utc", required=True)
    convert.add_argument("--symbol", default="SPY")
    capture = subs.add_parser("capture-alpaca", help="Bounded read-only REST capture; requires standalone Alpaca credentials")
    capture.add_argument("--out", type=Path, required=True)
    capture.add_argument("--history-start", required=True)
    capture.add_argument("--symbol", default="SPY")
    capture.add_argument("--feed", choices=("sip", "iex"), required=True)
    capture.add_argument("--round-lot-shares", type=int, required=True)
    capture.add_argument("--max-pages", type=int, default=3)
    capture.add_argument("--timeout", type=float, default=5)
    capture.add_argument("--variance", choices=("garch", "ewma"), default="garch")
    compare = subs.add_parser("verify-capture", help="Re-decode responses and compare shadow-prefix vs replay-as-of behavior")
    compare.add_argument("--capture", type=Path, required=True)
    tick_parser = subs.add_parser("shadow-tick", help="One persistent Linux shadow cycle; systemd schedules repetitions")
    tick_parser.add_argument("--root", type=Path, required=True)
    tick_parser.add_argument("--settings", type=Path, required=True)
    report_parser = subs.add_parser("shadow-report", help="Read-only daily shadow status, models and candidate reasons")
    report_parser.add_argument("--root", type=Path, required=True)
    report_parser.add_argument("--day")
    report_parser.add_argument("--format", choices=("json", "text"), default="text")
    research_parser = subs.add_parser("research", help="Frozen chronological model tournament; offline research only")
    research_parser.add_argument("--input", type=Path, required=True)
    research_parser.add_argument("--plan", type=Path, required=True, help="JSON with explicit epoch phase boundaries and fixed research policy")
    research_parser.add_argument("--out", type=Path, required=True)
    research_parser.add_argument("--symbol", default="SPY")
    research_parser.add_argument("--variance", choices=("garch", "ewma"), default="garch")
    research_demo = subs.add_parser("research-demo", help="Twelve synthetic sessions through the real research path")
    research_demo.add_argument("--out", type=Path, required=True)
    research_demo.add_argument("--world", choices=("persistent", "null", "reversal"), default="persistent")
    research_demo.add_argument("--variance", choices=("garch", "ewma"), default="ewma")
    research_verify = subs.add_parser("verify-research", help="Reconstruct research inputs, causal labels, model comparisons and scores")
    research_verify.add_argument("--run", type=Path, required=True)
    strategy = subs.add_parser("strategy-lab", help="Regime-aware strategy tournament, stresses and causal outcome feedback")
    strategy.add_argument("--input", type=Path, required=True)
    strategy.add_argument("--plan", type=Path, required=True)
    strategy.add_argument("--out", type=Path, required=True)
    strategy.add_argument("--symbol", default="SPY")
    strategy.add_argument("--variance", choices=("garch", "ewma"), default="garch")
    strategy_demo = subs.add_parser("strategy-demo", help="Synthetic premarket-to-strategy experiment, never market evidence")
    strategy_demo.add_argument("--out", type=Path, required=True)
    strategy_demo.add_argument("--world", choices=("persistent", "null", "reversal", "positive"), default="persistent")
    strategy_demo.add_argument("--variance", choices=("garch", "ewma"), default="ewma")
    strategy_verify = subs.add_parser("verify-strategy-lab", help="Reconstruct intelligence, scenarios, strategy comparisons and feedback")
    strategy_verify.add_argument("--run", type=Path, required=True)
    director = subs.add_parser("director", help="AI-directed bounded research cycle; no broker or capital authority")
    director.add_argument("--input", type=Path, required=True)
    director.add_argument("--plan", type=Path, required=True)
    director.add_argument("--out", type=Path, required=True)
    director.add_argument("--symbol", required=True)
    director.add_argument("--market-symbol", required=True)
    director.add_argument("--sector-symbol", required=True)
    director.add_argument("--variance", choices=("garch", "ewma"), default="garch")
    planner = director.add_mutually_exclusive_group(required=True)
    planner.add_argument("--proposal", type=Path, help="Retained external human/AI proposal; authorship not authenticated")
    planner.add_argument("--model", help="Explicit OpenAI model for runtime planning; OPENAI_API_KEY required")
    director_demo = subs.add_parser("director-demo", help="Synthetic control with a fixed scripted proposal; no LLM invocation")
    director_demo.add_argument("--out", type=Path, required=True)
    director_demo.add_argument("--world", choices=("catchup", "null", "continuation"), default="catchup")
    director_demo.add_argument("--variance", choices=("garch", "ewma"), default="ewma")
    director_verify = subs.add_parser("verify-director", help="Reconstruct director authority and both research children")
    director_verify.add_argument("--run", type=Path, required=True)
    peer = subs.add_parser("peer-experiment", help="Compare a peer-information challenger on a verified strategy lab")
    peer.add_argument("--source-run", type=Path, required=True)
    peer.add_argument("--out", type=Path, required=True)
    peer.add_argument("--market-symbol", required=True)
    peer.add_argument("--sector-symbol", required=True)
    peer_verify = subs.add_parser("verify-peer-experiment")
    peer_verify.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "director":
        from .director import run_director
        from .research import ResearchPlan
        result = run_director(args.input, args.out,
            plan=ResearchPlan(**json.loads(args.plan.read_text())),
            config=Config(symbol=args.symbol, variance=args.variance),
            market_symbol=args.market_symbol, sector_symbol=args.sector_symbol,
            proposal=_director_proposal(args.proposal) if args.proposal else None,
            model=args.model)
    elif args.command == "director-demo":
        import tempfile
        from .director import run_director
        from .peer_fixtures import peer_demo_document
        document, plan = peer_demo_document(world=args.world)
        proposal = {"schema": "APEX_RESEARCH_PROPOSAL_V1", "action": "RUN_PEER_DISLOCATION_STUDY",
            "target_symbol": "AAPL", "market_symbol": "SPY", "sector_symbol": "XLK",
            "rationale": "FIXED_SYNTHETIC_CONTROL: exercise connected readers; no runtime LLM or market-edge claim."}
        with tempfile.TemporaryDirectory(prefix="apex-peer-demo-") as tmp:
            input_path = Path(tmp) / "input.json"
            input_path.write_text(canonical(document))
            result = run_director(input_path, args.out, plan=plan,
                config=Config(symbol="AAPL", variance=args.variance),
                market_symbol="SPY", sector_symbol="XLK", proposal=proposal)
    elif args.command == "verify-director":
        from .director import verify_director
        result = verify_director(args.run)
    elif args.command == "peer-experiment":
        from .peer_experiment import run_peer_experiment
        result = run_peer_experiment(args.source_run, args.out,
            market_symbol=args.market_symbol, sector_symbol=args.sector_symbol)
    elif args.command == "verify-peer-experiment":
        from .peer_experiment import verify_peer_experiment
        result = verify_peer_experiment(args.run)
    elif args.command == "strategy-lab":
        from .strategy_lab import run_lab
        from .research import ResearchPlan
        result = run_lab(args.input, args.out, plan=ResearchPlan(**json.loads(args.plan.read_text())),
                         config=Config(symbol=args.symbol, variance=args.variance))
    elif args.command == "strategy-demo":
        from .strategy_lab import run_lab
        from .fixtures import strategy_demo_document
        doc, plan = strategy_demo_document(world=args.world)
        result = run_lab(canonical(doc).encode(), args.out, plan=plan, config=Config(variance=args.variance))
    elif args.command == "verify-strategy-lab":
        from .strategy_lab_verification import verify_lab
        result = verify_lab(args.run)
    elif args.command == "research":
        from .research import ResearchPlan, run_research
        result = run_research(args.input, args.out, plan=ResearchPlan(**json.loads(args.plan.read_text())),
                              config=Config(symbol=args.symbol, variance=args.variance))
    elif args.command == "research-demo":
        from .research import run_research
        from .fixtures import research_demo_document
        doc, plan = research_demo_document(world=args.world)
        result = run_research(canonical(doc).encode(), args.out, plan=plan, config=Config(variance=args.variance))
    elif args.command == "verify-research":
        from .research_verification import verify_research
        result = verify_research(args.run)
    elif args.command == "shadow-tick":
        from .runtime import load_settings, tick
        result = tick(args.root, load_settings(args.settings))
    elif args.command == "shadow-report":
        from .runtime import report
        result = report(args.root, day=args.day)
    elif args.command == "demo-capture":
        result = capture_demo(args.out, variance=args.variance)
    elif args.command == "capture-alpaca":
        result = capture_alpaca(args.out, history_start=args.history_start, config=Config(symbol=args.symbol, variance=args.variance),
                                feed=args.feed, round_lot_shares=args.round_lot_shares, max_pages=args.max_pages, timeout=args.timeout)
    elif args.command == "verify-capture":
        result = replay_capture(args.capture)
    elif args.command == "verify":
        result = verify_run(args.run)
    elif args.command == "demo":
        doc, start, end = demo_document()
        result = run(canonical(doc).encode(), args.out, start=start, end=end, config=Config(variance=args.variance))
    elif args.command == "replay":
        result = run(args.input, args.out, start=args.start, end=args.end, config=Config(symbol=args.symbol, variance=args.variance))
    else:
        epoch(args.retrieved_utc)
        result = from_massive_csv(args.csv.read_text(), args.symbol, args.retrieved_utc)
        with args.out.open("x") as handle:
            handle.write(canonical(result))
        result = {"observations": len(result["observations"]), "availability": "BAR_COMPLETION_ASSUMPTION_V1"}
    if args.command == "shadow-report" and args.format == "text":
        from .runtime import report_text
        print(report_text(result))
    else:
        print(json.dumps(result, indent=2))
    if result.get("status") == "MISMATCH" or result.get("accounting", {}).get("status") == "MISMATCH":
        raise SystemExit(2)
    if result.get("status") == "BLOCKED_NO_MARKET_DATA":
        raise SystemExit(3)
    if args.command == "shadow-tick" and result.get("status") not in ("SHADOW_OBSERVED", "IDLE_OUTSIDE_REGULAR_CLOCK_WINDOW"):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
