# Sizing the per-game cost limit from the LongCat trial

This note prices every recorded provider call in this trial at LongCat's
published rates, and derives the `--max-cost` rule proposed for the ARC-AGI-3
campaign (campaign manifest draft, decision 5). It records the data, the
rule, and why, so the rule can be re-derived as more runs are priced.

Recorded 2026-11-18 for COD-86. Not a scored result.

## Rates

`campaigns/rate-cards/longcat-2.5-2026-11-18.json`, from LongCat's
[pay-as-you-go page](https://longcat.ai/platform/docs/api-pay-as-you-go),
checked 2026-11-18. Dollars per million tokens:

| Uncached input | Cached input | Output (reasoning included) |
| --- | --- | --- |
| $0.30 | $0.006 | $1.20 |

The page heads the table "Discounted Price (limited-time)", with no end date
and no regular price. Failed requests (401, 403, 429, 500 and other errors)
are not billed.

The trial ran at `https://api.longcat.chat/openai/v1`, the host in LongCat's
API reference.

## Method

```
uv run python tools/price_recorded_runs.py \
    campaigns/rate-cards/longcat-2.5-2026-11-18.json \
    evidence/phase-0004-longcat-trial/ls20 \
    evidence/phase-0004-longcat-trial/ft09 \
    evidence/phase-0004-longcat-trial/rerun-ls20
```

The tool reads each `report.json` (every decision and model failure, each
attempt with billed tokens) and the supervisor consults in `session.sqlite3`.

Two limits on accuracy:

- **No cache split was recorded.** The harness did not read
  `prompt_tokens_details.cached_tokens` until COD-86, so every prompt token is
  priced as uncached. Input is only 9% to 33% of each run's cost, so cached
  pricing could lower these figures by at most about a third.
- **Unreported calls are priced at zero.** These are the transport failures
  that ended after 8 s with no reply (3 in `ls20`, 1 in `rerun-ls20`). LongCat
  doesn't bill failed requests, so zero is likely right.

## What the runs cost

| Run | Model calls | Game actions | Decisions | Cost | Spent on calls that ran out or failed | Per game action |
| --- | --- | --- | --- | --- | --- | --- |
| `ls20` (bare raises) | 31 | 6 | 11 | $0.448 | $0.397 (89%) | $0.075 |
| `ft09` | 5 | 0 | 1 | $0.124 | $0.124 (100%) | — |
| `rerun-ls20` (ADR-0021 order, current) | 31 | 10 | 12 | $0.254 | $0.190 (75%) | $0.025 |

Supervisor consults: one each in `ls20` and `rerun-ls20`, about $0.003 each.

Across the 24 decisions, cost per decision: median $0.019, mean $0.034,
maximum $0.128 (`ls20` sequence 125, which ran out at every limit and on the
re-ask). The current order (`rerun-ls20`) made 1.2 decisions and 3.1 model
calls per game action.

**Output tokens drive cost.** Most of the money goes on reasoning in calls that
run out, not on prompts.

## What the cost limit reserves

With the trial settings (`--initial-output-tokens 4096`,
`--max-output-tokens 32768`, `--repair-budget 1`), a decision may make up to
6 calls, and the cost limit reserves each at the largest answer with no cache
hits:

| Prompt | Reserved per decision |
| --- | --- |
| 9,720 (largest in the trial) | $0.253 |
| 32,768 | $0.295 |
| 65,536 (the full `--token-budget`) | $0.354 |

A run is refused its next decision once spend plus this reservation exceeds
the limit. So the limit needs about $0.40 of headroom above the spend it is
meant to allow, or the last affordable decision is refused early.

## Proposed rule

**`--max-cost` per game = $0.15 × the game's action limit + $0.40.**

- **$0.15 per action** is the worst decision seen ($0.128) at the current
  1.2 decisions per action ($0.154), rounded. The limit only binds if a game is, throughout,
  as bad as the single worst decision in the trial. That is 6x the current
  measured rate and 2x the old-order rate.
- **$0.40** is one full-context worst-case reservation ($0.354), rounded up.
- **It is a backstop, not the main bound.** The action, model-call and time
  limits should stop a normal game first. The cost limit catches what they
  can't see in dollars: a rate change mid-campaign, and decisions that burn
  reasoning without producing actions (`ft09` spent $0.124 for no actions).

With the trial's limits, for example:

| Action limit | `--max-cost` | Expected at the current $0.025 per action |
| --- | --- | --- |
| 15 | $2.65 | about $0.38 |
| 50 | $7.90 | about $1.25 |
| 100 | $15.40 | about $2.50 |

The model-call limit already bounds spend on its own. One call costs at most
$0.059 (65,536 prompt + 32,768 answer), so 40 calls cost at most $2.36. With
the trial's 40 calls the $2.65 limit for 15 actions wouldn't bind. It matters
when the model-call limit is raised for longer games, and whenever the rate
changes.

**Campaign total, for planning.** If the scored suite is about 21 games (the
25 public games the manifest draft mentions, less the 4 development games;
the real list is set at freeze), 3 repetitions and only the candidate arm
spending: 63 games. At 15 actions per game that is about $24 expected and at
most $167 under the rule. Scale both with the action limit.

## How to improve the rule

The sample is small: 3 runs, 2 games, 16 game actions, 24 decisions, one
model. Treat the rule as a first estimate.

- **Price every run.** Pass `--rate-card` on every run, with or without
  `--max-cost`, so each call's cost is recorded in the journal.
- **Check the cache split.** The first run with the current code shows
  whether LongCat reports `cached_tokens`. If it does, re-price and expect
  input cost to fall.
- **Re-derive after the first campaign games.** Re-run
  `tools/price_recorded_runs.py` over the new runs and replace $0.15 with the
  95th percentile cost per action, and $0.40 with the largest reservation
  actually made. Record the new rule and its data here or in its successor.
- **Re-check the rate card before freeze.** It is a limited-time price. If it
  changes, write a new dated card. Never edit the old one, so every run
  keeps the rates it ran at.
