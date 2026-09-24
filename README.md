# Data Lineage Trust Platform

Backend service for managing datasets, their immutable schema versions and
field-level lineage between schema versions. All metadata is persisted in a
SQLite database, so data created before a restart remains queryable afterwards.

## Requirements

- Python 3.12+

## Install and test

```bash
python -m pip install -e '.[dev]'
python -m pytest
```

## Start

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The SQLite database is stored at `data/lineage.db` by default. Override the
location with the `DATA_LINEAGE_DB` environment variable.

## Public API

All endpoints return JSON. Error responses use the stable shape
`{"error": "<code>", "detail": "<message>"}` and never expose SQL or stack
traces.

### Health

- `GET /health` returns `{"status":"ok"}` when the service is ready.

### Datasets

- `POST /datasets` — create a dataset. Body: `{"name": "orders", "description": "optional"}`.
  Returns `201` with `id`, `name`, `description`, `created_at`. Empty name →
  `422`; duplicate name → `409`.
- `GET /datasets` — list all datasets.

### Schema versions (immutable, numbered from 1 per dataset)

- `POST /datasets/{dataset}/versions` — create the next schema version. Body:
  `{"fields": [{"name": "id", "type": "integer", "nullable": false}]}`.
  Field list must be non-empty, field `name`/`type`/`nullable` are required and
  field names must be unique within the version. Unknown dataset → `404`;
  invalid fields → `422` (nothing is written).
- `GET /datasets/{dataset}/versions` — list all versions of a dataset with
  their fields.
- `GET /datasets/{dataset}/versions/{version}` — read one version with its
  fields. Unknown dataset/version → `404`.

### Field-level lineage

A lineage link maps one field of a target schema version to one field of a
schema version in a different (source) dataset.

- `POST /datasets/{dataset}/versions/{version}/lineage` — register a mapping.
  Body names both ends:

  ```json
  {
    "target_dataset": "dm_orders",
    "target_version": 1,
    "target_field": "id",
    "source_dataset": "raw_orders",
    "source_version": 1,
    "source_field": "order_id"
  }
  ```

  Target must match the path. Source and target datasets must differ and every
  referenced dataset/version/field must exist (`404` / `422` otherwise).
  Submitting the same complete mapping twice returns `409`.
- `GET /datasets/{dataset}/versions/{version}/lineage` — return every target
  field (including fields without sources) together with its source references.
  Results are sorted by target field name, then source dataset name, source
  version and source field name.
- `GET /datasets/{dataset}/versions/{version}/lineage/impact?field=<field>` —
  return the downstream impact of one source field. The path and the `field`
  query parameter together name an existing field; the response is
  `{"source": {...}, "impacted": [...]}` where `impacted` lists every field
  reachable from the source along lineage mappings (direct and indirect), each
  entry being `{"dataset", "version", "field"}`. Results are deduplicated,
  never contain the source itself (cycles terminate) and are sorted by dataset,
  version and field ascending. Unknown dataset/version/field → `404`; a
  missing, blank or invalid `field` parameter → `422`.

  Impact results are cached persistently (they survive restarts) and every
  read reflects the currently committed lineage graph: creating a schema
  version invalidates the cache entries related to that dataset, and creating
  a lineage mapping invalidates the cached impacts of the mapping's source
  field and of every field that can reach it. Unrelated cache entries are
  preserved.

### Quality rules

Quality rules validate rows against a schema version. Rules are version-scoped
and persisted (including their enabled state) across restarts.

- `POST /datasets/{dataset}/versions/{version}/quality-rules` — create a rule.
  Body: `{"name": "id required", "kind": "not_null", "params": {...}}`. Returns
  `201` with `id`, `name`, `kind`, `params`, `enabled`, `created_at`; `enabled`
  defaults to `true`. Rule names must be unique within a version (`409` on
  conflict). Referenced dataset/version/field must exist (`404`); other invalid
  input returns `422` and nothing is written. Supported kinds:
  - `not_null` — params `{"field": "<existing field>"}`.
  - `numeric_range` — params `{"field": "<existing field>", "min": <finite
    number>, "max": <finite number>}` with `min <= max`.
  - `unique` — params `{"fields": ["<existing field>", ...]}`: a non-empty list
    of distinct, existing field names.
