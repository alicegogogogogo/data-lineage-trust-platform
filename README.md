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
