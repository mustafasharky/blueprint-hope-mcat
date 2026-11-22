# Normalized MCAT data contract, version 1

This is the JSON format accepted by `study_sync.py`. It is separate from Blueprint
exports and browser captures. See [the synthetic example](examples/normalized_snapshot.json)
for a complete input.

## Input snapshot

Every snapshot requires these fields, with optional `capture_metadata` described below:

| Field | Meaning |
|---|---|
| `schema_version` | Integer `1` |
| `source` | `blueprint_mcat` |
| `learner_id` | Stable opaque learner identifier. Avoid email/name. One database per learner. |
| `captured_at` | When fetching this capture finished, timezone-aware ISO 8601. Never replace it with import time. For multiple responses, consult their individual times in `capture_metadata`. |
| `coverage` | Status of attempts, assessments, activities: `complete`, `partial`, or `unavailable`. |
| `attempts` | Question-level results, including repeated attempts as separate records. |
| `assessments` | Exam/section score records. |
| `activities` | Module or other progress records. Completion is not mastery. |

`complete` means the adapter traversed every page within its documented account scope.
Until that is verified use `partial`. `unavailable` requires an empty collection.
Even complete snapshots do not delete historical records. Empty and unavailable
are different.

Every record needs a stable `id`, `occurred_at`, and `updated_at`.
Require occurred_at <= updated_at <= captured_at. Times must include a timezone.
Keep IDs stable across imports. Retakes need distinct attempt IDs even when the
question is the same. Use source revision timestamps when available; do not update
them simply because a record was captured again.

The optional `capture_metadata` object preserves report capture provenance:

- `adapter`: `blueprint_report_v1`.
- For `scope: one_completed_activity`, `response_captured_at` maps `score` and
  `identity` to timezone-aware timestamps, or null when the original fetch time is unknown.
- For `scope: selected_completed_activities`, `reports` is a nonempty list of
  `{activity_id, captured_at, response_captured_at}` objects, one per activity.

Response times cannot follow that report's capture time, and report times cannot
follow the combined capture time. These details are retained in exported `sync_history`.
Capture completion is not proof that every response was fetched at the same instant.

## Attempts

Required: `question_id`, `section`, `topic`, `skill`, `correct`, plus common fields.

- `section`: CP, CARS, BB, PS.
- `topic`: source label; do not invent fine-grained classifications from an aggregate score.
- `skill`: source reasoning skill label, or null if absent.
- `correct`: boolean; null means unanswered. Null is excluded from accuracy's denominator.
- Optional: `assessment_id`, `seconds`, `answer_change`, `bookmarked`, `mode`, `topics`.
- `topics`: unique nonempty source labels for a question with one or more tags.
  When present, `topic` is their sorted labels joined with ` | `. The record remains
  one attempt; each topic summary includes it, so topic counts overlap and must not be summed.
- `answer_change`: none, right_to_wrong, wrong_to_right, wrong_to_wrong, or null.
- `mode`: a source label such as timed/untimed, or null.
- No question/passage text, explanations, cookies, tokens, or login responses are accepted.

The core retains attempt IDs and question IDs and reports unique-question counts;
it does not claim to identify the first-ever exposure from an incomplete history.

The completed-report adapter combines source activity ID and question ID for attempt
identity. It uses question `startTime` for `occurred_at` and `lastSeen` for `updated_at`
because question `endTime` may be null. `lastSeen` is not a verified revision clock:
conflicting values at the same timestamp stop import rather than silently replacing
history. Original timestamp strings remain in private report captures; SQLite uses
UTC microsecond precision. `timeSpent` is in seconds and can include passage time.

## Assessments and activities

Assessments: `name`, `scores` (map of CP/CARS/BB/PS/total to numeric scores or null),
optional `mode`, plus common fields. Preserve source scores; never convert a Qbank
percent-correct to an estimated official MCAT score.

Activities: `name`, `type`, `status`, optional `topic`, plus common fields.
An activity can describe module completion. Raw aggregate topic statistics, full
study plans, target scores, and flashcard ratings are not supported input fields.
Store learner goals and constraints as separate learning observations.

## Learning observations

An observation is separately authored, not overwritten by Blueprint imports:
`id`, `occurred_at`, `topic`, `kind`, `text`, `source`, `evidence_ids`.

Kinds: personal_context, misconception, practice_result, review_due.
Sources: learner_report or tutor_observation. Use stable evidence IDs for observed
mistakes; user-reported context may have an empty list. IDs are immutable and retry-safe.
Corrections are new observations referring to the superseded observation.

## Output and ownership

`study_state.json` contains current normalized records, source freshness and coverage,
sync history, local observations, and topic/skill summaries for rolling 24h/7d/30d/all.
Windows use `(as_of - duration, as_of]`. These are rolling windows, not calendar dates.

SQLite preserves prior revisions. The JSON export contains current records,
capture history, and learning observations, and is replaced on each export. Keep
separate tutor notes outside this generated file so later exports do not erase
them. Avoid overlapping writes to the same snapshot.

## Save a synthetic export

Run these commands from the repository root to build a separate demo database and
save its JSON report. The fixed reporting time keeps the example's rolling windows
reproducible.

```sh
python study_sync.py --db private/demo.sqlite import examples/normalized_snapshot.json
python study_sync.py --db private/demo.sqlite observe examples/observation.json
python study_sync.py --db private/demo.sqlite export --out private/demo_state.json --as-of 2026-10-22T20:00:00Z
```