- `GET /datasets/{dataset}/versions/{version}/quality-rules` — list rules
  sorted by `id` ascending.
- `PATCH /datasets/{dataset}/versions/{version}/quality-rules/{rule_id}` —
  enable or disable a rule. Body: `{"enabled": true}` (boolean only); returns
  the updated rule. Unknown rule/dataset/version → `404`.
- `POST /datasets/{dataset}/versions/{version}/quality-rules/evaluate` —
  evaluate rows. Body: `{"rows": [{...}, ...]}`. Only enabled rules run.
  Returns `{"dataset", "version", "results"}` with results sorted by `rule_id`;
  each result has `rule_id`, `name`, `passed` and `violations` (0-based row
  indices in ascending order).
  - `not_null` fails on missing or `null` values.
  - `numeric_range` fails on missing, `null`, non-numeric, boolean or
    out-of-range values (boundaries included).
  - `unique` compares the tuple of `fields` values per row; a missing field
    counts as `null`, and every row taking part in a duplicate group is a
    violation.
  - With an empty `rows` list every rule passes.
- `GET /datasets/{dataset}/versions/{version}/quality-rules/evaluations` —
  list the persisted evaluation history of the version, ordered by `sequence`
  (occurrence order). Every successful evaluation appends one immutable
  summary (`sequence`, `dataset`, `version`, `row_count`,
  `violation_row_count` — the number of distinct submitted rows violating at
  least one rule — `results` and `created_at`); rejected or failed
  evaluations leave no record, and an empty `rows` submission is recorded
  with zero violations. The endpoint takes no request body and no query
  parameters (`422`); unknown dataset/version → `404`.
- `GET /datasets/{dataset}/versions/{version}/quality-rules/evaluations/diff`
  — read-only diff between the two most recent recorded evaluations
  (`from_sequence` → `to_sequence`). `added_violation_rows` /
  `removed_violation_rows` are the 0-based row indices that started or
  stopped violating any rule, and each entry of `rules` carries the rule's
  `before`/`after` side (`violation_count` and `violations`), the per-rule
  `added_violations` / `removed_violations` and the numeric
  `violation_count_delta` (negative, zero or positive). A rule missing from
  one side (disabled or created between the two evaluations) has a `null`
  side and `null` row-level diff fields. With fewer than two recorded
  evaluations the response is an explicit empty result (null sequences,
  empty lists), not an error. Same `404`/`422` rules as the history
  endpoint; nothing is written.

### Quality anomaly detection

Anomaly detection runs over the persisted evaluation history of a schema
version. A version carries at most one detection config; scans append
immutable anomaly records that persist across restarts.

- `POST /datasets/{dataset}/versions/{version}/quality-rules/anomaly-detection`
  — register the version's detection config. The body contains exactly three
  integers: `consecutive_worsening_steps` (at least `2`),
  `violation_row_limit` and `rule_violation_limit` (both non-negative).
  Returns `201` with the config (`id`, `dataset`, `version`, the three
  thresholds, `created_at`). A second config for the same version returns
  `409`; unknown dataset/version → `404`; missing, extra, non-integer or
  out-of-range fields → `422` and nothing is written.
- `GET /datasets/{dataset}/versions/{version}/quality-rules/anomaly-detection`
  — return the registered config (`404` when none exists). The endpoint takes
  no request body and no query parameters (`422`).
- `POST /datasets/{dataset}/versions/{version}/quality-rules/anomaly-detection/scan`
  — run one detection pass over the persisted evaluation history and return
  the anomaly records this scan newly added (ordered by `id`); they are
  persisted in the same transaction. Scanning without a registered config
  returns `409`; an empty history is a successful scan that writes nothing.
  The endpoint takes no request body and no query parameters (`422`). Each
  record has `id`, `kind`, `sequence` (the history sequence it points to),
  `rule_id`, `violation_count` and `created_at`; `kind` is one of:
  - `row_limit` — the evaluation's `violation_row_count` exceeds
    `violation_row_limit` (`rule_id` is `null`).
  - `rule_limit` — one rule's violation count in the evaluation exceeds
    `rule_violation_limit`; `rule_id` carries the rule's id.
  - `trend` — the violation row counts of adjacent evaluations strictly
    increase and the consecutive increase count at the tail of the history
    reaches `consecutive_worsening_steps`; the record points to the final
    evaluation of the increasing sequence (`rule_id` is `null`). A shorter
    run or any decline produces no trend record.

  A record with the same kind, history sequence and rule id is never
  duplicated: repeated scans only persist and return newly appearing
  anomalies, and concurrent scans store each anomaly exactly once with
  contiguous ids.
