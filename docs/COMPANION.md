# MCAT companion

The companion is a local-first, single-learner evidence ledger with an optional
private service. Each person runs their own instance. The public repository holds
code, prompts, schemas and synthetic examples; learner data belongs in `private/`
or a private database. Code is provided under the [MIT license](../LICENSE).

## What works locally

- Additive capture storage with the original allowlisted fields, revision history,
  source conflicts, preserved legacy IDs and partial-coverage warnings.
- Calendar daily, weekly and monthly metrics and immutable report versions.
  Reports separate source/help/exposure conditions and retain denominators.
- Reviewed fixed-choice item import, optional lesson offers, answer-before-feedback
  persistence, hints, pauses, resumptions, partial completion and disputes.
- Independent delayed recall and transfer checks with actual lag, prespecified
  eligibility, and matured/tested/passed denominators.
- Text, Markdown, CSV and DOCX context uploads; optional structured question-history
  entries. A later upload can revise the same document and entry IDs. No Google
  account connection or ongoing Google Doc sync is needed.
- Separate official form history and conservative descriptive readiness criteria.
- Backup/restore, optional collector outbox/cursor sync, OAuth-protected MCP HTTP,
  local MCP stdio, and an optional budgeted local item-authoring pipeline.

The live ChatGPT account connection, notification delivery and scheduled writeback
are **unverified**. No schedules are installed by setup. PostgreSQL uses the same
ledger operations but requires a separate integration test before deployment.
No subject-expert-reviewed MCAT item bank ships with this release. An empty bank
returns `reviewed_bank_exhausted`, rather than presenting unreviewed items.

## Install and try the complete synthetic loop

