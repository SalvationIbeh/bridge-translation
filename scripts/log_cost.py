"""
Cost ledger for the Stage 1/2 spike (CLAUDE.md §8a — $300 cap across all providers).

Usage:
    python scripts/log_cost.py add --provider runpod --usd 4.16 --note "RTX 4000 Ada, 16h S1 runs"
    python scripts/log_cost.py add --provider openai --usd 3.00 --date 2026-05-30 --note "Stage 1 MT+TTS"
    python scripts/log_cost.py report

`add` appends one entry to runs/cost-ledger.json (created on first use).
`report` prints every entry, the total, and remaining budget; exits non-zero at the
80%-of-cap review threshold so it can gate scripts/CI. Entries whose cost is not yet
known can be added with --usd unknown; report flags them until they're priced.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Force UTF-8 on Windows consoles that default to cp1252 (matches gate2_accuracy_check.py)
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LEDGER_PATH = Path(__file__).resolve().parent.parent / "runs" / "cost-ledger.json"
BUDGET_USD = 300.0
REVIEW_THRESHOLD = 0.80  # CLAUDE.md §8a: hit 80% of cap → stop and review


def _load() -> dict[str, Any]:
    if LEDGER_PATH.exists():
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return {"budget_usd": BUDGET_USD, "entries": []}


def _save(ledger: dict[str, Any]) -> None:
    LEDGER_PATH.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")


def cmd_add(args: argparse.Namespace) -> int:
    ledger = _load()
    usd: float | None
    if args.usd.lower() in ("unknown", "tbd", "?"):
        usd = None
    else:
        usd = round(float(args.usd), 2)
    ledger["entries"].append(
        {
            "date": args.date,
            "provider": args.provider,
            "usd": usd,
            "estimate": args.estimate,
            "note": args.note,
        }
    )
    _save(ledger)
    print(f"Logged: {args.date} {args.provider} "
          f"{'$%.2f' % usd if usd is not None else 'UNPRICED'} — {args.note}")
    return cmd_report(args)


def cmd_report(_args: argparse.Namespace) -> int:
    ledger = _load()
    budget = float(ledger.get("budget_usd", BUDGET_USD))
    entries = ledger["entries"]
    if not entries:
        print(f"Ledger empty. Budget: ${budget:.2f}")
        return 0

    total = 0.0
    unpriced = []
    print(f"{'date':<12} {'provider':<12} {'usd':>8}  note")
    for e in entries:
        usd = e.get("usd")
        if usd is None:
            unpriced.append(e)
            shown = "???"
        else:
            total += usd
            shown = f"{usd:.2f}"
        est = "~" if e.get("estimate") else ""
        print(f"{e['date']:<12} {e['provider']:<12} {est}{shown:>7}  {e['note']}")

    pct = total / budget
    print(f"\nTotal: ${total:.2f} of ${budget:.2f} cap ({pct:.0%}) — ${budget - total:.2f} remaining")
    if unpriced:
        print(f"⚠ {len(unpriced)} unpriced entr{'y' if len(unpriced) == 1 else 'ies'} "
              f"— fill in from the provider billing dashboard (total above is an undercount).")
    if pct >= REVIEW_THRESHOLD:
        print(f"🛑 ≥{REVIEW_THRESHOLD:.0%} of cap reached — stop and review before spending more (CLAUDE.md §8a).")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="append a spend entry")
    p_add.add_argument("--provider", required=True,
                       help="runpod | openai | deepgram | elevenlabs | modal | ...")
    p_add.add_argument("--usd", required=True,
                       help="amount in USD, or 'unknown' to log now and price later")
    p_add.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD (default: today)")
    p_add.add_argument("--note", required=True, help="what the spend was for")
    p_add.add_argument("--estimate", action="store_true", help="mark the amount as approximate")
    p_add.set_defaults(func=cmd_add)

    p_rep = sub.add_parser("report", help="print all entries + total vs cap")
    p_rep.set_defaults(func=cmd_report)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