- `GET /datasets/{dataset}/versions/{version}/quality-rules/anomaly-detection/anomalies`
  — list every anomaly record of the version sorted by `id` ascending (empty
  when there are none, never an error). Same `404`/`422` rules as the scan
  endpoint; nothing is written.

### Privacy policies

Privacy policies attach a sensitivity classification and masking strategy to a
single field of a schema version, enabling role-based de-identification of row
data. Policies (including their enabled state) are persisted across restarts.

- `POST /datasets/{dataset}/versions/{version}/privacy-policies` — register a
  policy. Body:
  `{"field": "email", "classification": "PII", "masking": "partial", "allowed_roles": ["analyst"]}`.
  `field` must name an existing field of the version; `classification` is a
  non-empty string; `masking` is `redact` or `partial`; `allowed_roles` is an
  array of distinct, non-empty role names and may be empty. Returns `201` with
  the submitted fields plus `id`, `enabled` (defaults to `true`) and
  `created_at`. Registering two policies for the same field within one version
  returns `409`. Unknown dataset/version/field → `404`; other invalid input →
  `422` and nothing is written.
- `GET /datasets/{dataset}/versions/{version}/privacy-policies` — list policies
  sorted by `id` ascending.
- `PATCH /datasets/{dataset}/versions/{version}/privacy-policies/{policy_id}` —
  enable or disable a policy. Body: `{"enabled": true}` (boolean only); returns
  the updated policy. Unknown policy/dataset/version → `404`.
- `POST /datasets/{dataset}/versions/{version}/privacy-policies/view` — return a
  role-scoped, order-preserving copy of submitted rows. Body:
  `{"role": "guest", "rows": [{...}, ...]}` with a non-empty `role` and a list
  of row objects. Response: `{"dataset", "version", "rows"}`. Only enabled
  policies whose field appears in a row mask the value when `role` is not in
  `allowed_roles`:
  - `redact` replaces every non-`null` value with `"***"`.
  - `partial` keeps the first character and last two characters of strings
    longer than 4 characters (e.g. `"alice@example.com"` → `"aom"`); every other
    non-`null` value (short strings, numbers, booleans, …) is replaced with
    `"***"`.
  - `null` values and fields without a policy are returned unchanged; rows
    missing a covered field are left without it.

### Sensitive field identification

Beyond the registered privacy policies, the service can annotate schema
fields as *candidates* for sensitive data. Identifications are advisory only:
they never create or change a privacy policy. They are persisted across
restarts.

- `POST /datasets/{dataset}/versions/{version}/sensitive-identifications` —
  identify one field. Body contains exactly `field` and `samples`:
  `{"field": "user_email", "samples": ["alice@example.com"]}`. `field` must be
  a non-blank string naming an existing field of the version; `samples` is an
  array of scalar values (string, number, boolean or null; objects and arrays
  are rejected) and may be empty. The response is the identification record
  (see below). The first submission for a field returns `201`; re-running the
  same field refreshes its record in place and returns `200`, keeping its
  number. A record is generated even when nothing is matched. Unknown
  dataset/version → `404`; a field that does not exist → `404`; missing or
  extra body fields, a non-string or blank `field`, a non-scalar-array
  `samples` (including object/array items), or any query parameter → `422`
  and nothing is written.
- `GET /datasets/{dataset}/versions/{version}/sensitive-identifications` —
  list every identification record of the version sorted by number ascending.
  The endpoint takes no request body and no query parameters (`422`); unknown
  dataset/version → `404`.