Use Python 3.12 for the companion. The existing collector still supports its
documented Python versions. From the repository root on Windows:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-companion.txt
.venv\Scripts\python.exe -m companion.demo
.venv\Scripts\python.exe -m unittest discover -v
```

The demo uses a temporary isolated database, synthetic arithmetic questions and
scripted responses. It proves software behavior, not instructional quality or
MCAT improvement. It makes no API calls and does not touch actual learner history.
On macOS/Linux, substitute `.venv/bin/python`.

## Set up your own history

Choose your own IANA timezone. `America/New_York` below is an example, not an inferred
learner preference. Unknown goals, exam date and available time do not block reports.

```powershell
.venv\Scripts\python.exe -m companion setup --timezone America/New_York
.venv\Scripts\python.exe -m companion migrate-v1 private/study.sqlite
.venv\Scripts\python.exe -m companion import private/diagnostic-report-capture.json
.venv\Scripts\python.exe -m companion status
.venv\Scripts\python.exe -m companion report daily --out private/daily-report.json
```

Migration reads the old database without modifying it. Existing observations and
source revisions survive. Where v1 lacks original capture metadata, migration
records that limitation; re-importing the saved capture enriches those fields.
The collector now also saves new allowlisted captures to `private/companion.sqlite`
and queues them locally for optional sync. It does not upload automatically.
The v1 `accuracy` field retains its original answered-accuracy meaning.

Customize setup with a copy of `examples/companion_preferences.json` under
`private/`, then pass `--preferences private/preferences.json`. Setup values are
versioned. UTC in the example is a placeholder. Four offers/day and the pilot
delayed windows are configurable design defaults, not established optima.

Weekly reports cover the preceding Monday–Sunday; monthly reports cover the
previous full calendar month. Boundaries use IANA timezones and handle DST.
`--as-of` sets the evidence cutoff. To correct a historical period after a late import:

```powershell
.venv\Scripts\python.exe -m companion report daily --period-as-of 2026-10-23T12:00:00Z --correction-reason "Late capture added evidence to October 22"
```

The original report version remains retrievable. No import time becomes a study
occurrence. Ordinary reports use a deterministic narrative to prevent unsupported
numeric claims. ChatGPT can discuss that evidence, but free-form model prose is
not currently archived as a validated report; exact narrative review/validation
is a later extension. Weekly and monthly reports use the same concise format.

## Upload question history or other context

Export the one-time Google Doc as `.txt` or `.docx`, or supply `.md`/`.csv` text:

```powershell
.venv\Scripts\python.exe -m companion upload-context private/question-history.docx --id question-history --title "My question history"
```

To provide structured dates, reasons, topics and explicit attempt links, add
`--entries private/history-entries.json`, using `examples/context_entries.json`
as the shape. Entries need stable IDs. An edit uses the same document ID and
`--expected` with its current version. Original text and prior versions are kept.
Plain uploaded text is retained as context; it is not automatically interpreted
as a list of proven weaknesses. Topic/time similarity does not confirm a link.
Uploaded context can also be saved through `study_upload_context` in ChatGPT.

## Lessons and content QA

Content has separate `learner_view` and protected `instructor_view` fields. Import
an item plus a review file using `install-item ITEM.json --review REVIEW.json`.
`companion/models.py` defines the schema. A content version is immutable.
The review must identify the exact learner-view hash, a key-withheld solution,
reviewer identity, and every release check from `companion/items.py`.
Administrative imports assert review provenance; they do not prove an expert
actually reviewed an item. Audit all disputes and a sample with a qualified reviewer.

An accepted diagnostic needs a separate immediate-application item for the same
section/concept/skill and a different item family. Delayed recall and delayed
transfer items have distinct purposes and unseen families. The current rubric
uses fixed-choice correctness; reasoning is saved but remains explicitly ungraded.
No model-submitted correctness can override the server key.

The optional `generate-item BLUEPRINT.json --config CONFIG.json` command uses the
local OS-stored API key or `OPENAI_API_KEY`. It performs authoring, an independent
key-withheld solve, and a review in separate requests. Configure an explicit model,
current input/output prices per million tokens, checked pricing date, positive
daily/monthly USD caps and `max_output_tokens`. `ModelConfig` documents these fields.
It will not retrieve sources: missing verified claim excerpts return `needs_research`.
No billable generation was used to implement or test this release. API usage/cost
estimates and uncertain reservations are logged privately. Never upload that key
to a server merely to enable fixed-key grading or ChatGPT tutoring.

## ChatGPT, local use and hosting

Three deployment choices use the same service:

| Choice | Database | PC must stay on? |
|---|---|---|
| Local MCP | Local SQLite | Yes |
| Always-on self-host | SQLite on persistent disk or PostgreSQL | The service host must stay on |
| Cloud service | Private persistent SQLite disk or PostgreSQL | No; collection still requires the local PC |

Local MCP clients can launch:

```powershell
.venv\Scripts\python.exe -m companion.mcp_server --db private/companion.sqlite
```

Stdio trusts the local operating-system session and has no network listener.
For ChatGPT's supported local connection workflow, consult
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
and test account support. A tunnel does not make a sleeping PC available.
Tunnel setup needs a Platform tunnel ID and a separate runtime control-plane key.
It supports private connections; it is not a public plugin-distribution mechanism.
Open-source users can run their own instance and create their own private connection.

Remote HTTP requires an OAuth 2.1 authorization provider and HTTPS. Set:

```text
COMPANION_DATABASE=postgresql://... or /data/companion.sqlite
COMPANION_LEARNER=an-opaque-instance-owner
COMPANION_OAUTH_ISSUER=https://your-authorization-provider/
COMPANION_OAUTH_AUDIENCE=https://your-service.example/mcp
COMPANION_OAUTH_JWKS_URL=https://your-authorization-provider/jwks
COMPANION_OAUTH_SUBJECT=the-single-authorized-learner-subject
COMPANION_COLLECTOR_TOKEN=a-separate-random-secret-at-least-32-characters
```

Launch `python -m companion.mcp_server --transport streamable-http --host 0.0.0.0`
behind an HTTPS reverse proxy, or build `Dockerfile.companion`. Supply secrets via
your runtime's private configuration. The container copies only necessary code,
runs as a non-root user and needs a persistent `/data` volume for SQLite.
No hosting provider is required. No container build or remote deployment has been
performed here. Do not expose an unprotected local stdio session over HTTP.

The OAuth provider must support the ChatGPT client-registration method you choose,
authorization-code flow with PKCE, resource audience, and `study:read`/`study:write`
scopes. This project validates tokens and publishes resource metadata; it does not
implement its own login/authorization server. Follow the current
[OpenAI authentication documentation](https://developers.openai.com/plugins/build/auth)
and [custom MCP setup](https://developers.openai.com/api/docs/guides/custom-mcp-server).
The collector's separate bearer secret is for `/sync/*`, not ChatGPT authentication.

Set `COMPANION_SYNC_URL` and the matching collector token locally, then run
`python -m companion sync`. Upload batches are idempotent and download a bounded
cursor stream into a separate local hosted-change ledger. The central service is
the authority for merged reports; the local downloaded mirror does not overwrite
local source records or automatically merge into offline report projections.
Retries have backoff and stop after three upload attempts; inspect the outbox and
resolve failures before resetting an attempt counter administratively.
Use `python -m companion retry-sync` to reset pending retries after fixing the
transport problem. Quarantined source conflicts require source review, not retries.

## Capability gate and schedules

Use synthetic data to verify interactive read/write, scheduled read/write, reply
writeback, notifications and task capacity on the intended account. Record an actual
test with `record-capability NAME verified --evidence "Observed result and date"`.
Do not mark tests verified based on unit tests or documentation alone.
Only then enable `schedules_enabled`. Native ChatGPT tasks own notifications;
the backend only prepares due work. The code does not install native tasks.
If scheduled writes are unavailable but interactive writeback and scheduled reads
are verified, select `scheduled_delivery_mode: backend_prepared_reads`. Run
`python -m companion prepare-due` on the always-on host using its own timer. The
native ChatGPT task reads `study_list_ready_outputs`; this backend timer prepares
data and never sends a second notification.

The proposed report times are 08:00 daily, Monday 08:15 weekly, and the first of
the month at 08:30. Lesson slots come from preferences. Due processing skips
nonexistent DST slots and runs repeated local slots once. It has bounded catch-up,
leases, three attempts and explicit delivery states. External UI delivery is not
guaranteed exactly once. See [scheduled-task documentation](https://learn.chatgpt.com/docs/automations)
and the versioned prompts in `prompts/`.

## Retention, recovery and deletion

Keep meaningful source revisions, committed responses, reports and decisions until
the instance owner deletes the history. Backups contain private data and answer
keys. Use encrypted storage/volumes and your hosting provider's backup encryption.

```powershell
.venv\Scripts\python.exe -m companion backup private/companion-backup.json
.venv\Scripts\python.exe -m companion --db private/restored.sqlite restore private/companion-backup.json
```

Restore requires an empty database and matching learner identity. Automated tests
restore counts, sessions, original reports, provenance and retry acknowledgements.
For full deletion, stop writers, remove this single-learner instance's database,
local copies, context exports and backups, and remove the hosted instance if used.
Provider backups may remain until the configured retention period expires; set and
document that period at deployment. There is intentionally no model-facing SQL,
arbitrary URL fetch, shell or bulk-delete tool.

## Current limits

Automated tests exercise local reporting, lessons and delayed checks with synthetic
data. Hosted deployments, PostgreSQL restoration, live notifications and learning
outcomes still need real-world verification. The reports omit active study time,
commitments, review latency and scored reasoning because the current inputs do
not measure them.

Source retakes using a new activity ID remain distinct; a changed occurrence time
on the same activity/question is quarantined. A provider attempt/session ID beyond
the observed activity ID has not been verified, so ambiguous reused-activity retakes
remain a documented source limitation. No unseen account records become omissions.

One learner's selected lesson completions cannot establish a causal MCAT point
gain. Challenge intent is never calibrated item difficulty. A report is evidence
for the next learning decision, not a score forecast.
