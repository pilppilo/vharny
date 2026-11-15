# Real workspace and HTB runtime

Operator-approved, 2026-10-16. This specifies the PHASE-0004 runtime-delivery
gate; committed increments are evidence of partial implementation, not proof
that this specification is complete. ADR-0007 governs runtime-first delivery,
and PHASE-0004 retains all four campaign families as requirements for full
verification. This implementation map is subordinate to those documents.

## Required outcome

The operator can give `vharness-agent` an objective and either a codebase path
or an HTB box address. The agent uses real files, commands, network tools, and
interactive processes to pursue that objective. The operator can watch, give
direction, pause, stop, and resume without reconstructing the task manually.

Both workflows use the existing domain-neutral kernel, memory/context policy,
supervision, journal, and propose/receipt path. HTB identity must not select a
special prompt, scripted attack sequence, memory policy, or controller.

The operator creates the HTB box and connects the execution machine to its VPN.
Authorization is already established by the operator. There is no additional
authorization questionnaire, per-command approval gate, or target allowlist in
this feature. The operator chooses the execution environment before running.

The deliverable is a usable agent entry point and demonstrated real tasks.
Another fixture-only demonstration does not satisfy this specification.

## What exists and what is missing

The repository has `Environment`, `Runtime`, and `Evaluator` ports, durable
session creation/reopening, typed model decisions, operator commands, external
evaluation, and supervision. `agent/cli.py` currently constructs an in-memory
software fixture or a Gymnasium diagnostic. Its public commands are `run`,
`status`, and `message`; it does not provide a general workspace/tool runtime
or a command that resumes model execution from a stored session.

Implement the missing concrete runtime, application composition, and operator
surface. Reuse the existing kernel rather than building a second agent loop.
Integrate the existing supervisor; having supervision modules in the package is
insufficient if the runnable application never constructs or invokes them.

## Operator experience

These commands specify the proposed interface; they are not available yet.

Codebase work:

```sh
uv run vharness-agent run \
  --workspace /path/to/project \
  --objective "Fix the failing parser tests and explain the changes" \
  --check-command "uv run pytest tests/parser" \
  --run-dir .vharness/runs/parser-fix \
  --base-url <provider-url> --model <model-id> --api-key-env MODEL_KEY \
  --max-actions 100 --max-model-calls 150 --max-evaluations 10 \
  --max-steps 1000 --token-budget 16384 --max-output-tokens 2048
```

HTB work:

```sh
uv run vharness-agent run \
  --workspace /path/to/htb-work \
  --target 10.129.0.10 \
  --objective "Work on this HTB box; enumerate, investigate, and pursue access" \
  --run-dir .vharness/runs/htb-box \
  --base-url <provider-url> --model <model-id> --api-key-env MODEL_KEY \
  --max-actions 100 --max-model-calls 150 --max-evaluations 10 \
  --max-steps 1000 --token-budget 16384 --max-output-tokens 2048
```

The budgets above are explicit example run settings, not new defaults or a
guarantee that a task will finish. Preserve existing defaults until separately
chosen. Exhaustion retains a resumable run and reports the exhausted resource;
it cannot silently reset counters or start another session.

`--workspace` selects the real runtime. `--target` is optional task data visible
in the initial observation and run configuration; it does not select behavior
or restrict capabilities. An HTB run does not need a per-box integration or a
check command. Keep the existing fixture and Gymnasium commands working, and
reject ambiguous combinations of fixture switches and workspace options.

`--run-dir` owns the run manifest, journal, artifacts, and runtime records. Its
path resolves from the invocation directory, independently of `--workspace`.
An existing run directory cannot be overwritten by `run`. Resume uses its saved
session identity. Existing `--journal`/`--artifacts` usage remains supported;
combining those switches with `--run-dir` is rejected as ambiguous.

Operator controls:

```sh
uv run vharness-agent status --run-dir .vharness/runs/htb-box
uv run vharness-agent message --run-dir .vharness/runs/htb-box \
  --text "Investigate the web service next"
uv run vharness-agent pause --run-dir .vharness/runs/htb-box
uv run vharness-agent resume --run-dir .vharness/runs/htb-box
uv run vharness-agent stop --run-dir .vharness/runs/htb-box
```

Show the objective, current direction, remaining budgets, model decisions and
rationales, command/job identity, output, errors, questions, and supervisor
notices. Output must become visible while a long command is running. The
interactive terminal accepts messages and controls without waiting for a model
call or command to finish. A second command can submit durable controls to a
running session without opening another coordinator writer.

