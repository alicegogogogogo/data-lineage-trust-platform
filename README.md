# Data Lineage Trust Platform

Backend service for a data-lineage and trustworthy-computing platform. It manages
datasets, their immutable schema versions, and field-level lineage edges between
schema versions. All state is persisted in SQLite, so data survives restarts.

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

The SQLite database file defaults to `./lineage.db`; override it with the
`DLTP_DATABASE_PATH` environment variable.

## Public API

- `GET /health` returns `{"status":"ok"}` when the service is ready.

### Datasets

- `POST /datasets` — body `{"name": "...", "description": "..."?}`. Returns
  `201` with `{"id", "name", "description", "created_at"}`. Empty names get
  `400`; duplicate names get `409`.
- `GET /datasets` — list all datasets ordered by name.
- `GET /datasets/{name}` — one dataset, `404` if unknown.

### Schema versions

- `POST /datasets/{name}/versions` — body
  `{"fields": [{"name", "type", "nullable"}, ...]}` (non-empty, unique field
  names). Versions are immutable and numbered from 1 upwards per dataset.
  Returns `201` with `{"dataset", "version", "created_at", "fields"}`.
  Unknown dataset → `404`; empty/duplicate/incomplete fields → `400`.
- `GET /datasets/{name}/versions` — all versions with their fields.
- `GET /datasets/{name}/versions/{version}` — one version with its fields,
  `404` if the dataset or version does not exist.

### Field-level lineage

- `POST /lineage` — body `{"target_dataset", "target_version", "target_field",
  "source_dataset", "source_version", "source_field"}`. Both endpoints must
  already exist (`404` otherwise), source and target must not be identical
  (`400`), and resubmitting the same full mapping is rejected (`409`).
  Returns `201` with the mapping plus `{"id", "created_at"}`.
- `GET /lineage/{dataset}/{version}` — all fields of that target version with
  their source references: `{"target_dataset", "target_version", "fields":
  [{"field", "sources": [{"dataset", "version", "field"}]}]}`, sorted by
  target field name, source dataset name, source version, and source field
  name. `404` if the dataset or version does not exist.

All error responses are JSON of the form `{"detail": "..."}` and never expose
SQL or stack traces.
