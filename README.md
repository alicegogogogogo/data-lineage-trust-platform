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