Each record has `sequence` (the per-version number from `1`), `dataset`,
`version`, `field`, `evidence`, `confidence`, `source` and `created_at`.
`source` references the identified schema field as
`{"dataset", "version", "field"}`. `evidence` lists the hits, name hits first
and then sample hits; each entry is
`{"kind": "name"|"sample", "category": <word>, "value": <sample or null>}`.

Detection rules:

- The sensitive words are `email`, `phone`, `id_card`, `password`, `token` and
  `birth`. A case-insensitive substring match against the field name is a name
  hit; every contained word contributes one hit, in the word order above.
- Sample matching only inspects string samples and only for fields whose
  declared type is `string` (an email-looking sample of a non-string field is
  ignored — the name is the only signal). An email sample has exactly one `@`
  with non-empty text on both sides and a dot in the domain; a phone sample is
  an 11-digit string starting with `1`. Either shape is a sample hit.
- `confidence` is `high` with both name and sample hits, `medium` for sample
  hits only, `low` for a name hit only and `none` (with empty `evidence`)
  when neither side matched.

### Row snapshots

A row snapshot persistently records the rows of a schema version at one point in
time. Snapshots (including their rows) survive restarts.

- `POST /datasets/{dataset}/versions/{version}/snapshots` — persist a snapshot.
  Body: `{"rows": [{...}, ...]}` where `rows` must be an array of JSON objects
  (it may be empty). The JSON values and the order of rows/object keys are deep
  copied verbatim. Returns `201` with `id`, `dataset`, `version`,
  `created_at` and `row_count` (no rows). Unknown dataset/version → `404`;
  malformed or non-object rows → `422` and nothing is written.
- `GET /datasets/{dataset}/versions/{version}/snapshots` — list snapshot
  metadata sorted by `id` ascending (no `rows`).
- `GET /datasets/{dataset}/versions/{version}/snapshots/{snapshot_id}` — return
  the metadata together with the saved `rows`. Unknown dataset/version/snapshot
  → `404`.
- `GET /datasets/{dataset}/versions/{version}/snapshots/at?timestamp=<ISO-8601>` —
  return the most recent snapshot whose `created_at` is not later than
  `timestamp`, including its rows. The timestamp must be a timezone-aware
  ISO-8601 date-time (an offset or trailing `Z`): missing, invalid or
  timezone-less values return `422`; unknown dataset/version → `404`; when no
  snapshot exists at or before the timestamp the response is `404`.
- `GET /datasets/{dataset}/versions/{version}/snapshots/{snapshot_id}/diff/{other_snapshot_id}` —
  compare two snapshots as JSON-object multisets. Object key order does not
  affect equality, while array order and JSON value types do (e.g. `1`, `1.0`,
  `"1"` and `true` are all distinct), and duplicate rows are counted. Both
  snapshots must belong to the dataset and version named in the path, otherwise
  `422`; unknown dataset/version/snapshot → `404`. The response is
  `{"from_snapshot_id", "to_snapshot_id", "added", "removed"}`; `added` lists
  rows present more often (or only) in the `to` snapshot and `removed` the
  converse, each entry being `{"row": {...}, "count": <int>}` sorted by the
  canonical (key-sorted) JSON text of `row`.

### Retention policies and lineage-aware snapshot deletion

A schema version can carry a single retention policy; snapshot deletion is a
two-stage request/confirm flow that refuses to remove snapshots still feeding
downstream fields. Policies and requests are persisted across restarts.

- `POST /datasets/{dataset}/versions/{version}/retention-policies` — create the
  version's one retention policy. Body: `{"retention_days": 30}` with a
  non-negative integer. Returns `201` with `id`, `dataset`, `version`,
  `retention_days`, `created_at`. A second policy for the same version returns
  `409`; unknown dataset/version → `404`; other invalid input → `422`.
- `POST /datasets/{dataset}/versions/{version}/snapshots/{snapshot_id}/deletion-requests` —
  request deletion. Body contains only a non-empty `reason`. The snapshot and
  the version's retention policy must exist. Returns `201` with `id`,
  `snapshot_id`, `policy_id`, `reason`, `status`, `impacted`, `created_at`.
  `impacted` lists every field directly or indirectly reachable downstream of
  any field of the snapshot's version along lineage mappings; entries are
  deduplicated and sorted by dataset, version and field ascending. The request
  is `blocked` when any downstream field exists and `pending` otherwise. A
  second `pending`/`blocked` request for the same snapshot returns `409`.
