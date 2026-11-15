---
id: PHASE-0004
title: Deliver the operator runtime, then validate the capability ladder
type: phase
status: implementing
owners: []
created: 2026-09-04
updated: 2026-10-16
depends_on: [ARCH-0001, ROAD-0001, BENCH-0001, PHASE-0003, ADR-0007]
supersedes: []
related: [ARCH-0001, ROAD-0001, BENCH-0001, ADR-0007, WORK-0001]
---

# Deliver the operator runtime, then validate the capability ladder

## Observable outcome

PHASE-0004 first delivers an operator-usable local runtime for real workspace
and HTB work through the existing kernel, memory/context policy, supervision,
journal, and proposal/receipt path. Runtime delivery can be used before
benchmark campaigns are prepared. The phase then validates the unchanged agent
machinery through software, Gymnasium, ARC-AGI-3, and operator-authorized Hack
The Box campaigns. All four campaign families remain required for full phase
verification.

## Entrance criteria

PHASE-0003 is verified and ADR-0007 is accepted. These conditions permit
PHASE-0004 implementation. A prepared host, provider credentials, tool setup,
and selected HTB task are prerequisites only for their corresponding live
acceptance runs. Before each scored campaign run, its BENCH-0001 manifest must
freeze suite and versions, settings, budgets, seeds, repetitions, targets,
tolerances, assistance labels, and evidence locations. The operator selects
and independently authorizes any HTB lab before its use.

## In scope

- Thin mechanical environment integrations needed for the selected suites.
- A usable `vharness-agent` local runtime for real workspace and operator-selected
  HTB tasks, using the existing domain-neutral agent contracts.
- Bounded blocking I/O, model, and supervisor calls with one coordinator owning
  session-state application and the session journal; a separate runtime store
  may hold execution records.
- Local IPC for CLI control clients and a bounded in-process result inbox for
  workers, with responsive operator controls.
- Durable run/resume, reconciliation, and externally attributable evaluation,
  including typed persisted operator-evaluation mode when no check command is
  configured.
- One unscripted codebase acceptance task and one operator-selected HTB
  acceptance task, with external evaluation evidence.
- Contract conformance tests proving adapters do not change kernel policy.
- Paired baseline/current runs, repetitions, failure injection, and cost reports.
- Operator-assisted versus autonomous outcome labeling.
- Per-rung capability conclusions, cross-domain regression analysis, and honest
  milestone recommendations based on external evidence.

## Out of scope

Target provisioning/reset, authorization checks, credential management, VPN
management, flag validation/submission, cloned benchmark graders, benchmark-
specific prompts, model fine-tuning, and deletion of the previous execution path.

## Contracts added or changed

PHASE-0004 adds the runtime/application/operator contracts governed by ADR-0007
and specified in `specs/proto/06-workspace-runtime.md`. Only the coordinator
writes session events, projections, or session state. Workers may write their
separate runtime execution store and return results, but have no independent
scheduling or dispatch authority and cannot mutate session state. If an
external environment cannot be expressed through ARCH-0001 and ADR-0007, stop
and propose an architecture/decision amendment rather than smuggling behavior
through an adapter. Pin each external protocol/version in its run manifest.

## Implementation sequence

1. Deliver real workspace file and command/job operations through the existing
   ports, with durable operation identity and honest handling of unknown effects.
2. Compose a usable `vharness-agent` run/resume interface, run directory,
   responsive local IPC controls, supervisor calls, and external evaluation.
3. Exercise reopen/reconciliation, pause/stop, operator evaluation, and recovery
   through the application. Stop is terminal; no test or interface implies that
   stopping or killing reverses effects.
4. Retain one unscripted codebase task and one operator-selected HTB task with
   source revision, settings, journal, context manifests, runtime output, and
   external evaluation evidence. Each task must receive external acceptance
   for its predeclared objective or milestone; retain failed trials as failures
   and repeat after correcting general defects. For HTB, the objective and
   acceptance condition come from the operator; a scan cannot substitute when
   the requested outcome is access or box completion.
5. Freeze each campaign rung's suite, versions, settings, budgets, seeds,
   repetitions, baseline, targets, tolerances, assistance labels, and evidence
   locations before scored runs.
6. Run smoke tests, then the paired/repeated BENCH-0001 matrix through all four
   target environment families.
7. Analyze completion, efficiency, reliability, recovery, and human
   intervention; fix general defects or create explicit experiments without
   tuning hidden per-environment policy.