`resume` reattaches to an existing running coordinator or, after acquiring
exclusive ownership, reopens the journal and reconciles pending operations
before restarting model decisions. It does not create a fresh task. Resume
retains limits, counters, objective versions, messages, and unresolved effects.
Provider parameters may be supplied again; secrets are never recovered from
saved files. Extending an exhausted budget requires an explicit operator action
through the existing resource contract, or a separately reviewed contract
change if that contract cannot express it. Never implement an implicit reset.

## Real tool surface

Tools describe mechanics, not an imposed workflow. The model decides whether to
inspect, execute, debug, request evaluation, ask a question, or change approach.

| Action | Required behavior |
| --- | --- |
| `read` | Read a real file, with byte/line range selection and explicit truncation. |
| `write` | Create or replace a real text file; publish the previous/new content identity and resulting receipt. |
| `exec` | Start a command using the configured host shell and selected working directory; return a stable job ID and initial output. |
| `job_read` | Read new stdout/stderr or PTY output from a job using an explicit offset; return running/exited/unknown status and exit code when known. |
| `job_write` | Send input to a live job, including interactive terminal input and EOF. |
| `job_stop` | Request termination of an owned job/process group and report the observed outcome. |

An `exec` receipt says the start operation succeeded, not that the command or
objective succeeded. A running job is an external resource that subsequent
typed actions can inspect or interact with. This preserves the kernel's single
unresolved proposal rule without keeping the original start proposal pending
while the model needs to send input. Record the native command outcome
separately from successful submission/read/write operations.

Provide PTY support on the first supported execution platform, Linux/WSL, so
interactive commands, listeners, and acquired shells are usable. Pipes remain
available for ordinary commands and separate stdout/stderr. Persistent jobs can
outlive one tool action; they are not killed whenever `exec` returns. Commands
may use installed tools and network access, including the HTB VPN route. No
fixed enumeration/exploitation pipeline or task-category capability cap applies.

Working directories and paths are resolved explicitly; a directory change in
one shell must not silently alter every later tool invocation. Validate argument
types, paths, encodings, offsets, and job identity at the runtime boundary.
Workspace selection is a working location, not a claim of sandbox isolation.
Use the operator-selected execution user's actual permissions and runtime.

Retain complete command output in artifacts and expose bounded excerpts to the
model with retrieval references. Large output must not fill coordinator memory
or silently disappear. Distinguish tool-not-found, bad working directory,
permission error, nonzero exit, timeout, signal, truncation, and unknown outcome.
The model must receive actionable diagnostics and may choose another tool.

## Execution ownership and durability

Keep execution outside the kernel. The concrete environment connection submits
typed proposals to an operator-selected local runtime worker. That worker owns
OS processes, PTYs, output capture, job handles, and execution receipts. It makes
no agent decisions. Start with a local worker on the machine containing the
tools and VPN; a remote transport is not required for this delivery.

Session code remains responsible for intent, reservation, event application,
and lineage. Runtime storage owns only execution identities, dispatch facts,
job/output state, and recoverable receipts. Do not build an additional product
audit system, authorization service, target manager, or agent scheduler.

Before an effect, durably record the proposal identity and canonical request
fingerprint in the runtime. Repeated submissions with the same identity and
content return the recorded result; conflicting reuse is an error. Reconcile
against durable runtime facts after coordinator interruption. The runtime worker
is in-process and non-surviving: coordinator death loses live handles, and
ordinary subprocesses/descendants and their external effects may survive. The
operator surface reports such survivors `UNKNOWN`; it never auto-kills them. A
PID alone cannot establish ownership or command success, and a recovered PID is
never killed without verifying process-group/session identity plus a start
marker.

If the runtime died after starting an effect but before recording its outcome,
report uncertainty and retain the original operation identity. Do not rerun the
command automatically. `NOT_SUBMITTED` requires evidence that dispatch did not
occur. Neither a missing receipt nor a dead process proves that no effect
happened. The operator surface must show an unresolved operation and the
supported way to supply reconciliation evidence or end the run.

Pause prevents new model calls and tool submissions; it does not pretend an
already running command was frozen. Stop prevents scheduling and explicitly
requests shutdown of jobs owned by this runtime, reporting jobs whose outcome
remains unknown. An interrupt detaches/checkpoints where possible and states
which jobs remain live. Killing a process never implies reversal of its effects.
Reconnection and final close must account for all owned jobs and terminal state.