- `GET .../snapshots/{snapshot_id}/deletion-requests` — list the snapshot's
  deletion requests sorted by `id` ascending. The collection remains
  addressable after a confirmed request has deleted its snapshot.
- `POST .../snapshots/{snapshot_id}/deletion-requests/{request_id}/confirm` —
  confirm a request (no body). Confirmation succeeds only while the request is
  `pending` and the snapshot is at least `retention_days` old; otherwise `409`
  and nothing is deleted. On success the snapshot is deleted atomically with
  the status change to `confirmed`, and the response adds `confirmed_at`. Once
  deleted, the snapshot no longer appears in snapshot read, list, `at` or diff
  responses.

  Unknown dataset/version/snapshot/policy/request → `404`; other invalid input
  → `422` and nothing is written.

### Processing tasks

A processing task is a named unit of work attached to a schema version; each
run records one attempt. Tasks and runs are persisted across restarts.

- `POST /datasets/{dataset}/versions/{version}/processing-tasks` — create a
  task. Body: `{"name": "extract", "depends_on": [1], "max_attempts": 3}`.
  `name` must be non-empty and unique within the version (`409` on conflict);
  `depends_on` is an array of distinct task ids from the same version (default
  `[]`, unknown ids → `404`); `max_attempts` is a positive integer (default
  `1`). Returns `201` with `id`, `dataset`, `version`, `name`, `depends_on`,
  `max_attempts`, `status` (`pending`), `attempt_count` (`0`) and
  `created_at`. Other invalid input → `422` and nothing is written.
- `GET /datasets/{dataset}/versions/{version}/processing-tasks` — list tasks
  sorted by `id` ascending.
- `GET /datasets/{dataset}/versions/{version}/processing-tasks/{task_id}` —
  return the task together with its `runs`, sorted by `attempt` ascending.
  Each run has `id`, `task_id`, `attempt`, `status`, `started_at`,
  `finished_at` and `error`. Unknown dataset/version/task → `404`.
- `POST /datasets/{dataset}/versions/{version}/processing-tasks/{task_id}/runs` —
  start a new run. Only allowed while the task is `pending` or `failed`, has
  attempts left (`attempt_count < max_attempts`) and every task in
  `depends_on` has status `succeeded`; otherwise `409` and nothing is written.
  Returns `201` with the new `running` run and increments the task's
  `attempt_count` (the task becomes `running`).
- `POST /datasets/{dataset}/versions/{version}/processing-tasks/dispatch` —
  worker batch claim. The body is optional and contains only an optional
  `limit`: omit the body (or send `{}`) to claim one task, or send
  `{"limit": <positive integer>}`. Within a single transaction, up to `limit`
  startable tasks are selected by task id ascending; a task is startable when
  it is `pending`, or `failed` with attempts left, and every direct dependency
  is `succeeded` at selection time. Each selected task gets a new `running`
  run for its next attempt, `attempt_count` is incremented and the task becomes
  `running`. `running`, `succeeded`, attempt-exhausted and dependency-blocked
  tasks are skipped; a task started earlier in the same request is only
  `running`, so its dependents are not selectable until a later request.
  Returns `201` with `{"dataset", "version", "runs"}`; `runs` is sorted by
  task id ascending and each run has the same fields as the single-task start
  endpoint. When no task is startable the response is still `201` with an
  empty `runs` list. Concurrent dispatches (and dispatches interleaved with
  single-task starts) never create a duplicate attempt or two running runs for
  the same task, and a single response never exceeds `limit`. Unknown
  dataset/version → `404`; extra body fields, a non-integer (including
  boolean) or non-positive `limit` → `422` and nothing is written.
- `PATCH /datasets/{dataset}/versions/{version}/processing-tasks/{task_id}/runs/{run_id}` —
  finish the currently running run. Body: `{"status": "succeeded"}` or
  `{"status": "failed", "error": "<non-empty message>"}`. Writes `finished_at`
  and moves the task to the same status; a `failed` task with attempts left
  can be started again. Finishing an already finished run → `409`; a run that
  does not belong to the task/version in the path → `422`; unknown
  dataset/version/task/run → `404`; other invalid input → `422` and nothing
  is written.
