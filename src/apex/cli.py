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
    planner.add_argument("--codex-model", help="Explicit model through Codex CLI with saved ChatGPT sign-in; requires supported local permissions")
    subs.add_parser("brain-status", help="Check subscription planner availability without requesting model inference")
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
    paper_demo = subs.add_parser("paper-demo", help="Synthetic paper account lifecycle, with observed-later fixture quotes")
    paper_demo.add_argument("--out", type=Path, required=True)
    paper_demo.add_argument("--world", choices=("positive", "adverse", "no-quotes"), default="positive")
    paper_demo.add_argument("--variance", choices=("garch", "ewma"), default="garch")
    paper_replay = subs.add_parser("paper-replay", help="Chronological recorded/synthetic experiment in a new persistent paper account")
    paper_replay.add_argument("--input", type=Path, required=True)
    paper_replay.add_argument("--out", type=Path, required=True)
    paper_replay.add_argument("--session", type=Path, required=True)
    paper_replay.add_argument("--start", type=epoch, required=True)
    paper_replay.add_argument("--end", type=epoch, required=True)
    paper_replay.add_argument("--symbol", default="SPY")
    paper_replay.add_argument("--variance", choices=("garch", "ewma"), default="garch")
    paper_tick = subs.add_parser("paper-service-tick", help="Service the persistent paper account from a measured file feed; no broker")
    paper_tick.add_argument("--root", type=Path, required=True)
    paper_tick.add_argument("--input", type=Path, required=True)
    paper_tick.add_argument("--session", type=Path, required=True)
    paper_tick.add_argument("--settings", type=Path, required=True)
    paper_report = subs.add_parser("paper-report", help="Reconstruct orders, positions and net paper P&L")
    paper_report.add_argument("--root", type=Path, required=True)
    paper_report.add_argument("--format", choices=("json", "text"), default="json")
    paper_report.add_argument("--out", type=Path)
    paper_verify = subs.add_parser("verify-paper", help="Independently reconstruct paper-account arithmetic and retained event references")
    paper_verify.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "paper-demo":
        from .paper_runtime import PaperSession, replay
        doc, start, _ = demo_document(quotes=args.world != "no-quotes")
        doc["source"] = "SYNTHETIC_PAPER_" + args.world.upper().replace("-", "_") + "_EXECUTION_CONTROL_NOT_EDGE"
        if args.world == "adverse":
            doc["control_scope"] = "Adverse future-quote execution sensitivity; same pre-decision information; not market performance."
            for row in doc["observations"]:
                if row["kind"] == "quote" and row["event_epoch"] > start:
                    factor = 1 - .0005 * ((row["event_epoch"] - start) / 60)
                    row["bid"] *= factor
                    row["ask"] *= factor
        calendar = PaperSession(start - 300, start - 300 + 390 * 60, "XNYS",
                                "SYNTHETIC_CALENDAR_FIXTURE", start - 3600)
        result = replay(doc, args.out, start=start, end=start + 900, session=calendar,
                        config=Config(variance=args.variance))
    elif args.command == "paper-replay":
        from .paper_runtime import PaperSession, replay
        from .paper_service import read_json
        result = replay(read_json(args.input), args.out, start=args.start, end=args.end,
                        session=PaperSession(**read_json(args.session, limit=16384)),
                        config=Config(symbol=args.symbol, variance=args.variance))
    elif args.command == "paper-service-tick":
        from .paper_service import tick_files
        result = tick_files(args.root, input_path=args.input, session_path=args.session, settings_path=args.settings)
    elif args.command == "paper-report":
        from .paper_runtime import report
        result = report(args.root)
        if args.out:
            with args.out.open("x") as handle:
                handle.write(json.dumps(result, indent=2) + "\n")
    elif args.command == "verify-paper":
        from .paper_book import verify_paper_book
        result = verify_paper_book(args.root / "paper.sqlite")
    elif args.command == "brain-status":
        from .codex_planner import codex_status
        result = codex_status()
    elif args.command == "director":
        from .director import run_director
        from .research import ResearchPlan
        result = run_director(args.input, args.out,
            plan=ResearchPlan(**json.loads(args.plan.read_text())),
            config=Config(symbol=args.symbol, variance=args.variance),
            market_symbol=args.market_symbol, sector_symbol=args.sector_symbol,
            proposal=_director_proposal(args.proposal) if args.proposal else None,
            model=args.model, codex_model=args.codex_model)
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
    if args.command == "paper-report" and args.format == "text":
        from .paper_service import report_text
        print(report_text(result))
    elif args.command == "shadow-report" and args.format == "text":
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
    if args.command == "paper-service-tick" and (result.get("status", "").startswith("BLOCKED")
            or result.get("status") == "MODEL_FAILED_EXIT_SERVICE_RETAINED"):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
