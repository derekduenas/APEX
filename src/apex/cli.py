"""Offline research and explicit read-only capture. Import never makes requests."""
import argparse
import json
from datetime import datetime
from pathlib import Path

from .core import Config, canonical
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
    quotes_parser = subs.add_parser("fetch-decision-quotes",
                                    help="One read-only request per decision instant of a frozen plan; research input only")
    quotes_parser.add_argument("--plan", type=Path, required=True)
    quotes_parser.add_argument("--out", type=Path, required=True)
    quotes_parser.add_argument("--symbol", default="SPY")
    quotes_parser.add_argument("--feed", choices=("sip", "iex"), required=True)
    quotes_parser.add_argument("--round-lot-shares", type=int, required=True)
    quotes_parser.add_argument("--timeout", type=float, default=10)
    merge_parser = subs.add_parser("merge-inputs", help="Combine research input documents, keeping each one's provenance")
    merge_parser.add_argument("--input", type=Path, required=True, action="append", dest="inputs")
    merge_parser.add_argument("--out", type=Path, required=True)
    merge_parser.add_argument("--retrieved-utc", required=True)
    research_verify = subs.add_parser("verify-research", help="Reconstruct research inputs, causal labels, model comparisons and scores")
    research_verify.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "fetch-decision-quotes":
        from .decision_quotes import fetch_decision_quotes
        from .research import ResearchPlan
        result = fetch_decision_quotes(args.out, plan=ResearchPlan(**json.loads(args.plan.read_text())),
                                       config=Config(symbol=args.symbol), feed=args.feed,
                                       round_lot_shares=args.round_lot_shares, timeout=args.timeout)
    elif args.command == "merge-inputs":
        epoch(args.retrieved_utc)
        from .data import merged_document
        document = merged_document([json.loads(p.read_text()) for p in args.inputs], retrieved_utc=args.retrieved_utc)
        with args.out.open("x") as handle:
            handle.write(canonical(document))
        result = {"components": len(document["components"]), "observations": len(document["observations"]),
                  "output": str(args.out)}
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