- `POST /datasets/{dataset}/versions/{version}/processing-tasks/batch-complete` —
  finish several currently running runs in one transaction. Body contains only
  a non-empty `runs` array; each item names a task of the path version and one
  of its runs: `{"task_id": <int>, "run_id": <int>, "status": "succeeded"}` or
  the same with `"status": "failed"` and an `error` that is non-empty after
  trimming whitespace (`succeeded` items carry no `error`). The same task must
  not appear twice. The whole batch is validated before any write and either
  every item completes or none do; on success the response is
  `{"dataset", "version", "runs"}` with the completed runs sorted by task id
  ascending, each carrying the same fields as the single-run finish response.
  Every run gets a `finished_at` timestamp and its task moves atomically to
  the same status; `failed` tasks keep their consumed attempts and stay
  retryable while attempts remain. Tasks whose dependencies succeed in the
  same batch are not started or skipped within that batch (the dependencies
  are `running` at selection time, as with dispatch) and become startable
  immediately afterwards, each at its next continuous attempt. Empty/missing
  `runs`, extra fields, non-integer (including boolean) ids, an unknown status
  or a missing/blank/non-string `error` → `422`; an empty body or malformed
  JSON → `422`; any query parameter → `422`. Unknown dataset/version → `404`;
  a batch task that does not exist or belongs to another dataset/version →
  `404`; an unknown run → `404` (a run belonging to another dataset/version is
  treated as out of scope); a run that belongs to another task of the same
  version → `422`. An already finished run, a duplicate task in the batch or a
  task that is not currently `running` (not currently completable) → `409`,
  and the entire batch is rolled back: statuses, timestamps and counters are
  unchanged. Concurrent batch completion is single-winner against the
  single-run finish/cancel endpoints, run starts and batch dispatch: exactly
  one racing operation commits and the others receive `409`, never leaving a
  `running` run with a half-written `finished_at` or rolling `attempt_count`
  back. Audit records appended afterwards record the run's terminal status,
  and the schedule, audit report and post-restart reads present the batch
  results through the existing fields and ordering.
- `POST /datasets/{dataset}/versions/{version}/processing-tasks/{task_id}/runs/{run_id}/cancel` —
  cancel the currently running run. The body contains only a non-empty
  `reason` string; leading and trailing whitespace is trimmed and the result
  is stored as the run's `error`. Returns the updated run with `finished_at`
  written and `status` `failed`; the task is atomically moved to `failed` in
  the same transaction while `attempt_count` is not rolled back, so a
  cancelled task follows the existing start rules: it can run again when its
  dependencies have succeeded and attempts remain, and the next run keeps the
  continuous attempt sequence. Only a `running` run can be cancelled;
  cancelling an already `succeeded`/`failed` run returns `409` and changes no
  field. A run that does not belong to the task/version in the path → `422`;
  unknown dataset/version/task/run → `404`; a missing reason, an extra body
  field, a non-string reason or one that is blank after trimming, an empty
  body, malformed JSON or any query parameter → `422` and nothing is
  written. Cancellation is single-winner against finishing, single-task
  starts and batch dispatch: exactly one racing operation performs the state
  transition and the others receive `409`, so no run is left `running` with a
  half-written `finished_at`.
- `PUT /datasets/{dataset}/versions/{version}/processing-tasks/{task_id}/dependencies` —
  atomically replace the task's dependency list. Body contains only
  `{"depends_on": [<task id>, ...]}` (the array may be empty); the ids must
  reference existing tasks of the same version without duplicates and must not
  include the target task itself. The replacement is rejected when the target
  task is not `pending` or when it would introduce a cycle in the dependency
  graph; on success the updated task is returned. Unknown
  dataset/version/task/dependency → `404`; a self dependency, a cycle or a
  non-`pending` target → `409`; any other invalid body → `422` and nothing is
  written. The existing run-start rule uses the updated dependencies.
