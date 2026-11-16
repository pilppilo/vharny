# ARC-AGI-3 campaign manifest

**Status: DRAFT. Not frozen. No scored run may start from this file.**

This is the predeclared campaign manifest that BENCH-0001 and PHASE-0004
step 5 require for the ARC-AGI-3 rung (rung 3). It fixes the suite, versions,
settings, budgets, repetitions, baseline, targets, tolerances, assistance
labels, and evidence locations before any target result is seen. ADR-0019
permits this campaign to proceed while the runtime delivery gate is open.

Tracking: Linear COD-39 (this manifest), COD-33 (the adapter it depends on).

## Decisions needed from the operator

Each row has a proposal so the draft is complete enough to review. Nothing
here is decided until the operator says so.

| # | Decision | Proposal | Why |
| --- | --- | --- | --- |
| 1 | Scored suite | The public ARC-AGI-3 games reported by the pinned toolkit, minus the development games in decision 2 | BENCH-0001 names "the available official ARC-AGI-3 set/toolkit" |
| 2 | Development games | `ls20`, `ft09`, `vc33` | They are the original preview games and the docs' examples; COD-34 confirms they run locally without an API key. Anything used for fixtures or tuning must not be scored |
| 3 | Where scored runs execute | Toolkit `ONLINE` mode through the ARC API, with one scorecard per campaign arm | The scorecard then comes from the external authority, as ADR-0002 requires. `COMPETITION` mode is not proposed: it scores against every available game, including the development games |
| 4 | Model and provider | Operator's choice; one model for the whole campaign | Results from different models cannot be pooled |
| 5 | Per-game budgets | Operator's choice of environment actions, model calls, and output tokens per game | Budgets bound cost and must be equal across arms |
| 6 | Repetitions | 3 per game per arm | Each game is long and costly; BENCH-0001 allows fewer than five when cost and confidence limits are disclosed |
| 7 | Baseline arm | The same adapter driven by uniformly random legal actions under the same action budget | Gives a floor that isolates what the agent adds; needs no model spend |
| 8 | Targets | Operator's choice; see "Targets" for the form and public reference points | Targets must be numeric and set before scoring |
| 9 | Failure-injection game | One scored game, chosen at freeze | PHASE-0004 requires injected process loss in at least one task per family |

## Suite

- **Family:** ARC-AGI-3, BENCH-0001 rung 3.
- **External authority:** the official ARC-AGI toolkit and its scorecard. The
  adapter submits legal actions and retains official results. It does not
  grade (ADR-0002).
- **Scored games:** to be listed at freeze as exact game IDs. A game ID has
  the form `<name>-<version>`, for example `ls20-016295f7601e`; the version
  part pins the game build. The list is taken from the toolkit's
  `get_environments()` at the pinned version, not typed by hand.
- **Development games:** per decision 2. Used for recorded fixtures (COD-34),
  conformance tests, the smoke run (COD-38), and any tuning. Never scored.
- **Public set size, for orientation only:** public reports describe 25 public
  games. The frozen list is authoritative, whatever its length.

## Versions

Filled at freeze, after COD-34 pins the toolkit.

| Item | Value |
| --- | --- |
| `arc-agi` toolkit version | pending COD-34 (0.9.9 was current on 2026-11-16) |
| Toolkit operation mode | per decision 3 |
| Vharness git revision | recorded at freeze; must be clean |
| Vharness package version | recorded at freeze |
| Agent configuration digest | recorded at freeze |
| Adapter runtime and evaluator identity | recorded at freeze |
| Supervision policy | the shipped default at the frozen revision (`monitor.p6.v1` under ADR-0014) |
| Model identifier and provider endpoint | per decision 4; key referenced by variable name only |

Scoring rules belong to the toolkit version. The toolkit's changelog shows
they have changed between releases (level scores squared in 0.9.3; caps
changed in 0.9.7), so results from different toolkit versions are not
comparable and are never pooled.

## Settings and budgets

Identical for every game and every arm unless stated.

| Setting | Value |
| --- | --- |
| Environment actions per game | per decision 5 |
| Model calls per game | per decision 5 |
| Output tokens per model call | per decision 5 |
| Supervisor calls | counted inside the model-call budget; recorded separately |
| Wall-clock limit per game | set at freeze |
| Kernel, memory, context, and supervision settings | the shipped defaults; no ARC-specific prompt, profile, or tuning (ADR-0003) |

