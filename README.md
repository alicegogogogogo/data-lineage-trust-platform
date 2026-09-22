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
- `GET /datasets/{dataset}/versions/{version}/processing-tasks/audit-report` —
  read-only audit report for the whole version. Takes no parameters: the
  request body must be absent or `{}`, query parameters are rejected and a
  malformed/non-empty body is `422` with nothing written; unknown
  dataset/version → `404`. Returns
  `{"dataset", "version", "summary", "tasks"}`. `summary` contains
  `task_count`, `run_count`, `pending_tasks`, `running_tasks`,
  `succeeded_tasks`, `failed_tasks`, `exhausted_tasks` (failed tasks that have
  used every allowed attempt) and `invalid_audit_runs` (runs whose audit chain
  fails re-verification); the totals are accumulated from the same rows as the
  detail. `tasks` is sorted by task id ascending and each task carries every
  processing-task field plus `runs`; `runs` is sorted by `attempt` ascending,
  carries every run field plus `proof`. `proof` re-verifies that run's chain
  in the same way as the `/verify` endpoint and is
  `{"valid", "checked_count", "last_evidence_hash"}`, where
  `last_evidence_hash` is the stored hash of the final record (`null` for an
  empty chain, which reports `proof: null`); a failed verification keeps all
  detail and simply reports `"valid": false`. The report is recomputed from
  persisted state on every read, so repeated reads and reads after a restart
  return identical results.

`evidence_hash` is the hexadecimal SHA-256 of a canonical JSON document built
from every stored field except `id`, `created_at` and `evidence_hash` itself
(`event`, `input_summary`, `result_summary`, `sequence`, `run_status`,
`previous_hash`): keys are sorted by Unicode code point, no insignificant
whitespace is emitted and the text is UTF-8 encoded.