Use trajectory cursors for a mutable workspace and HTB observations. Do not
advertise restorable snapshots or automatic rollback. ARCH-0001 explicitly
allows irreversible environments to expose trajectory cursors: a committed
milestone records externally supported progress, not control of world state.
Failed attempts remain recorded and their effects remain real. New attempts
must see the actual current observation alongside the committed milestone.

## Model and operator configuration

Use the existing OpenAI-compatible model connection and counted repair path.
Require the operator's base URL, model ID, and named key environment variable
before live calls. A missing or rejected named key stops with an actionable
error; do not discover credentials or substitute a leftover/default key.

Persist non-secret settings, action schemas, runtime/evaluator identity, context
policy identity, limits, and usage. Do not add a credential store. Configure
supervision through the existing policy/accounting contract, expose its model
and cost settings, and retain accepted/rejected guidance and escalation. Use the
same policy and prompts for HTB and software; observations supply domain facts.

Operator messages can redirect attention; changing the objective or its success
condition uses the typed steering command and creates a new objective version.
Late results remain attributed to the version that caused them. Messages queued
during slow I/O must be applied before another decision or dispatch.

## Completion and evaluation

Useful execution is available without a benchmark campaign. Benchmark reporting
and claims of broad capability remain a separate evidence task. Do not require
campaign manifests merely to run an operator-directed task.

The model can propose completion but cannot accept its own work:

- With `--check-command`, the operator selects the external checker and its
  acceptance semantics. Retain its command, native exit/output, evaluated
  trajectory cursor, objective version, and applicable lineage baseline. A zero
  exit means the selected check passed; it does not prove requirements that the
  check does not assess. Timeouts and unavailable checkers never count as passes.
- Without a check command, expose an operator evaluation request. The operator
  can accept or reject a milestone/completion with an evidence reference and a
  reason. The response is correlated to that request, state, and objective.
  An unanswered request waits without model calls; it is not an automatic
  rejection or permission to keep retrying. Terminal and detached workflows
  must both support answering it.
- For HTB, keep native flag validation and submission in HTB/operator systems.
  The operator may supply their result as external evaluation evidence. Do not
  create a flag validator or make `--target` imply any automatic acceptance.

Only applicable, externally accepted evaluation can advance lineage or complete
a finite task. Starting a process, returning exit code zero from an arbitrary
agent-selected command, reaching a budget, and emitting a final message cannot
do so. Check results remain visible to the model for further debugging.

### Operator evaluation decision

The operator's answer to an evaluation request is a durable `EvaluationDecision`
command correlated to a `request_id`. Admission and durable intake are allowed
while paused or stopped. Consumption and promotion occur only when the request
is unconsumed, its objective version and attempt still match, and the session is
schedulable. A stopped session is terminal, so retained intake is evidence only.
The decision never acts as an implicit reset or an automatic pass.

- A nonempty operator reason is required for every decision. The receipt copies
  `request.evaluator_version` exactly and uses `comparison="operator acceptance"`.
  An accepted decision requires a typed `ArtifactRef`: `completed=False`
  records a milestone (`evidence=(evidence_ref,)`, `completed=False`), while
  `completed=True` records completion
  (`completion_evidence=(evidence_ref,)`, `completed=True`). A rejected
  decision requires `completed=False` and may omit its evidence reference.
- Receipt identity is deterministic from `request_id` plus the decision content.
  Replaying the same decision is a no-op; a conflicting decision for the same
  `request_id` is rejected visibly. Command admission/intake is persisted, and
  `command_applied` is committed atomically with promotion when the decision is
  schedulable and applicable.

When no check command is selected, persist an explicit typed evaluation mode
that selects operator evaluation. Never branch on `acceptance_semantics` prose
to choose this mode.

### Deferral and intake-only retention

Evaluation and operator-decision intake are recorded durably but are not
consumed or promoted while the session is paused. A check-command result or an
operator decision that arrives while paused records `evaluation_intake`
(`INTAKE_RECORDED`) only; on resume the intake-recovery path re-checks
promotion applicability against the current objective and head, so a steer
during the wait yields `STALE`. A stopped session is terminal: intake is
recorded and retained as evidence but never consumed or promoted. No new
implicit settlement transition is introduced; paused evaluation usage stays
unsettled until consumption and is visibly reported as unsettled exposure, and
stopped intake is retained with its unresolved accounting visible.