8. Independently review raw evidence and reproducible aggregates, and publish
   achieved and remaining campaign rungs.

Passing the runtime delivery gate makes the runtime available for operator use
while PHASE-0004 remains `implementing`. It does not mark the phase implemented
or verified; all phase exit criteria and the campaign verification gate remain
required.

## Compatibility and migration

The legacy entry point remains the default and selectable for comparison. The
new kernel writes only its versioned journal format. No merge into `next` is
planned for the foreseeable future. Any later merge, default-entrypoint
switch, removal of old internals, or migration of old state requires a separate
future operator decision and is outside this phase.

## Test strategy

Run adapter conformance tests with recorded external fixtures before live use.
Execute the repetition and reporting rules in BENCH-0001. Independently verify
aggregate calculations from raw manifests and receipts. Inject process loss in
runtime application tests and campaign tasks; preserve uncertainty and verify
that recovery does not cause blind duplicate effects. Campaign failure
injection covers at least one task per family.

## Exit criteria and required evidence

### Runtime delivery gate

- The operator can use `vharness-agent` on a supported Linux/WSL host to read
  and write real workspace files, execute and inspect commands, and use
  interactive jobs over multiple actions. Output remains retrievable and
  diagnostics distinguish failure from unknown outcomes.
- The operator surface admits messages, pause, and stop while a model call or
  command blocks. A paused session makes no new model calls or submissions;
  queued controls are applied before later scheduling. Stop prevents new work,
  requests shutdown of owned jobs, reports survivors or uncertain outcomes, and
  leaves the session terminal.
- Session events and state are written only by the coordinator. Runtime
  execution records are stored separately. Bounded workers may return model,
  supervisor, or runtime results through the coordinator inbox, without
  independent scheduling or dispatch authority.
- Run/resume reopens durable state and reconciles pending operations. Missing
  receipts, lost handles, or dead processes do not prove non-submission. An
  indeterminate operation is not resent automatically, and neither stop nor
  process termination is represented as effect reversal.
- With a configured external check command, its native result and applicable
  trajectory/objective identity remain recorded. Without one, the typed,
  persisted operator-evaluation mode records a correlated request and decision;
  the model cannot accept its own work. Paused or stopped sessions may retain
  evaluation intake as durable evidence, but only a schedulable, applicable
  decision may be consumed or promoted. Stopped sessions remain terminal.
- Complete one unscripted real codebase task and one unscripted
  operator-selected HTB task with external acceptance of each predeclared
  objective or milestone. Each record includes the source revision, non-secret
  settings, journal, context manifests, runtime output, operator assistance,
  and external evaluation evidence. Retain failures as failures; claims remain
  limited to accepted demonstrated tasks. The HTB objective and acceptance
  condition come from the operator, and a scan cannot substitute for a request
  to obtain access or complete the box.
- Document the supported execution platform and demonstrate these contracts
  through the application and operator surface, not only isolated runtime
  mocks.

Passing this gate permits useful operator-directed execution before campaigns
are prepared. It does not mark PHASE-0004 `implemented` or `verified`.

### Campaign verification gate

- All four target environment families meet their predeclared capability targets;
  otherwise the unmet rung and evidence remain explicit and the phase is not
  represented as fully verified.
- Run manifests prove unchanged core/prompt/algorithms across families.
- Efficiency, reliability, recovery, and human-intervention results satisfy
  BENCH-0001 reporting, predeclared tolerances, and regression coverage.
- Earlier achieved rungs remain regression coverage; campaign failure injection
  exercises at least one task per family.
- No adapter contains agent planning, scoring imitation, authorization, target
  lifecycle, or benchmark-specific behavioral prompts.
- Raw evidence and reproducible aggregate calculations receive independent
  review. All repository verification commands pass. PHASE-0004 becomes
  `implemented` only when its full scoped work exists, and becomes `verified`
  only when both named gates and all exit criteria have linked evidence.

A default-entrypoint switch is not an exit criterion.

## Risks and recovery

External services, costs, and environment changes can make runs incomparable.
Invalidate and rerun only affected cells with a new manifest; never merge them
silently. A weak domain result is evidence for a general algorithm experiment,
not permission to add a hidden benchmark profile. Coordinator or runtime-worker
loss can leave processes or effects live or unknown; missing receipts do not
prove non-submission, and stop or process termination does not reverse effects.