- `GET /datasets/{dataset}/versions/{version}/processing-tasks/schedule` —
  return `{"dataset", "version", "tasks"}` with tasks sorted by `id` ascending.
  Each task carries every processing-task field plus `schedule_state` and
  `blocking_task_ids`. `schedule_state` is `running` or `succeeded` for tasks
  in those states; a `failed` task is `retryable` while attempts remain and
  `exhausted` once they are used up; a `pending` task is `ready` when all of
  its direct dependencies have succeeded, `upstream_failed` when any
  dependency chain contains an `exhausted` or `upstream_failed` task, and
  `blocked` otherwise. `blocking_task_ids` lists the direct dependencies that
  have not succeeded, sorted ascending; it is empty for non-pending tasks.
  States are recomputed on every read (including after a successful retry)
  and both the dependency graph and the schedule survive restarts.
- `GET /datasets/{dataset}/versions/{version}/processing-tasks/audit-report` —
  read-only audit report for the whole version; the endpoint takes no request
  body and no query parameters (both are rejected with `422`; unknown
  dataset/version → `404`). Returns
  `{"dataset", "version", "summary", "tasks"}`. `summary` contains
  `task_count`, `run_count`, `pending_tasks`, `running_tasks`,
  `succeeded_tasks`, `failed_tasks`, `exhausted_tasks` and
  `invalid_audit_runs`; status counters count tasks by their stored status,
  `exhausted_tasks` counts `failed` tasks that have used up
  `max_attempts`, and `invalid_audit_runs` counts runs whose audit chain fails
  re-verification. `tasks` is sorted by task `id` and each task carries every
  processing-task field plus `runs`; `runs` is sorted by `attempt` and each
  run carries its run fields plus `proof`. `proof` re-runs the same checks as
  the per-run verify endpoint over that run's audit chain and is
  `{"valid", "checked_count", "last_evidence_hash"}`; `last_evidence_hash` is
  the stored evidence hash of the chain's final record and is `null` only for
  an empty chain — it is still returned when `valid` is `false`, and the run
  details are retained. Summary counters always agree with the returned
  details and the report is deterministic across restarts.

### Processing run audit records (append-only proof chain)

Every run carries an independent, tamper-evident chain of audit records.
Records are append-only (the database rejects updates and deletes, and there
are no per-record HTTP routes), numbered from `1` per run and linked through
SHA-256 evidence hashes. They persist across restarts.

- `POST /datasets/{dataset}/versions/{version}/processing-tasks/{task_id}/runs/{run_id}/audit-records` —
  append a record. Body contains exactly `event`, `input_summary` and
  `result_summary`, each a string that is non-empty after trimming whitespace.
  Returns `201` with the submitted fields plus `id`, `sequence` (continuous
  from `1` within the run), `run_status` (the run's status at write time:
  `running`/`succeeded`/`failed`), `previous_hash` (`null` for the first
  record, otherwise the previous record's `evidence_hash`), `evidence_hash`
  and `created_at`. Missing, extra, non-string or blank fields → `422` and
  nothing is written; unknown dataset/version/task/run → `404`; a run that
  does not belong to the path task (including one in another version) →
  `422`. Concurrent appends never reuse a sequence or break the chain.
- `GET .../runs/{run_id}/audit-records` — list the run's records in ascending
  `sequence` order. Same `404`/`422` path rules.
- `GET .../runs/{run_id}/audit-records/verify` — verify the chain. Returns the
  path identifiers together with `valid` and `checked_count`:
  `{"dataset", "version", "task_id", "run_id", "valid", "checked_count"}`.
  Verification recomputes every `evidence_hash`, checks that `sequence` values
  are continuous from `1` and that each `previous_hash` equals the preceding
  record's `evidence_hash` (the first must be `null`). An intact chain returns
  `"valid": true` (also for an empty chain).

`evidence_hash` is the hexadecimal SHA-256 of a canonical JSON document built
from every stored field except `id`, `created_at` and `evidence_hash` itself
(`event`, `input_summary`, `result_summary`, `sequence`, `run_status`,
`previous_hash`): keys are sorted by Unicode code point, no insignificant
whitespace is emitted and the text is UTF-8 encoded.