### Failure-preserving evaluation dedup

Re-applying an evaluation after a crash never manufactures success. A terminal
`RECEIPT_CONSUMED` operation returns its recorded disposition; a terminal
`VERIFICATION_FAILED` operation returns the recorded verification failure; a
`NOT_SUBMITTED` operation has no recorded receipt, so a newly arriving receipt
needs an explicit contradiction/reconciliation rule rather than the success
fast path. A terminal operation replayed with identical identity and content is
a no-op; different content under one `request_id` is a contract error.

## Module responsibilities

Settled placement:

| Location | Responsibility |
| --- | --- |
| `agent/adapters/workspace.py` | Workspace `Environment`: action schemas (`read`/`write`/`exec`/`job_read`/`job_write`/`job_stop`), observations, and boundary validation (types, paths, encodings, offsets, job identity). |
| `agent/adapters/process_runtime.py` | In-process local worker: process groups, PTYs, jobs, output capture into the artifact store, durable dispatch facts, reconciliation, and capability flags. |
| `agent/adapters/runtime_store.py` | Separate runtime execution SQLite store for operation fingerprints, dispatch facts, jobs, output references, and recoverable receipts; never a writer to the session journal. |
| `agent/adapters/process_jobs.py` | Blocking process/PTY/job operations used by the bounded runtime workers; returns typed results and owns no session state. |
| `agent/adapters/workspace_evaluator.py` | `--check-command` evaluator and operator accept/reject receipt construction. |
| `agent/control.py` | Transport inbox and local socket server/client; bounded, serialized payloads only, no SQL and no durable state. |
| `agent/application.py` | Composition root and coordinator loop: build dependencies, own the I/O worker pool, socket listener, and supervisor wiring, and drive `Session`. |
| `agent/cli.py` | Thin parser to application functions or the control client; keeps the observable behavior and test compatibility of the existing `run`/`status`/`message` fixture/Gymnasium paths through the refactor into `application.py`/`control.py`. The preservation promise is behavioral and test-enforced, not source-identity. |
| `tests/agent/test_workspace*.py` | Real filesystem/process contract tests, operator integration, and crash/reopen tests. |

**Session writer and runtime store.** The coordinator owns the session-journal
SQLite connection and is the only writer of session events, projections, and
session state. The runtime worker may write only its separate runtime execution
store described above. Socket/pipe control clients submit serialized commands
to the coordinator transport inbox. In-process I/O workers return serialized
or typed results through the same bounded inbox; `Session._inbox` is drained
only by the coordinator through `enqueue()`/`advance()`.

**Capability flags (evidence-gated).** `idempotency=True` (durably record
`(proposal_id, fingerprint, dispatch-intent)` before the effect and the result
afterward; the same ID and content returns the recorded result, conflicting
reuse is an error, and `UNKNOWN` is never re-executed). `reconciliation=True`
(`RECEIPT`/`NOT_SUBMITTED`/`UNKNOWN`/`UNSUPPORTED`; `UNKNOWN` does not imply
recoverability). `cancellation=False` (`job_stop` is an action; there is no
`Runtime.cancel` port and killing is not reversal). `revisions=False`
(workspace and HTB are irreversible trajectory cursors).

**Change surface.** `models.py` adds `EvaluationDecision(OperatorCommand)` and an
`OPERATOR_EVALUATION_WAIT` wait constant; `session_state.py` decodes it with
strict field validation; `transitions.py` and `replay.py` gain its transition
kind and reducer branches; `session.py` is decomposed into `prepare_decision`/
`apply_decision` with a `DecisionHandle`, adds three-phase action submission,
operator-evaluation pending state, duplicate fast paths, and reopen-only crash
reconciliation. `codec.py` needs no change beyond verifying round-trip.

Keep `kernel.py`, `memory.py`, and `context.py` responsibilities unchanged.
Any necessary session changes must apply existing typed results and controls,
not move execution, evaluation, terminal I/O, or transport logic into the
coordinator. Do not replace first-phase contracts to simplify the application.

## Delivery and acceptance

Deliver commit-sized increments, each leaving existing workflows working:

1. Real workspace read/write and command/job runtime through existing ports,
   including output retrieval, interactive input, timeout, and reconciliation.
2. Usable `run` composition and visible operator controls, durable run directory,
   provider configuration, and actual supervisor wiring.
