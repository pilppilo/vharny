# LongCat-2.5 sizing trial on two development games

Two ARC-AGI-3 development games, `ls20-9607627b` and `ft09-0d8bbf25`
(campaign manifest draft, decision 2), played through `vharness-agent` with
**LongCat-2.5**. Toolkit `arc-agi` 0.9.10 in `normal` mode. Code: commit
`3e99b4d` (main after #31). Recorded 2026-11-17.

The trial was run to size the campaign budgets and to test two changes against
the real provider:

- streamed replies (`--provider-stream`, COD-76);
- answer-limit growth (`--initial-output-tokens`, COD-88).

It is not a scored run. Both runs are kept as they happened.

## Settings

Both games, run in parallel:

| Setting | Value |
| --- | --- |
| `--provider-stream` | on |
| `--provider-timeout` | 600 s |
| `--initial-output-tokens` | 4,096 |
| `--max-output-tokens` | 32,768 |
| `--token-budget` | 65,536 |
| `--max-actions` | 15 |
| `--max-model-calls` | 40 |
| `--max-evaluations` | 1 |
| `--repair-budget` | 1 |

## Result

| Game | Decisions answered | Game actions | Time in answering calls | Time in calls that ran out or failed | End |
| --- | --- | --- | --- | --- | --- |
| `ls20` | 7 | 6, all `succeeded` | 7.2 min | 67.1 min | A decision ran out at every limit and on the re-ask; the operator had already sent `stop` |
| `ft09` | 0 | 0 | 0 | 24.1 min | The first decision ran out at every limit and on the re-ask |

Neither game was evaluated, so there is no scorecard.

## What the trial shows

### Streaming

Streaming gets past the 300 s cut. Before, calls ended at 300 s even with a
600 s timeout (smoke run, COD-76). Here, calls of 420 s to 526 s finished
normally: for example `ft09` sequence 8, at 526 s and 483 s.

`probe-provider --provider-stream` confirmed that LongCat streams usage
counts, reasoning tokens included.

### Growing the limit

Growth works as built, but it doesn't suit this model:

- **Raises restart the reasoning.** After a raise, LongCat starts its
  reasoning over from scratch. It doesn't continue where it was cut off.
- **Reasoning length varies from try to try.** The same decision can need
  very different lengths on different tries. `ls20` sequence 26 ran out at
  16,384 tokens, then answered at the next limit in 1,591. Sequence 51 ran out
  at 4,096, then answered in 370. A raise therefore works more like a fresh
  roll of the dice than a bigger budget.
- **Most of the time and tokens went on calls that ran out.** Across both
  games, answering calls took 7.2 min. Calls that ran out or failed took
  91.2 min.

ADR-0017's re-ask, which asks for the answer now without more reasoning,
rescued a decision the raises could not. `ls20` sequence 85 ran out at every
limit up to 32,768, then the re-ask answered in 44 s with 2,755 tokens. It did
not always work: sequence 125 and `ft09` sequence 8 ran out on the re-ask too.

### One decision ends the game

Both runs ended because one decision ran out at every limit and on its re-ask.
In a campaign that ends the game early. A decision that runs out should
probably cost model calls, not end the game (see COD-86).

### Pace

`ls20` took 75 minutes for 6 game actions. At this pace a 15-action game
takes about 1.5 to 2.5 hours.

### Provider errors

`ls20` had three provider transport failures. Each ended after 8 s with no
reply, at sequences 73 (on the re-ask), 102 and 113. The run recovered from
each one.

## Each decision in `ls20`

Attempts are given as answer limit, how the call ended, output tokens and
duration. "length" means the call ran out.

| Seq | Outcome | Attempts |
| --- | --- | --- |
| 8 | answered | 4,096 stop 1,357 23 s |
| 15 | answered | 4,096 length 68 s; 8,192 length 137 s; 16,384 length 282 s; 32,768 stop 14,675 257 s |
| 26 | answered | 4,096 length 57 s; 8,192 length 109 s; 16,384 length 245 s; 32,768 stop 1,591 27 s |
| 41 | answered | 4,096 stop 1,743 31 s |
| 51 | answered | 4,096 length 59 s; 8,192 stop 370 9 s |
| 61 | answered | 4,096 length 64 s; 8,192 stop 2,256 37 s |
| 73 | transport failure | 4,096 / 8,192 / 16,384 / 32,768 all length (51, 120, 236, 474 s); re-ask failed after 8 s |
| 85 | answered | 4,096 / 8,192 / 16,384 / 32,768 all length (57, 106, 194, 420 s); re-ask stop 2,755 44 s |
| 102 | transport failure | failed after 8 s |
| 113 | transport failure | failed after 8 s |
| 125 | ran out | 4,096 / 8,192 / 16,384 / 32,768 all length (55, 105, 241, 454 s); re-ask length 466 s |

Game actions: `1, 1, 1, 2, 2, 2`.

## Re-run of `ls20` with the ADR-0021 order (COD-91)

`rerun-ls20/` is the same game with the same settings, at commit `9e9f2d4`
(main after #33). With growth on, a call that runs out is now first re-asked
for the answer at the same limit, and the raises that follow keep asking for
it (ADR-0021).

Both runs made 31 model calls:

| | Trial (bare raises) | Re-run (answer now first) |
| --- | --- | --- |
| Decisions answered / failed | 7 / 4 | 11 / 1 |
| Game actions | 6 | 10, all `succeeded` |
| Time in answering calls | 7.2 min | 7.5 min |
| Time in calls that ran out or failed | 67.1 min | 28.1 min |
| Output tokens in those calls | 282,624 | 114,688 |
| Wall-clock span | 75 min | 36 min |

The re-run's one failure was a provider transport error after 8 s
(sequence 27); the run recovered.

How the decisions ended:

- **Fit the starting limit:** 3 of 11 decisions answered within 4,096 tokens.
- **The same-limit re-ask answered:** 2 decisions. Sequence 68 took 5 s and
  125 tokens; sequence 131 took 11 s and 514 tokens.
- **A raised call answered, still asking for the answer:** 6 decisions. Often
  the answer came quickly: sequence 99 took 6 s and 160 tokens at 16,384;
  sequence 80 took 23 s and 430 tokens at 32,768.
- **The same-limit re-ask ran out too:** in 6 of the 8 decisions that ran out
  at first. LongCat does not reliably stop reasoning when asked.

The run stopped at sequence 154, waiting for an operator answer to the
supervisor's third repetition alert, when the cloud environment restarted and
ended the process.

This is one run of each order. Reasoning lengths vary from try to try, so the
comparison is indicative, not controlled. The direction is clear, though: the
run that kept asking for the answer spent less than half the time and tokens
on calls that ran out, and lost one decision instead of four.

## Files in each game folder

The folders are `ls20/`, `ft09/` and `rerun-ls20/`; each holds:

- `manifest.json`: the run directory's `run.json` (settings, no secrets).
- `session.sqlite3`: the full session journal (self-contained copy).
- `runtime.sqlite3`: the runtime's write-ahead dispatch records.
- `artifacts/`: content-addressed observations, receipts and evidence.
- `context-manifests.json`: every `context_assembled` event.
- `report.json`: everything extracted from the journal:
  - decisions and model failures, each attempt with its `output_limit`;
  - limit raises and re-asks;
  - game actions and their receipts.
- `run.log`: the operator stream. Neither log contains a key.

The provider key was loaded from a file whose value ended in a carriage return.
The first probe failed as a "connection error" until the line ending was
stripped. COD-90 tracks reporting that as a clear configuration error.
