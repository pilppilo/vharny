# AGENTS.md

Stable repository engineering instructions take precedence over planning
documents. For current planning direction, read `plans/README.md` and apply
`plans/PROTOCOL.md`. This repository does not include the Plant CLI or the
coordination tooling.

## What this repository is

Two systems share this package:

1. **Legacy pipeline** (`vharness.cli`, probes, generators, detectors,
   evaluators): bounded scan and eval. It is not the Vharness Next kernel.
   The legacy entry point remains the default; any default-entrypoint switch
   requires a separate operator decision (ADR-0007).
2. **AVO-inspired session kernel** (`src/vharness/agent/`): durable journal,
   multi-action attempts, single committed lineage, and propose/receipt I/O.
   PHASE-0001 is verified. New agent work belongs here.

Vharness Next is a general-purpose long-horizon agent for authorized recon
and assessment, interactive reasoning, control, and software tasks. NVIDIA
AVO is architectural inspiration, not a clone and not a score target.

## Current work

1. Read `plans/README.md` for the current focus. It names Now, Read, and
   Workstream. Read `plans/PROTOCOL.md` completely when creating or changing
   a governed planning document, or while resolving a planning conflict.
2. For code shape, select `specs/proto/` documents for the affected module, in
   listed order. Read more documents for cross-module changes or when the
   module mapping is uncertain. Proto is an implementation map subordinate to
   accepted plans. If proto and a governing document conflict, quote both exact
   passages and ask which governs.
3. Open WORK-0001 history only to verify specific evidence or resolve a
   planning question.

Current phase status, readiness, scope, blockers, and next action are mutable
planning direction. Use `plans/README.md` and the authority order in
`plans/PROTOCOL.md`; do not duplicate those facts here. The handoff is
non-authoritative. Follow the current governing restrictions, including phase
and merge boundaries. If governing documents conflict, quote both and ask the
operator unless an accepted ADR explicitly resolves that same conflict. Do not
add empty later-phase modules. Do not rewrite Phase 1 contracts to make the
loop prettier. Stop at commit-sized boundaries.

## Operating boundary

When a live model or provider is needed, ask the operator for base URL,
model id, and the environment variable name that holds the key. Do not
search env, auth files, or leftover keys. If a named key is missing or
rejected, stop and ask again.

A named operator trio (base URL, model id, key env var name) is the provider
selection for its run. Treat it like any other run parameter: record it in
the run settings/receipt and proceed. Readiness is proven at launch time by
the acceptance gate's own live check (one authenticated call to exactly the
recorded trio; 401/403/402/429 and transport failures refuse the launch);
`vharness-agent probe-provider` is the tool for that call and for ad-hoc
checks, not a mandatory extra step once the gate covers it. Do not re-ask
permission, do not reconcile a new trio against previously recorded providers,
and do not treat a provider change as a planning conflict. Previously recorded
trios describe past runs; they authorize nothing about future ones.

Vharness is used only after the operator has established authorization.

The kernel reasons, plans, and proposes typed actions. The external runtime
executes or rejects them. External evaluators own completion and scores.
Do not add authorization verification, credential stores, policy gates,
target lifecycle control, side-effect auditing, or duplicate graders.

Safe-method behavior is a development and test profile, not a permanent
product limitation. Do not infer future capability caps from a phase plan,
benchmark plan, or safety-oriented slice.

When plans conflict, quote the conflicting text, name the document IDs, stop
dependent work, and ask which document governs. Do not independently declare
a plan obsolete, non-binding, complete, or out of scope.

Do not edit plan or spec status, completion criteria, roadmap direction, or
architecture boundaries unless the user explicitly asks for that edit.

## Architecture fidelity

Build one domain-neutral agent core. Environment adapters translate mechanics
only. Benchmark identity must not select prompts, memory policy, or control
flow.

The AVO operator is `Vary(P_t) = Agent(P_t, K, f)`: committed lineage, a
knowledge base the agent may consult, and an external evaluation function.
A variation attempt may contain many inspect, act, debug, and evaluate steps
in an order the **model** chooses. The coordinator must not impose a fixed
plan/implement/evaluate pipeline.

Unsuccessful attempts stay in journal history and never enter committed
lineage. Only an externally accepted evaluation creates a successor node.

Local contracts in ARCH-0001 are authoritative. Do not guess unpublished
NVIDIA internals.

