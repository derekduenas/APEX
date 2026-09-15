"""Offline commands only; import does not contact providers or a broker."""
import argparse
import json
from datetime import datetime
from pathlib import Path

from .core import Config, canonical
from .data import from_massive_csv
from .engine import run
from .fixtures import demo_document
from .verification import verify_run


def epoch(text):
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError("Timestamp requires an explicit timezone")
    return dt.timestamp()


def main():
    parser = argparse.ArgumentParser(description="APEX offline research; no broker execution")
    subs = parser.add_subparsers(dest="command", required=True)
    demo = subs.add_parser("demo")
    demo.add_argument("--out", type=Path, required=True)
    demo.add_argument("--variance", choices=("garch", "ewma"), default="garch")
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
    args = parser.parse_args()
    if args.command == "verify":
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
    print(json.dumps(result, indent=2))
    if result.get("status") == "MISMATCH" or result.get("accounting", {}).get("status") == "MISMATCH":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
