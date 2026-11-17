# ARC-AGI-3 local smoke test with a real model (COD-38)

One public ARC-AGI-3 game, `dc22-fdcac232`, played through `vharness-agent` with
**LongCat-2.5** (`https://api.longcat.chat/openai/v1`), toolkit `arc-agi` 0.9.10
in `normal` mode (game run locally, scorecard on the ARC API, anonymous key).
Code: commit `b8cde12` (main after #22). Recorded 2026-11-17.

This is a smoke test of the whole path, not a scored campaign run. It is kept
as it happened, failures included.

## Result

The path works end to end: game download, reset, model decisions, game
actions, durable receipts, supervision, operator controls, scorecard
evaluation and a clean stop. The model did not make progress on the game: the
official scorecard reports 0 of 6 levels and score 0.0, so the session
correctly rejected both evaluations.

| Attempt | Settings that changed | Outcome |
| --- | --- | --- |
| `attempt-1-output-512` | defaults: 512 output tokens, 16,384-token context, 120 s provider timeout | Failed on the first decision: both tries spent 511 of 512 output tokens on reasoning (`output-exhausted`). Input was 15.8K tokens. |
| `attempt-3-timeout-120s` | 8,192 output tokens, 65,536-token context | 2 game actions succeeded. Two calls hit the 120 s provider timeout; one decision then spent 8,191 of 8,192 tokens on reasoning, twice, and the run ended in `output-exhausted`. |
| `attempt-4-scorecard` | 32,768 output tokens, 600 s timeout, 20-action budget | 11 game actions, all accepted. Paused and resumed mid-game, supervisor escalations answered by the operator, 2 scorecard evaluations (`incomplete`), then stopped by the operator. |

Attempt 2 is not kept: it ran in a sandbox that ended the process when the
launching shell call returned, before its first model call finished. That was
a harness problem, not the model or vharny.

## Attempt 4 in detail

- **Game actions (11):** `1, 1, 1, 2, 2, 1, 6, 6, 1, 1, 1`. Every receipt is
  `succeeded`. The scorecard independently counts 11 actions and 0 resets,
  matching the runtime's own durable count.
- **Model decisions (15):** 11 actions, 2 evaluation requests, 2 answers to
  supervisor guidance. Decision time 7.4 s to 221.9 s (median 15.7 s);
  reasoning tokens 80 to 17,283 (median 487). One transport error was
  recovered automatically.
- **Operator controls, live:** `pause` (status went to `paused` once the
  in-flight call finished), `resume`, two `message`s, `stop`. All five were
  admitted by the running coordinator (`command_admitted` = 5).
- **Supervision:** the progress supervisor flagged repetition of action 1 and
  escalated to the operator twice (stages 3 and 4). The operator answers asked
  the model to stop repeating and request an evaluation, which it did. These
  are operator steering, recorded in the journal; they are not a hint about
  the game.
- **Scorecard (`scorecard.json`):** `completed: false`, `score: 0.0`,
  `levels_completed: 0` of `level_count: 6`, latest run `NOT_FINISHED`. The
  evaluator copied these numbers without recomputing them.

## What this shows about LongCat-2.5 on this task

- It reasons at length before acting on a full 64x64 frame. The default 512
  output tokens and 120 s timeout are far too small; 32,768 tokens and 600 s
  worked.
- It tends to repeat one action, which the supervisor catches.
- A 200-action run would take hours at this pace. Campaign runs with this
  model need either a much longer wall-clock budget or a model that decides
  faster.

## Files in each attempt

- `manifest.json`: the run directory's `run.json` (non-secret settings).
- `session.sqlite3`: the full session journal (self-contained copy).
- `runtime.sqlite3`: the runtime's write-ahead dispatch records.
- `artifacts/`: content-addressed observations, receipts and evidence.
- `context-manifests.json`: every `context_assembled` event.
- `report.json`: decisions, failures, actions, receipts and evaluations
  extracted from the journal.
- `run.log`: the operator stream. The toolkit printed its anonymous ARC key
  here; it is redacted (`[redacted]`), and COD-71 tracks stopping the toolkit
  from logging it.
- `scorecard.json` (attempt 4 only): the stored official scorecard.

The LongCat key was passed by environment-variable name only and appears in
none of these files.
