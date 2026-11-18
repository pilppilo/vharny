"""Price recorded runs at a rate card, to size cost limits from real usage (COD-86).

Reads each run's ``report.json`` (model decisions and failures, every attempt
with its billed tokens) and ``session.sqlite3`` (supervisor consults), and
prices every provider call at the given rate card. Prompt tokens are priced as
uncached, because no recorded run kept the provider's cache split, so the
figures are an upper bound on input cost.

Usage::

    uv run python tools/price_recorded_runs.py RATE_CARD.json RUN_DIR [RUN_DIR ...]
    uv run python tools/price_recorded_runs.py card.json \\
        evidence/phase-0004-longcat-trial/{ls20,ft09,rerun-ls20} --json

Prints, per run: calls, actions, billed tokens, cost, the share spent on calls
that ran out or failed, cost per game action, and each decision's cost.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from vharness.agent.pricing import RateCard, load_rate_card


def _calls(report: dict) -> list[dict]:
    calls = []
    for kind in ("decisions", "model_failures"):
        for decision in report.get(kind, []):
            for attempt in decision["attempts"]:
                calls.append(
                    {"kind": kind, "sequence": decision["sequence"], **attempt}
                )
    return calls


def _supervisor_usage(session: Path) -> list[tuple[int | None, int | None]]:
    if not session.exists():
        return []
    con = sqlite3.connect(f"file:{session}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select payload from events where kind = 'guidance_received'"
        ).fetchall()
    finally:
        con.close()
    usage = []
    for (payload,) in rows:
        measured = json.loads(payload).get("usage") or {}
        usage.append((measured.get("input_tokens"), measured.get("output_tokens")))
    return usage


def _price(card: RateCard, prompt: int | None, output: int | None) -> Decimal:
    # An unreported call is priced at zero here; such calls are counted
    # separately so the reader can see how much usage is missing.
    return card.cost(prompt or 0, output or 0) or Decimal(0)


def price_run(card: RateCard, run: Path) -> dict:
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    calls = _calls(report)
    cost = sum(
        (_price(card, c["prompt_tokens"], c["completion_tokens"]) for c in calls),
        Decimal(0),
    )
    wasted = sum(
        (
            _price(card, c["prompt_tokens"], c["completion_tokens"])
            for c in calls
            if c["finish_reason"] == "length" or c["error"]
        ),
        Decimal(0),
    )
    supervisor = _supervisor_usage(run / "session.sqlite3")
    supervisor_cost = sum((_price(card, p, o) for p, o in supervisor), Decimal(0))
    decisions: dict[tuple[str, int], Decimal] = {}
    for c in calls:
        key = (c["kind"], c["sequence"])
        decisions[key] = decisions.get(key, Decimal(0)) + _price(
            card, c["prompt_tokens"], c["completion_tokens"]
        )
    actions = len(report.get("actions", []))
    total = cost + supervisor_cost
    return {
        "run": str(run),
        "model_calls": len(calls),
        "unreported_calls": sum(
            1
            for c in calls
            if c["prompt_tokens"] is None or c["completion_tokens"] is None
        ),
        "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in calls),
        "output_tokens": sum(c["completion_tokens"] or 0 for c in calls),
        "largest_prompt": max((c["prompt_tokens"] or 0 for c in calls), default=0),
        "game_actions": actions,
        "decisions": len(decisions),
        "supervisor_consults": len(supervisor),
        "model_cost": str(cost),
        "ran_out_or_failed_cost": str(wasted),
        "supervisor_cost": str(supervisor_cost),
        "total_cost": str(total),
        "cost_per_action": str(total / actions) if actions else None,
        "decision_costs": {
            f"{'failure' if kind == 'model_failures' else 'decision'}-{seq}": str(value)
            for (kind, seq), value in sorted(decisions.items(), key=lambda i: i[0][1])
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("rate_card", type=Path)
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    card = load_rate_card(args.rate_card)
    results = [price_run(card, run) for run in args.runs]
    if args.json:
        print(json.dumps({"rate_card": card.to_payload(), "runs": results}, indent=2))
        return 0
    for r in results:
        print(
            f"{r['run']}: {r['model_calls']} calls ({r['unreported_calls']} unreported), "
            f"{r['game_actions']} actions, {r['decisions']} decisions, "
            f"${Decimal(r['total_cost']):.4f} total, "
            f"${Decimal(r['ran_out_or_failed_cost']):.4f} on calls that ran out or failed, "
            f"per action {'—' if r['cost_per_action'] is None else '$%.4f' % Decimal(r['cost_per_action'])}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
