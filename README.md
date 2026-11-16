# Harny

Harny helps teams use language models for practical security assessment and
authorized technical work. It can scan source code, identify security
findings, produce CI-friendly reports, and run bounded interactive tasks with
durable evidence.

The default experience is a predictable code-security workflow:

```text
discover code → ask a model for a structured assessment → validate findings →
produce reports
```

Harny is designed for authorized testing and review. It does not grant access
to systems, approve targets, or replace your organization's security controls.

## Choose the workflow you need

### Scan source code

Use the standard `vharness` command to inspect a repository across supported
security domains and produce JSON, Markdown, or SARIF reports.

### Evaluate a model

Run the same assessment pipeline against the included labeled corpus or your
own chat-format dataset to compare model quality.

### Run a bounded interactive task

The opt-in `vharness-agent` preview provides a durable session kernel for
model-chosen actions, runtime receipts, external evaluation, and replayable
evidence. It currently includes a bounded software fixture and an official
Gymnasium/FrozenLake diagnostic adapter.

## Install

From a Harny checkout:

```bash
uv sync
```

For the optional Gymnasium diagnostic adapter:

```bash
uv sync --extra gymnasium
```

Harny requires Python 3.10 or newer. Model-backed workflows require an
OpenAI-compatible provider endpoint and the provider's API key.

## Scan a repository

Run an offline demonstration without making model calls:

```bash
uv run python -m vharness scan ./my-repo --generator mock
```

Run a real scan by configuring an OpenAI-compatible endpoint. Configuration can
come from command-line flags, environment variables, or a TOML profile.

```bash
# POSIX shell: export VHARNESS_API_KEY="<provider-key>"
# PowerShell: $env:VHARNESS_API_KEY="<provider-key>"
uv run python -m vharness scan ./my-repo \
  --base-url https://provider.example/v1 \
  --model <model-id>
```

To preview discovery and triage before sending anything to a provider:

```bash
uv run python -m vharness scan ./my-repo --dry-run
```

Create reports for common developer workflows:

```bash
uv run python -m vharness scan ./my-repo \
  --format sarif,markdown,json \
  --out report
```

SARIF can be uploaded to GitHub code scanning or another compatible security
results platform. Suggested patches are advisory and should be reviewed before
they are applied.

`scan` and `replay` exit with these codes:

| Code | Meaning |
| --- | --- |
| 0 | Every attempt was analyzed (and no findings, with `--fail-on-findings`). |
| 1 | Findings were reported and `--fail-on-findings` was set. |
| 2 | Usage or configuration error. |
| 3 | The scan is incomplete: some model calls or response parsing failed. |

An incomplete scan has not analyzed the code covered by its failed attempts, so
exit code 3 takes precedence over 1. Pass `--allow-incomplete` to exit 0 anyway.
Every report states how many attempts succeeded: SARIF sets
`invocations[0].executionSuccessful` and lists each failure as a tool execution
notification, the Markdown report flags an incomplete scan at the top, and the
JSON report is an object with `coverage` counts and a `findings` list.

## Configure a provider

Configuration is resolved per field in this order:

1. command-line flags
2. environment variables
3. a TOML configuration profile

Example profile:

```toml
# ~/.config/vharness/config.toml
[default]
base_url = "http://localhost:11434/v1"
model = "qwen2.5-coder:7b"

[openrouter]
base_url = "https://openrouter.ai/api/v1"
model = "mistralai/mistral-small"
api_key = "<store-this-securely>"
```

Use a named profile with:

```bash
uv run python -m vharness scan ./my-repo --profile openrouter
```

Do not put provider keys in prompts, source files, reports, or committed
configuration. Harny records run metadata and usage in its local logs; provider
billing, account quotas, and permissions remain provider responsibilities.

## Select security domains

Limit a scan when you know which areas are relevant:

```bash
uv run python -m vharness scan ./my-repo \
  --analyzers shell,distroconf
```

Built-in analysis areas include C/C++ memory safety and injection risks, web
application vulnerabilities, shell and installer scripts, distro/system
configuration, labeled corpora, and chat-format datasets.

## Evaluate a model

Score a model against the included labeled corpus:

```bash
uv run python -m vharness eval
```

Evaluate a chat-format JSONL dataset:

```bash
uv run python -m vharness eval \
  --dataset ./data/validation.jsonl \
  --skip-corpus
```

The evaluation workflow reports precision, recall, F1, false-positive rate on
clean examples, and CWE accuracy where labels are available.

## Vharness Next preview

`vharness-agent` is an opt-in preview for bounded interactive work. It keeps a
SQLite journal and content-addressed artifacts, records typed model decisions
and runtime receipts, and allows only an externally accepted evaluation to
promote a committed result.

Run the local software preview without a provider or secret:

```bash
uv run vharness-agent run --local-smoke \
  --objective "keep value equals 1" \
  --journal .vharness/smoke.sqlite3 \
  --artifacts .vharness/smoke-artifacts
```

The normal provider-backed path requires an explicitly authorized endpoint:

```bash
# PowerShell: $env:OPENAI_API_KEY="<provider-key>"
# POSIX shell: export OPENAI_API_KEY="<provider-key>"
uv run vharness-agent run \
  --objective "keep value equals 1" \
  --base-url https://provider.example/v1 \
  --model <model-id> \
  --api-key-env OPENAI_API_KEY \
  --max-actions 6 --max-model-calls 8 --max-evaluations 2
```

The preview also supports a scripted FrozenLake diagnostic checkpoint:

```bash
uv run vharness-agent run \
  --objective "reach the goal" \
  --environment gymnasium \
  --gym-id FrozenLake-v1 \
  --gym-seed 0 \
  --scripted-checkpoint \
  --max-actions 8 --max-model-calls 10 --max-evaluations 2 --max-steps 16
```

This checkpoint validates the mechanical adapter and evidence path; it is not
a claim of general model reasoning or general Gymnasium capability.

Sessions can be inspected and guided by an operator:

```bash
uv run vharness-agent status \
  --session-id <session-id> \
  --journal .vharness/session.sqlite3 \
  --artifacts .vharness/artifacts

uv run vharness-agent message \
  --session-id <session-id> \
  --journal .vharness/session.sqlite3 \
  --artifacts .vharness/artifacts \
  --text "continue with the current objective"
```

The preview is bounded and remains separate from the default scanner. Runtime
execution and authoritative evaluation stay outside the kernel.

## Projects and run history

Projects can keep a non-secret manifest beside local run state:

```bash
uv run python -m vharness project init ./my-service
uv run python -m vharness project status --project ./my-service
uv run python -m vharness project runs --project ./my-service
```

Usage history is available from local JSONL run logs:

```bash
uv run python -m vharness usage --log-file scan_log.jsonl
```

These records describe Harny activity. They are not a provider billing or
quota report.

## Security and privacy

- Use Harny only against systems, code, and data you are authorized to assess.
- Treat analyzed code and model output as untrusted data.
- Provider keys are supplied by environment-variable name for the agent preview
  and should never be placed in prompts or committed files.
- API errors and parsing failures remain visible in run results; they are not
  silently treated as clean scans.
- Suggested patches are advisory. Review them before applying changes.
- Harny does not provide authorization, target lifecycle control, credential
  management, policy enforcement, or authoritative grading.

## Outputs and replay

The scanner can produce Markdown, JSON, and SARIF reports. Run logs retain the
information needed to inspect what was sent, what the model returned, and how
the response was interpreted. The Vharness Next preview additionally retains
durable journal events and content-addressed artifacts for replay and evidence
verification.

## Extensibility

Installed packages can add probes, generators, detectors, or evaluators through
the `vharness.plugins` entry-point group. Local skills can provide read-only
instructions for a scan:

```bash
uv run python -m vharness scan ./my-repo --skill ./skills/security-review
```

Skill files are included as model instructions and their digest is recorded in
the run log. Harny does not execute scripts or automatically read unrelated
files bundled beside a skill.

## Current limitations

- The default scanner is the stable customer-facing workflow; Vharness Next is
  an opt-in preview.
- The Next preview supports bounded fixture environments, not arbitrary target
  control.
- The FrozenLake route is scripted diagnostic evidence, not a benchmark of
  general agent capability.
- OpenAI-compatible providers may expose different models, limits, usage
  telemetry, and error behavior.

## License

See the repository license files for licensing terms.