A run that exhausts a budget ends there and is scored as it stands.

## Repetitions and seeds

- Per decision 6, each scored game is run the same number of times in each
  arm.
- The games are deterministic for a given action sequence; variation comes
  from the model. Where the provider accepts a seed it is recorded per run.
  Where it does not, the repetition index is the pairing key.
- Baseline and candidate runs are paired by game and repetition index.

## Arms

- **Baseline:** per decision 7.
- **Candidate:** `vharness-agent` at the frozen revision with the frozen
  settings.

## Contamination and memory

- Each scored game starts a fresh session with empty private memory.
- Nothing learned on development games, and no solution trace, walkthrough,
  or prior scored-game memory, enters a scored session.
- Scored and development runs use separate run directories.

## Assistance labels

Every run is labelled, and the labels are reported separately:

- **Autonomous:** no operator message or control after start.
- **Assisted:** any operator message or corrective control. Assisted runs do
  not count toward targets.
- **Aborted:** stopped by the operator. Retained and reported as not
  completed.

## Native metrics

Reported exactly as the official scorecard supplies them, with no
recomputation:

- campaign score;
- per-game score;
- levels completed per game, and games completed;
- environment actions per level and per game.

Vharness counters (model calls, tokens, supervisor calls, wall time, retries,
malformed outputs, resumes) are reported alongside as diagnostics and must
reconcile with retained receipts.

## Targets

Set per decision 8, before freeze. The form is fixed here; the numbers are
the operator's.

- **Completion target:** at least `N` levels completed in total across the
  scored games, counted on the median repetition per game.
- **Score target:** official campaign score of at least `X`.
- **Reliability conditions, all required:** zero duplicate external effects
  after recovery; every submitted action correlated to a receipt; the
  failure-injection game resumes without a blind resend.

Public reference points, for calibration only and not verified here: the
ARC-AGI-3 preview competition was won at 12.58%; one harness published an
unverified 36.08% on the 25 public games in March 2026. Scores from other
toolkit versions or other sets are not comparable with this campaign.

## Improvement, equivalence, regression

- **Improvement:** the candidate meets both targets and beats the baseline
  arm on campaign score and on total levels completed.
- **Equivalence:** candidate and baseline differ by less than the tolerance
  set at freeze on both measures.
- **Material regression:** not applicable to the first campaign on this rung;
  later campaigns compare against this one.
- **Inconclusive:** paired results too few or too variable to support the
  declared comparison. An inconclusive result is not a pass.

## Missing and failed runs

- A run that fails for a provider or network reason is retained as a failure
  and counts as zero toward targets. It is not silently rerun.
- A rerun is allowed only for a cell invalidated by an external change (for
  example a toolkit or game update mid-campaign). The affected cells are rerun
  under a new manifest version and are never merged with the old ones.
- An unknown outcome, where it cannot be shown whether an action reached the
  game, is reported as unknown and counts as zero.

## Failure injection

Per decision 9, one scored game has the coordinator process killed mid-game
and the session resumed. The run is reported with the rest, and must show no
duplicate action and an honest record of anything left indeterminate.

## Evidence

- **Location:** `evidence/campaign-arc-agi-3-<freeze date>/`, one directory
  per arm, game, and repetition.
- **Retained per run:** run manifest, session journal, context manifests,
  runtime receipts, stored frames, the official scorecard as returned, and
  the assistance label.
- **Retained per campaign:** this manifest as frozen, the closed scorecards,
  and the script that builds the aggregate tables from the raw files.
- Secrets and provider-hidden reasoning are excluded.

## Freeze procedure

1. COD-34 through COD-38 are done and the smoke run is retained.
2. The operator has made every decision above, and the tables are filled.
3. Status changes to FROZEN with the freeze date, the Vharness git revision,
   the agent configuration digest, and the exact scored game IDs.
4. The SHA-256 of this file as frozen is recorded in the campaign evidence
   directory and in WORK-0001.
5. After freeze this file is not edited. Any change is a new manifest version
   in a new file, and results under different versions are never pooled.

## Sources checked while drafting (2026-11-16)

- ARC-AGI-3 documentation: <https://docs.arcprize.org/> and
  <https://docs.arcprize.org/available-games>
- ARC-AGI toolkit repository and changelog: <https://github.com/arcprize/ARC-AGI>
- Game listing API: <https://docs.arcprize.org/api-reference/games/list-available-games>