PHASE-0002 modules: `kernel.py` (typed `AgentResult`, no I/O), `memory.py`
(projections over events), `context.py` (deterministic bands and manifest).
`session.py` applies one result through the existing intent/reservation/event
path. Do not fold those responsibilities into `session.py`.

## Python engineering

Apply these rules when creating or materially changing Python code. User
instructions and established repository conventions take precedence.

Before a non-trivial change:

1. Inspect nearby code, tests, and project configuration.
2. Identify whether the area has clear module boundaries or script-style
   orchestration.
3. If it is script-style, improve structure when doing so is small, local,
   and makes the changed behavior easier to test. Separate orchestration,
   I/O, parsing, and domain logic.
4. Do not preserve poor structure merely because it exists. Do not perform
   an unrelated repository-wide rewrite.
5. Reuse sound patterns; otherwise introduce the smallest clear structure
   the surrounding code can adopt.
6. State any material assumption briefly, then proceed.
7. Prefer the smallest design that cleanly meets the requirement.

Write production code, not one-off scripts. A CLI entry point or `__main__`
block may only parse arguments, configure dependencies, and call an
application function.

### Structure

- Give each module and function a clear, cohesive responsibility.
- Split when responsibilities differ, not to meet a line count.
- Keep related behavior together. Do not create generic `utils.py` modules.
- Put new code beside equivalent existing code.
- Extract duplication when it is shared behavior or policy, not coincidental
  similarity.
- Prefer explicit dependencies through constructors or parameters over
  global mutable state.
- Use guard clauses to keep nesting shallow.

### Types and data

- Add complete type hints to new or changed public APIs and important
  internal boundaries, including return types.
- Use domain models (`dataclass`, `TypedDict`, or an existing validation
  model) when data crosses a module, I/O, or business-logic boundary.
- Do not use `Any` unless required by an external API; explain why in a
  concise comment.
- Represent optional values as `T | None`.

### Errors and boundaries

- Validate untrusted input at the boundary.
- Catch only errors that can be handled meaningfully.
- Preserve causes: `raise DomainError(...) from exc`.
- Use project-specific exception types when callers must distinguish a
  domain failure. Do not create a custom exception for every error.
- Never use bare `except` or silently ignore an error.

### Readability

- Match formatter, linter, import style, Python version, and docstring
  convention. Default: PEP 8, line length 88, Python 3.10 syntax.
- Use descriptive names. Constants for domain-meaningful or reused values;
  keep obvious local literals inline.
- Do not add unused imports, dead code, commented-out code, speculative
  abstractions, or compatibility layers the task does not require.
- Use f-strings unless the surrounding code or API requires another style.
- Document public interfaces and non-obvious decisions. Avoid docstrings
  that only restate the function name.

### Verification

- Add or update tests for behavior changes and bug fixes.
- Run the narrowest relevant formatter, linter, type checker, and tests.
- Report what was verified and any check not run.

Target `src/vharness/agent` and `tests/agent` for Pylint and Pyright.
Do not whole-tree format the legacy pipeline unless asked.

### Completion

Do not present incomplete, untested, or assumed behavior as complete. State
limitations, assumptions, and remaining work.

## Verification commands

```text
uv run pytest
uv run pylint src/vharness/agent
uv run pyright
uv run black --check src/vharness/agent tests/agent
```

The agent kernel imports POSIX `fcntl`, so collect and run the suite on Linux.

## Planning edits

When the user asks to change governed plans, follow `plans/PROTOCOL.md`:
identify dependents, add an ADR when a durable choice changes, update
architecture and affected schematics, reassess phase readiness, update the
index in the same change, and append WORK-0001. When a new entry changes its
current scope, evidence or revision, unresolved question or blocker, or next
action, refresh the WORK-0001 Current handoff in the same planning change.
Reserve a new ID in the index before drafting a new governed document.

## Operator interaction discipline

- While the operator is applying a fix or handling anything on their side,
  make zero tool calls. When they say it is done, the next reply is the
  verdict on their fix and nothing else.
- An external interruption (antivirus action, permission prompt, unexpected
  failure) is reported the moment it is seen; everything stops until the
  operator says continue.
- A question from the operator is a complete task by itself. Answer it in
  words and end the turn. Do not run tools after answering, do not start or
  resume queued work in the same turn, and do not treat a question as
  approval to continue.

Write to the operator plainly and briefly. Lead with the answer, then the
detail — don't bury the point under structure. Use whatever precise terms the
work needs, but say what they mean the first time; the operator hasn't read the
plans and shouldn't have to ask.