3. Resume/reattach and external evaluation, with process-loss and operator-control
   tests through the application rather than only isolated runtime mocks.
4. Demonstrate one unscripted codebase task and one operator-selected HTB task.
   Record failures as failures and fix general defects exposed by real use.

The feature is not complete at steps 1–3. In particular, a scripted temporary
file edit and a prerecorded scan transcript cannot substitute for step 4.

| Required check | Acceptance evidence |
| --- | --- |
| Real files | The agent observes and changes the selected on-disk workspace; unrelated existing files are preserved unless the task explicitly changes them. |
| Commands and network | Real command output is returned to the model; a local network service tests mechanics without paid calls or HTB dependence. |
| Long and interactive jobs | A job remains usable across several actions; output arrives incrementally; input/EOF and termination reach the correct process. |
| Responsive controls | Messages, pause, and stop are admitted during a blocked command/model call and applied before subsequent scheduling. |
| Crash/restart | A new coordinator reopens disk state and reconciles; an effect executed before interruption is not blindly submitted twice. |
| Uncertain effects | Worker loss in the dispatch/receipt gap visibly blocks unsafe resend and preserves original identity. |
| Real state semantics | Failed attempts and restart retain current observations; committed lineage does not falsely imply filesystem or target rollback. |
| External completion | Rejection, missing evidence, stale versions, missing checker, and timeouts cannot promote or complete. Operator acceptance works while attached or detached. |
| Shared agent | Same core, prompts, context policy, and supervisor configuration run both task families without domain-dependent controller branches. |
| Codebase acceptance | With a real model and a selected repository, complete a concrete operator task; retain the diff, command results, and external acceptance. |
| HTB acceptance | With a real model and the operator's running box, independently discover and act on current observations toward the chosen objective; retain tool results and operator/HTB acceptance of the stated milestone. |

Before each live acceptance run, state the exact task and success condition.
For HTB, the condition must match what the operator wants to accomplish; do not
substitute a port scan if the requested outcome is access or box completion.
Retain the source revision, settings/limits, model/provider identity, journal,
context manifests, runtime output, supervision, assistance, and external result.
One successful task proves that task, not broad autonomous HTB capability.

Run the relevant new tests and existing agent regressions, Black, Pylint, and
Pyright, followed by the repository-required committed-source full gate under
the verification baton. Document execution platform support; Linux/WSL is the
first required platform, and native Windows PTY behavior must not be claimed
without corresponding evidence.

## Scope boundaries and review

ADR-0007 governs runtime-first PHASE-0004 delivery. PHASE-0004 is `implementing`
after its `accepted` → `ready` → `implementing` reassessment: PHASE-0003 is
verified, ADR-0007 is accepted, and runtime implementation commits are present.
The runtime delivery gate permits useful operator work before campaigns; all
four BENCH-0001 campaign families remain in scope and are required for full
PHASE-0004 verification. The legacy entry point remains the default. No merge
into `next` is planned for the foreseeable future; any later merge or
default-entrypoint switch requires a separate future operator decision.

Live-model acceptance still requires the operator's base URL, model ID, and
named key-environment variable. A prepared host, provider credentials, tool
setup, and selected HTB task are prerequisites for their live acceptance runs,
not for phase implementation.

**Control and ownership architecture.** The session coordinator alone applies
session-state changes and writes the session journal/projections. Bounded
workers may execute blocking model, supervisor, and runtime calls, then return
typed results through the coordinator's bounded inbox; they have no independent
scheduling or dispatch authority and cannot mutate session state. Runtime
execution records use a separate store. CLI control clients submit over local
socket/pipe IPC to the coordinator transport inbox; in-process workers return
through that same bounded inbox. The coordinator records command admission and
later applied/rejected disposition separately. `resume` reopens the journal and
reconciles pending work; with no live coordinator it never assumes a control
was applied without a durable event.

HTB provisioning/reset, VPN management, target lifecycle, credential storage,
authorization verification, flag grading/submission, and a new benchmark campaign
framework are excluded. Existing host access and installed tools supply execution
capabilities; tool errors remain observable. This is not a future capability cap.

The principal design choices for review are the shared local runtime, real
interactive jobs, durable run/resume interface, external completion path, and
the requirement for both real acceptance runs. A remote execution transport,
browser interface, and automatic installation of a tool suite can be considered
separately; they are not prerequisites for running on a prepared Linux/WSL host.
