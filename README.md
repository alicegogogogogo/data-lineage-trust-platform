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

Quality rules validate rows of data against a schema version. Rules (and their
enabled/disabled state) are persisted and survive restarts.

- `POST /datasets/{dataset}/versions/{version}/quality-rules` — create a rule.
  Body: `{"name": "id-present", "kind": "not_null", "parameters": {"field": "id"}}`.
  Returns `201` with `id`, `name`, `kind`, `parameters`, `enabled` (defaults to
  `true`) and `created_at`. Rule names are unique within a version (`409`).
  Supported kinds:
  - `not_null` — parameters `{"field": "<existing field>"}`.
  - `numeric_range` — parameters `{"field": "<existing field>", "min": 0, "max": 100}`;
    `min`/`max` must be finite numbers with `min <= max`.
  - `unique` — parameters `{"fields": ["a", "b"]}`, a non-empty list of
    distinct, existing field names.

  Unknown dataset, version or referenced field → `404`; any other semantic
  problem (empty name, unknown kind, bad parameters) → `422`, and nothing is
  written.
- `GET /datasets/{dataset}/versions/{version}/quality-rules` — list rules for
  the version, sorted by `id`.
- `PATCH /datasets/{dataset}/versions/{version}/quality-rules/{rule_id}` —
  body `{"enabled": false}`; only the boolean `enabled` field is accepted.
  Returns the updated rule. Unknown rule → `404`.
- `POST /datasets/{dataset}/versions/{version}/quality-rules/evaluate` — body
  `{"rows": [{...}, {...}]}`. Only enabled rules run. Returns `dataset`,
  `version` and `results` sorted by `rule_id`; each result has `rule_id`,
  `name`, `passed` and `violations` (ascending, zero-based row indices). An
  empty `rows` array passes every rule.
  - `not_null` fails on missing or `null` values.
  - `numeric_range` fails on missing, `null`, non-numeric, boolean or
    out-of-range values.
  - `unique` compares the combination of the named fields; missing fields
    count as `null`.
