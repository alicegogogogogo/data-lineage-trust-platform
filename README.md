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
- `GET /datasets/{dataset}/versions/{base_version}/compatibility/{target_version}` —
  read-only breaking-change check of the target version against the base
  version, computed fresh from the persisted field definitions on every read
  (nothing is written; field order is not a difference). The endpoint takes no
  request body and no query parameters (`422`); unknown dataset/version →
  `404`, with the same 404-before-422 precedence as the version diff. The JSON
  document is deterministic (fixed key order, compact whitespace, exactly one
  trailing newline) with top-level keys `base_version`, `target_version`,
  `breaking_changes` and `breaking_change_count`. A breaking change is exactly
  one of: the target removed a base field (`removed`), changed a field's type
  (`type_changed`) or tightened a nullable field to not nullable
  (`nullable_tightened`); added fields and nullable loosening are not
  breaking. Each entry has `field`, `kind`, `before` (base definition) and
  `after` (target definition, `null` for a removed field), entries are sorted
  by field name ascending and `breaking_change_count` equals the number of
  entries. Comparing a version with itself returns an empty list and a zero
  count. The verdict is advisory for release decisions only: it runs no
  migration or rollback.
- `GET /datasets/{dataset}/versions/{base_version}/compatibility/{target_version}/impact` —
  read-only companion of the compatibility check: the same top-level keys
  (`base_version`, `target_version`, `breaking_changes`,
  `breaking_change_count`) and the same breaking entries, each entry carrying
  one extra key `impacted` after `after`. `impacted` lists every field
  directly or indirectly downstream of the broken field along the lineage
  mappings, starting from the base version's same-named field and — when the
  field still exists in the target version — also from the target version's
  field. Each item is `{"dataset", "version", "field"}`; the set is
  deduplicated, never contains a start field itself (cycles terminate) and is
  sorted by dataset, version and field ascending. A field with no downstream
  yields an empty list. The endpoint takes no request body and no query
  parameters (`422`); unknown dataset/version → `404` with the same
  404-before-422 precedence as the compatibility check, and the JSON document
  is deterministic (fixed key order, compact whitespace, exactly one trailing
  newline). The read is fully side-effect free: version definitions, lineage
  mappings and the impact cache are never written.
- `GET /datasets/{dataset}/evolution-summary` — read-only whole-dataset
  summary of adjacent-version breaking changes and their downstream impact,
  computed fresh on every read (no caching; nothing is written, and version
  definitions, lineage mappings and the impact cache are never touched).
  Quality and privacy state play no role. The path carries only the dataset
  name; the request takes no body and no query parameters (`422`); an unknown
  dataset → `404`, with the same 404-before-422 precedence as the
  compatibility reads. A dataset with fewer than two versions returns `200`
  with an empty `pairs` array and all-zero totals, never an error. The JSON
  document is deterministic (fixed key order, compact whitespace, exactly one
  trailing newline) with top-level keys `dataset`, `pairs` and `totals`.
  `pairs` is ordered by base version number ascending and lists every
  adjacent version pair, including pairs without breaking changes; each entry
  has exactly `base_version`, `target_version`, `breaking_count`,
  `impacted_count` and `impacted_datasets` in that order. `breaking_count` is
  the number of breaking entries of the pair, judged exactly as in the
  pairwise compatibility impact response (`removed`, `type_changed` or
  `nullable_tightened`; several changes of one field collapse into a single
  entry). The pair's impacted set is the union of every breaking entry's
  downstream fields — same start-field rule as the pairwise impact response,
  merged and deduplicated, never containing a start field itself (cycles
  terminate) — `impacted_count` is its size and `impacted_datasets` lists the
  distinct dataset names appearing in it, sorted ascending. `totals` has
  exactly `pair_count` (the number of pairs), `breaking_count` and
  `impacted_count`, each the sum of the per-pair values over the whole
  dataset.
- `GET /datasets/{dataset}/fields/{field}/trajectory` — read-only
  cross-version trajectory of one named field of the dataset, computed fresh
  on every read (no caching; nothing is written, and version definitions,
  lineage mappings and the impact cache are never touched). Only the
  persisted version field definitions and lineage mappings are read; quality,
  privacy, snapshot and processing-task state play no role. The path carries
  the dataset name and the field name; the request takes no body and no query
  parameters (`422`); an unknown dataset → `404`, and a field that does not
  appear in any of the dataset's versions → `404`, both with the same
  404-before-422 precedence as the evolution summary. A dataset with fewer
  than two versions returns `200` with an empty `changes` array, never an
  error. The JSON document is deterministic (fixed key order, compact
  whitespace, exactly one trailing newline) with top-level keys `dataset`,
  `field`, `entries` and `changes` in that order. `entries` lists every
  schema version of the dataset ordered by version number ascending; each
  entry has exactly `version` and `definition`, where `definition` is the
  field's persisted definition in that version (`type` and `nullable` only)
  or `null` — the key is never omitted — when the field does not exist
  there. `changes` lists one entry per adjacent version pair, ordered by
  base version ascending; each entry has exactly `base_version`,
  `target_version`, `status`, `impacted` and `impacted_datasets` in that
  order. `status` reuses the breaking-entry vocabulary of the compatibility
  check — `added` (the field appears on the target side), `removed` (it
  disappears on the target side), `type_changed`, `nullable_tightened` —
  extended with `nullable_loosened` (only the nullability relaxes) and
  `unchanged` (both definitions agree, including both absent); a field that
  both changes type and tightens nullability collapses into a single
  `type_changed` status. `impacted` lists every field directly or indirectly
  downstream of the tracked field for that pair along the lineage mappings,
  with the same start-field rule as the pairwise compatibility impact
  response (the base version's same-named field and, when it exists, the
  target version's); each item is `{"dataset", "version", "field"}`, the set
  is deduplicated, never contains a start field itself (cycles terminate),
  is sorted by dataset, version and field ascending and is empty when the
  field has no downstream. `impacted_datasets` lists the distinct dataset
  names appearing in `impacted`, sorted ascending.

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
- `DELETE /datasets/{dataset}/versions/{version}/lineage` — remove one
  registered mapping. Same address and same body shape as registration: the
  six locating fields name the complete mapping to delete and the body target
  must match the path. Every locating value readable in the request is
  resolved first, so an unknown dataset, version or field is a `404` ahead of
  every request-shape error; when all six values name existing resources but
  the mapping itself was never registered, the delete is likewise a `404`.
  An empty or whitespace body, invalid JSON, a non-object payload, missing or
  extra fields, wrongly typed values, blank names, any query parameter, a
  mapping whose source and target datasets coincide or whose body target
  disagrees with the path are all `422`. No rejection changes the lineage
  graph, the impact cache or any other metadata. A registered mapping is
  deleted atomically and returns `200` with the removed mapping in the
  registration response shape; concurrent deletes of the same mapping succeed
  exactly once (the loser gets `404` and changes nothing). The deletion is
  visible to every lineage read immediately and survives restarts; the cached
  impacts of the mapping's source field and of every field that can reach it
  are invalidated exactly as on registration, unrelated entries are kept.
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
  missing, blank, invalid or repeated `field` parameter → `422` (a repeated
  `field` is judged before the field lookup, so it is always a `422`, never a
  field `404`).
- `GET /datasets/{dataset}/versions/{version}/lineage/impact/cache-audit` —
  read-only consistency audit of the version's impact cache, appended one
  segment after the impact query address. The path dataset and version name
  the audit scope; every field of that version is checked against its current
  cache record and recomputed fresh from the committed lineage graph using
  exactly the impact-query semantics (downstream fields merged and
  deduplicated, the start field excluded, cycles terminating). Each entry is
  `{"field", "status"}` with `cached` (the stored record equals the
  recomputed result), `missing` (no cache record because the field's impact
  was never queried — a normal state, not an error) or `mismatch` (the stored
  record differs). Entries are sorted by field name ascending and the sort
  does not depend on database order. The JSON document is deterministic
  (fixed key order, compact whitespace, exactly one trailing newline) with
  top-level keys `dataset`, `version`, `entries` and `counts` in that order;
  `counts` gives `cached_count`, `missing_count` and `mismatch_count`, each
  equal to the number of entries with that status. The audit is computed on
  every read and never writes: no cache record is inserted, invalidated or
  repaired, so impact queries, impact paths and source paths behave exactly
  as before. The endpoint takes no request body and no query parameters; any
  body bytes (whitespace-only included) or query parameter → `422`, and an
  unknown dataset or version → `404`, with `404` taking precedence over
  `422` as in the impact query.
- `POST /datasets/{dataset}/versions/{version}/lineage/impact/cache-audit/repair`
  — controlled repair of the version's impact cache, appended one segment
  after the read-only audit address and accepting POST only. Every field of
  the version is recomputed against the committed lineage graph with the
  exact impact-query semantics: a field without a cache record gets one
  (`created`), a stored record that disagrees is rewritten (`updated`) and a
  matching record is left untouched (`unchanged`). Entries are sorted by
  field name ascending and the JSON document is deterministic (fixed key
  order, compact whitespace, exactly one trailing newline) with top-level
  keys `dataset`, `version`, `entries` and `counts` in that order; each
  entry has exactly `field` and `action`, and `counts` gives
  `created_count`, `updated_count` and `unchanged_count`. Written records
  are ordinary cache rows visible immediately to impact queries and
  surviving restarts; a subsequent audit reports every field as `cached`.
  The endpoint takes no request body and no query parameters — any body
  bytes (whitespace-only included) or query parameter → `422`, an unknown
  dataset or version → `404` (with `404` taking precedence as in the audit),
  and concurrent repairs of the same version have a single winner, the
  loser receiving `409` and changing nothing. Every rejection writes
  nothing; lineage registrations and deletions, impact queries, path
  explanations, source-path reads and the read-only audit are unaffected.
- `GET /datasets/{dataset}/versions/{version}/lineage/impact/cache-invalidations`
  — read-only invalidation trail of the version's impact cache, appended one
  segment after the impact query address and accepting GET only. Whenever a
  lineage mapping is successfully registered or deleted, every invalidated
  field that actually had a cache record (the mapping's source field and
  every field that can reach it) leaves one append-only record under its own
  dataset and version; cache invalidation triggered by schema version
  creation leaves no trail. Each entry has exactly `sequence`, `cause`,
  `field` and `created_at` in that order: `sequence` numbers the version's
  records from 1 in write order (continuous, never reused, even under
  concurrent registrations and deletions), `cause` is `registered` or
  `deleted` after the triggering mapping write and `created_at` is the
  timezone-bearing write time. Entries are sorted by sequence ascending and
  a version without records returns an empty `entries` array, never an
  error. The JSON document is deterministic (fixed key order, compact
  whitespace, exactly one trailing newline) with top-level keys `dataset`,
  `version` and `entries` in that order. The trail is append-only and
  survives restarts; the read only reads persisted records and never writes,
  invalidates or repairs anything. The endpoint takes no request body and no
  query parameters; any body bytes (whitespace-only included) or query
  parameter → `422`, and an unknown dataset or version → `404`, with `404`
  taking precedence over `422` as in the impact query.
- `GET /datasets/{dataset}/versions/{version}/lineage/impact-paths?field=<field>` —
  read-only shortest-path explanation of the same impact, computed fresh on
  every read (no caching; nothing is written, and version definitions, lineage
  mappings and the impact cache are never touched). The path and the `field`
  query parameter together name an existing field. The JSON document is
  deterministic (fixed key order, compact whitespace, exactly one trailing
  newline) with top-level keys `source`, `impacts`, `direct_count` and
  `indirect_count` in that order; `source` is the start field reference
  (`{"dataset", "version", "field"}`). Each `impacts` entry has exactly the
  location keys `dataset`, `version`, `field`, then `path` and `path_length`;
  entries are deduplicated, never contain the source and are sorted by dataset,
  version and field ascending. `path` is the shortest node sequence from the
  source to the field, including both ends, and each node has the same
  `{"dataset", "version", "field"}` shape; `path_length` is the number of
  edges on the path (`1` for a direct downstream field, increasing for
  indirect ones). Among several shortest paths, the lexicographically smallest
  node sequence (compared by dataset, version and field) is chosen; cycles
  terminate. `direct_count` counts fields at distance 1 and `indirect_count`
  the fields farther away, and a field with no downstream returns an empty
  `impacts` array and both counts zero, never an error. The endpoint takes no
  request body and no query parameter other than `field` (a missing, blank or
  repeated `field` is likewise a `422`); unknown dataset/version/field →
  `404`, with `404` taking precedence over every `422` except that a repeated
  `field` is judged before the field lookup and is therefore always a `422`.
- `GET /datasets/{dataset}/versions/{version}/lineage/impact/source-paths?field=<field>` —
  read-only upstream companion of the lineage impact query, computed fresh on
  every read (no caching; nothing is written, and version definitions, lineage
  mappings and the impact cache are never touched). The path and the `field`
  query parameter together name an existing field; the `field` value is
  matched literally (never trimmed), so a whitespace-padded name that matches
  no stored field is a `404`. The JSON document is deterministic (fixed key
  order, compact whitespace, exactly one trailing newline) with top-level keys
  `source`, `origins`, `direct_count`, `indirect_count` and
  `source_dataset_count` in that order; `source` is the start field reference
  (`{"dataset", "version", "field"}`). Each `origins` entry has exactly the
  location keys `dataset`, `version`, `field`, then `path` and `path_length`;
  entries are deduplicated, never contain the start field and are sorted by
  dataset, version and field ascending. An origin is a field whose mapping
  points (directly or transitively) at the start field. `path` is the
  shortest node sequence walking from the start field up to the origin,
  including both ends, and each node has the same
  `{"dataset", "version", "field"}` shape; `path_length` is the number of
  edges on the path (`1` for a direct source, increasing for indirect ones).
  Among several shortest paths, the lexicographically smallest node sequence
  (compared by dataset, version and field) is chosen; cycles terminate.
  `direct_count` counts origins at distance 1, `indirect_count` the origins
  farther away and `source_dataset_count` the distinct dataset names among
  all origins; a field with no sources returns an empty `origins` array and
  all counts zero, never an error. The endpoint takes no request body and no
  query parameter other than `field` (a missing, blank or repeated `field` is
  likewise a `422`); unknown dataset/version/field → `404`, with `404`
  taking precedence over every `422` except that a repeated `field` is judged
  before the field lookup and is therefore always a `422`.
- `GET /datasets/{dataset}/lineage-coverage` — read-only whole-dataset check
  reporting, for every schema version at once, how the version's fields are
  covered by registered lineage mappings. Versions are sorted by version
  number ascending and a dataset without versions returns an empty `versions`
  array, never an error. Each version entry has exactly `version`, `fields`,
  `linked_field_count`, `unlinked_field_count` and `mapping_count` in that
  order; `fields` lists every field of the version sorted by field name, each
  entry having exactly `field`, `sources` and `source_dataset_count`.
  `sources` lists every mapping source registered with the field as its
  target; each reference has the location keys `dataset`, `version` and
  `field` in that order, is deduplicated and sorted by those three keys
  ascending, and the sort never depends on database order. A field without a
  source is listed all the same with an empty `sources` array and a
  `source_dataset_count` of zero; that count counts the distinct source
  dataset names among the references, never the number of mappings.
  `mapping_count` equals the sum of the fields' source-reference counts and
  `linked_field_count` plus `unlinked_field_count` equals the number of
  fields. The JSON document is deterministic (fixed key order, compact
  whitespace, exactly one trailing newline) with top-level keys `dataset`,
  `versions` and `totals` in that order; `totals` gives `version_count`,
  `field_count`, `linked_field_count`, `unlinked_field_count` and
  `mapping_count`, each equal to the sum of the matching per-version values
  (all zero for a dataset without versions). The check is recomputed on
  every read and never writes: lineage mappings and the impact cache are
  never inserted, invalidated, repaired or otherwise touched. The endpoint
  takes no request body and no query parameters; any body bytes
  (whitespace-only included) or query parameter → `422`, and an unknown
  dataset → `404`, with `404` taking precedence over the shape checks.
- `GET /datasets/{dataset}/lineage-source-coverage` — read-only whole-dataset
  source-side companion of the lineage coverage check, reporting, for every
  schema version at once, how the version's fields are registered as the
  source end of lineage mappings. Versions are sorted by version number
  ascending and a dataset without versions returns an empty `versions`
  array, never an error. Each version entry has exactly `version`, `fields`,
  `referenced_field_count`, `unreferenced_field_count` and `mapping_count`
  in that order; `fields` lists every field of the version sorted by field
  name, each entry having exactly `field`, `downstreams` and
  `downstream_dataset_count`. `downstreams` lists every mapping target
  registered with the field as its source; each reference has the location
  keys `dataset`, `version` and `field` in that order, is deduplicated and
  sorted by those three keys ascending, and the sort never depends on
  database order. A field without a downstream is listed all the same with
  an empty `downstreams` array and a `downstream_dataset_count` of zero;
  that count counts the distinct target dataset names among the references,
  never the number of mappings. `mapping_count` equals the sum of the
  fields' downstream-reference counts and `referenced_field_count` plus
  `unreferenced_field_count` equals the number of fields. The JSON document
  is deterministic (fixed key order, compact whitespace, exactly one
  trailing newline) with top-level keys `dataset`, `versions` and `totals`
  in that order; `totals` gives `version_count`, `field_count`,
  `referenced_field_count`, `unreferenced_field_count` and `mapping_count`,
  each equal to the sum of the matching per-version values (all zero for a
  dataset without versions). The check is recomputed on every read and
  never writes: lineage mappings and the impact cache are never inserted,
  invalidated, repaired or otherwise touched, and the target-side lineage
  coverage read behaves exactly as before. The endpoint takes no request
  body and no query parameters; any body bytes (whitespace-only and a
  single space included) or query parameter → `422`, and an unknown
  dataset → `404`, with `404` taking precedence over the shape checks.

  Impact results are cached persistently (they survive restarts) and every
  read reflects the currently committed lineage graph: creating a schema
  version invalidates the cache entries related to that dataset, and creating
  or deleting a lineage mapping invalidates the cached impacts of the
  mapping's source field and of every field that can reach it. Unrelated
  cache entries are preserved.

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
- `GET /datasets/{dataset}/versions/{version}/quality-rules/evaluations/at?timestamp=...`
  — read-only point-in-time single-record look-back, appended one segment
  after the evaluation history address and accepting GET only. `timestamp`
  must be a timezone-bearing ISO-8601 date-time (an offset or trailing `Z`);
  only evaluation summaries written at or before it form the look-back
  window, and the returned summary is the window's highest-sequence record
  in exactly the single-record shape of the history (`sequence`, `dataset`,
  `version`, `row_count`, `violation_row_count`, `results`, `created_at`).
  A window with no recorded evaluation is a normal response, not an error:
  the same key set is still returned with `sequence`, `row_count`,
  `violation_row_count` and `created_at` set to null (keys are never
  omitted) and `results` an empty list. Only persisted evaluation summaries
  are read: rules are never re-run, history summaries are never rewritten,
  nothing is cached or written, and the window grows naturally as new
  evaluations are persisted, so the same data yields the same document byte
  for byte across process restarts, and the ordering of rule results never
  depends on database natural order. The JSON document is deterministic
  (fixed key order, compact whitespace, exactly one trailing newline). The
  path dataset/version resolves first (`404`); afterwards any request body
  bytes (whitespace-only included), a missing, repeated, unparseable or
  timezone-less `timestamp`, or any other query parameter are a `422` that
  writes nothing. Errors keep the stable `{"error", "detail"}` shape without
  exposing SQL, stack traces or internal objects; the evaluation history,
  the pairwise and point-in-time diffs and every other endpoint are
  unchanged.
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
  empty lists), not an error. The path dataset/version resolves first
  (`404`); afterwards any request body bytes (whitespace-only included) or
  any query parameter are a `422` that writes nothing.
- `GET /datasets/{dataset}/versions/{version}/quality-rules/evaluations/diff/at?timestamp=...`
  — read-only point-in-time companion of the evaluation diff, appended one
  segment after the pairwise diff address and accepting GET only.
  `timestamp` must be a timezone-bearing ISO-8601 date-time (an offset or
  trailing `Z`); only evaluation summaries written at or before it count as
  the look-back window, and the two that participate are the window's
  highest-sequence pair — the newest is `to`, its predecessor `from`. The
  comparison is exactly the pairwise diff's: `added_violation_rows` /
  `removed_violation_rows` are the 0-based row indices that started or
  stopped violating any rule, and each `rules` entry carries the rule's
  `before`/`after` side (`violation_count` and `violations`), the per-rule
  `added_violations` / `removed_violations` and the numeric
  `violation_count_delta`. A rule missing from one side (disabled or created
  between the two evaluations) has a `null` side and `null` row-level diff
  fields — the key is never omitted. A window with fewer than two recorded
  evaluations returns the same explicit empty result as the pairwise diff
  (null sequences, empty lists), not an error. Only persisted evaluation
  summaries are read: rules are never re-run, history summaries are never
  rewritten, nothing is cached or written, and the window grows naturally as
  new evaluations are persisted, so the same data yields the same document
  byte for byte across process restarts. The JSON document is deterministic
  (fixed key order, compact whitespace, exactly one trailing newline). The
  path dataset/version resolves first (`404`); afterwards any request body
  bytes (whitespace-only included), a missing, repeated, unparseable or
  timezone-less `timestamp`, or any other query parameter are a `422` that
  writes nothing. Errors keep the stable `{"error", "detail"}` shape without
  exposing SQL, stack traces or internal objects; the evaluation history,
  the pairwise diff and every other endpoint are unchanged.
- `GET /datasets/{dataset}/versions/{version}/quality-rules/evaluations/trend`
  — read-only trend summary, appended one segment after the evaluation
  history address and accepting GET only. The caller sends no request body
  and no query parameter. The summary is computed fresh from the persisted
  evaluation history on every read: rules are never re-run, history
  summaries are never rewritten, nothing is cached or written, and the same
  history yields the same document byte for byte across process restarts,
  with every ordering explicit rather than dependent on database natural
  order.
  - `evaluations` lists one entry per evaluation ordered by ascending
    `sequence`, each carrying the history summary's `created_at` (write
    time), `row_count` (submitted rows) and `violation_row_count`, plus
    `violation_row_count_delta` — the change in violation row count against
    the previous evaluation; on the first evaluation it is `null` and the
    key is never omitted.
  - `rules` gives one row per rule that appears in the history, sorted by
    rule id ascending, including rules that have since been disabled and
    rules that never recorded a violation. Each row carries `rule_id` and
    `name`, the cumulative `violation_row_count` (summed over evaluations),
    the `violating_evaluation_count`, and `first_violation_sequence` /
    `last_violation_sequence` — the sequences of the rule's first and last
    violating evaluation, both `null` (never omitted) when the rule never
    violated; its counts are zero in that case.
  - `totals` reports the whole-version `evaluation_count`, the
    `violation_row_count` total (the sum of the per-evaluation counts) and
    the `rule_count` of distinct rules the history involves. An empty
    history is an explicit empty result (empty `evaluations` and `rules`,
    all-zero totals), returned normally rather than as an error.
  - The JSON document is deterministic (fixed key order, compact
    whitespace, lowercase booleans, exactly one trailing newline). The path
    dataset/version resolves first (`404`); afterwards any request body
    bytes (whitespace-only included) or any query parameter are a `422`
    that writes nothing. Errors keep the stable `{"error", "detail"}`
    shape without exposing SQL, stack traces or internal objects; the
    evaluation history, the pairwise and point-in-time diffs, anomaly
    detection and every other endpoint are unchanged.

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

### Quality gate

The quality gate is the release-readiness verdict of a schema version. It is
computed fresh from the persisted records on every read — nothing is cached,
re-run or written, and the rule definitions are never consulted (violations
recorded for rules since disabled still count).

- `GET /datasets/{dataset}/versions/{version}/quality-rules/gate` — return the
  verdict as a deterministic JSON document (fixed key order, compact
  whitespace, exactly one trailing newline):
  `{"dataset", "version", "verdict", "reasons", "counts"}`.
  - `verdict` is one of:
    - `pass` — the latest recorded evaluation passed every rule and no
      anomaly record exists; a zero-violation evaluation left by an empty
      `rows` submission is a valid basis.
    - `undetermined` — no evaluation was ever recorded; `reasons` is empty
      and the read succeeds (never an error).
    - `fail` — the latest recorded evaluation still has violating rows, or
      the version carries any anomaly record.
  - `reasons` lists every factor that lowers the verdict, sorted by `kind`,
    `sequence` and `rule_id` (null ids first), all ascending. Each reason has
    `kind`, `sequence`, `rule_id` and `violation_count` (a missing rule id is
    `null`, never omitted). Every rule with violations in the latest
    evaluation contributes one `violation` reason with its violation row
    count; every persisted anomaly record contributes one reason of its own
    kind (`row_limit`, `rule_limit` or `trend`). A rule that appears in both
    yields two reasons — they are never merged or deduplicated.
  - `counts` reports how many persisted records the verdict examined:
    `evaluations`, `anomalies` and `reasons` (the length of the reason list).
  - The endpoint takes no request body and no query parameters: any body
    bytes — including whitespace-only ones — or any query parameter are a
    `422`, checked after the path dataset/version resolves (`404` first).
    Nothing is written on any rejection.
- `GET /datasets/{dataset}/versions/{version}/quality-rules/gate/at?timestamp=...`
  — the same verdict as it stood at a requested instant. `timestamp` must be
  a timezone-bearing ISO-8601 date-time; only evaluations and anomaly records
  written at or before it count, and the latest evaluation of that window is
  the one with the highest sequence inside it. The response is the same
  deterministic document as the bare gate (`dataset`, `version`, `verdict`,
  `reasons`, `counts`, one trailing newline), with the counts covering only
  the records inside the window. A window without any evaluation is
  `undetermined` with an empty reason list — a normal response, never an
  error. The endpoint accepts GET only and is strictly read-only. The path
  dataset/version resolves first (`404`); afterwards any request body bytes
  (whitespace-only included), a missing, repeated, unparseable or
  timezone-less `timestamp`, or any other query parameter are a `422` that
  writes nothing.
- `GET /datasets/{dataset}/quality-gate-export` — read-only cross-version
  export of the dataset's whole quality gate state, computed fresh on every
  read (no caching; nothing is written, modified or deleted, and no
  evaluation, anomaly record or gate verdict is touched). The path carries
  only the dataset name; the request takes no body and no query parameters —
  any body bytes, including a purely whitespace or single-space body, or any
  query parameter → `422`; an unknown dataset → `404`, checked before the
  request shape, with the same 404-before-422 precedence as the compliance
  exports. A dataset without schema versions returns `200` with an empty
  `versions` array and all-zero totals, never an error. The JSON document is
  deterministic (fixed key order, compact whitespace, exactly one trailing
  newline):

  ```json
  {"dataset":"orders","versions":[{"version":1,"verdict":"fail","reason_count":1,"violation_row_count":2},{"version":2,"verdict":"undetermined","reason_count":0,"violation_row_count":null}],"totals":{"version_count":2,"failed_count":1,"reason_count":1,"violation_row_count":2}}
  ```

  The top-level keys are exactly `dataset`, `versions`, `totals` in that
  order. `versions` is ordered by version number ascending and each entry has
  exactly `version`, `verdict`, `reason_count` and `violation_row_count` in
  that order. `verdict` is the current per-version gate verdict (one of
  `pass`, `undetermined`, `fail`) under exactly the same computation as the
  per-version gate read; `reason_count` is the number of reasons currently
  lowering that verdict (the length of the per-version gate's reason list);
  `violation_row_count` is the violation row count of the latest recorded
  evaluation and is `null` — never omitted — when the version was never
  evaluated. `totals` has exactly `version_count`, `failed_count`,
  `reason_count` and `violation_row_count`: the number of version entries,
  the number of versions whose verdict is `fail`, and the sums of the
  per-version reason and violation row counts (null violation row counts
  count as zero).
- `GET /datasets/{dataset}/quality-rule-coverage` — read-only cross-version
  check of how the dataset's schema fields are covered by quality rules,
  computed fresh on every read (no caching; nothing is written, modified or
  deleted, and no rule definition or evaluation record is touched). The path
  carries only the dataset name; the request takes no body and no query
  parameters — any body bytes, including a purely whitespace or single-space
  body, or any query parameter → `422`; an unknown dataset → `404`, checked
  before the request shape, with the same 404-before-422 precedence as the
  compliance exports. A dataset without schema versions returns `200` with an
  empty `versions` array and all-zero totals, never an error. The JSON
  document is deterministic (fixed key order, compact whitespace, exactly one
  trailing newline; the same data renders byte-identically after a process
  restart and ordering never depends on the database's natural order):

  ```json
  {"dataset":"orders","versions":[{"version":1,"fields":[{"field":"amount","coverage":"disabled","kinds":["numeric_range"],"rule_ids":[3]},{"field":"id","coverage":"enabled","kinds":["not_null","unique"],"rule_ids":[1,2]},{"field":"region","coverage":"unregistered","kinds":null,"rule_ids":null}]}],"totals":{"version_count":1,"field_count":3,"enabled_count":1,"disabled_count":1,"unregistered_count":1}}
  ```

  The top-level keys are exactly `dataset`, `versions`, `totals` in that
  order. `versions` is ordered by version number ascending and each entry has
  exactly `version` and `fields`, with every field of the version listed by
  field name ascending. Each field entry has exactly `field`, `coverage`,
  `kinds` and `rule_ids` in that order. `coverage` is one of the literals
  `enabled` (at least one enabled rule references the field), `disabled`
  (rules reference the field but all of them are disabled) and
  `unregistered` (no rule references the field). `kinds` is the
  de-duplicated list of referencing rule kinds sorted by literal value and
  `rule_ids` the referencing rules' ids de-duplicated and sorted ascending;
  every field of a unique rule counts as referenced. When a field is also
  referenced by disabled rules alongside enabled ones, their kinds and ids
  are included too. For an `unregistered` field both `kinds` and `rule_ids`
  are `null` — the keys are never omitted. `totals` has exactly
  `version_count`, `field_count`, `enabled_count`, `disabled_count` and
  `unregistered_count`: each is the sum of the per-version entries, and the
  field count equals the sum of the three per-state counts.
- `GET /datasets/{dataset}/quality-anomaly-summary` — read-only cross-version
  summary of the scale of quality anomalies, computed fresh on every read (no
  caching; nothing is written, modified or deleted, and no config, anomaly
  record or evaluation history is touched). The path carries only the
  dataset name; the request takes no body and no query parameters — any body
  bytes, including a purely whitespace or single-space body, or any query
  parameter → `422`; an unknown dataset → `404`, checked before the request
  shape, with the same 404-before-422 precedence as the quality gate export.
  A dataset without schema versions returns `200` with an empty `versions`
  array, zero counters and null ranges, never an error. The JSON document is
  deterministic (fixed key order, compact whitespace, exactly one trailing
  newline; the same data renders byte-identically after a process restart and
  ordering never depends on the database's natural order):

  ```json
  {"dataset":"orders","versions":[{"version":1,"config":{"consecutive_worsening_steps":2,"violation_row_limit":0,"rule_violation_limit":1000},"anomalies":[{"id":1,"kind":"row_limit","sequence":1,"rule_id":null,"violation_count":1,"created_at":"2026-09-29T16:27:18.012206+00:00"}],"stats":{"row_limit_count":1,"rule_limit_count":0,"trend_count":0,"anomaly_count":1,"max_sequence":1,"first_created_at":"2026-09-29T16:27:18.012206+00:00","last_created_at":"2026-09-29T16:27:18.012206+00:00"}},{"version":2,"config":{"consecutive_worsening_steps":null,"violation_row_limit":null,"rule_violation_limit":null},"anomalies":[],"stats":{"row_limit_count":0,"rule_limit_count":0,"trend_count":0,"anomaly_count":0,"max_sequence":null,"first_created_at":null,"last_created_at":null}}],"totals":{"version_count":2,"configured_version_count":1,"row_limit_count":1,"rule_limit_count":0,"trend_count":0,"anomaly_count":1,"max_sequence":1,"first_created_at":"2026-09-29T16:27:18.012206+00:00","last_created_at":"2026-09-29T16:27:18.012206+00:00"}}
  ```

  The top-level keys are exactly `dataset`, `versions` and `totals` in that
  order. `versions` is ordered by version number ascending and each entry has
  exactly `version`, `config`, `anomalies` and `stats` in that order.
  `config` holds exactly the three registered thresholds
  (`consecutive_worsening_steps`, `violation_row_limit`,
  `rule_violation_limit`); when no config is registered all three are `null`
  — the keys are never omitted. `anomalies` lists every anomaly record of the
  version still stored, ordered by `id` ascending (an empty array for a
  version without records); each record has exactly `id`, `kind`,
  `sequence`, `rule_id`, `violation_count` and `created_at`, where `kind` is
  one of `row_limit`, `rule_limit` and `trend` and `rule_id` is `null`
  (never omitted) except on `rule_limit` records. `stats` gives exactly
  `row_limit_count`, `rule_limit_count`, `trend_count`, `anomaly_count`,
  `max_sequence`, `first_created_at` and `last_created_at`: the four
  counters (per-kind counts and their total), the largest record sequence and
  the earliest/latest record write times; the last three are `null` — never
  omitted — when the version has no record. `totals` has exactly
  `version_count`, `configured_version_count` and the same seven stats keys
  in the same order: the counters are the sums of the per-version values,
  while the maximum sequence and the earliest/latest times span every record
  that still exists in the whole dataset (null when no record exists
  anywhere).

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
- `PUT /datasets/{dataset}/versions/{version}/privacy-policies/{policy_id}` —
  revise a policy. Body carries exactly the three adjustable fields:
  `{"classification": "PII", "masking": "partial", "allowed_roles":
  ["analyst"]}`, using the same value rules as registration — a non-empty
  `classification` after trimming, `masking` of `redact` or `partial`, and an
  `allowed_roles` array of distinct, non-empty-after-trimming role names that
  may be empty. The three fields are replaced together from one whole
  submission. The revision never changes the policy id, the field it belongs
  to, its `enabled` state or its `created_at`; it writes only the policy
  itself — no policy is created, no identification record is touched and no
  masking suggestion is registered. Returns `200` with the updated policy in
  the same shape as the policy read, and the revision survives restarts. The
  next privacy view read (and every later masked read) judges each value by
  the revised classification, masking and allowed roles. Already-written
  masking-hit records, access records and cleanup requests are unaffected —
  their contents and ordering stay as written; the grouping of the summary,
  diff and reconciliation responses keeps the same counting rules, while the
  policy classification and masking reported on trend rows (and the policy
  entries of the coverage check and compliance export) reflect the current
  values. Advisory masking suggestions are unaffected. Unknown
  dataset/version/policy → `404`, judged before every request-shape check;
  missing or extra fields, a blank `classification`, an illegal `masking`
  value, duplicate or blank `allowed_roles` entries, an empty or
  whitespace-only body, malformed JSON, a non-object payload or any query
  parameter → `422` and nothing is written. Concurrent revisions of the same
  policy have exactly one winner; the loser receives `409` and changes no
  field.

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

  Every successful view also appends one audit record per value it actually
  masked (the same field masked in several rows yields one record per masked
  value; a role in `allowed_roles`, a `null` value, an uncovered field or a
  disabled policy never hits); the records are persisted per version and are
  append-only.

  Every successful view additionally appends exactly one access record,
  whether or not it masked any value (an empty `rows` submission is recorded
  too). Rejected or failed views leave neither kind of record.
- `GET /datasets/{dataset}/versions/{version}/privacy-policies/view/access-records`
  — list every access record of the version, ordered by `sequence` ascending
  (empty when there are none, never an error). Each record has `sequence`,
  `role`, `row_count` (the number of submitted rows), `masked_count` (the
  number of masking-hit records the same view wrote, so the two logs
  cross-check) and `created_at`. The endpoint takes no request body and no
  query parameters (`422`); unknown dataset/version → `404`. Nothing is
  written.
- `GET /datasets/{dataset}/versions/{version}/privacy-policies/view/audit-records`
  — list every masking-hit record of the version, ordered by `sequence`
  ascending (empty when there are none, never an error). Each record has
  `sequence`, `field`, `policy_id`, `role`, `masking` and `created_at`. The
  endpoint takes no request body and no query parameters (`422`); unknown
  dataset/version → `404`. Nothing is written.
- `GET /datasets/{dataset}/versions/{version}/privacy-policies/view/audit-records/search`
  — read-only filtered retrieval of the same masking-hit records. The
  response is the same record collection as the full list (each record has
  `sequence`, `field`, `policy_id`, `role`, `masking` and `created_at`),
  sorted by `sequence` ascending; a filter that matches nothing returns an
  empty array, never an error. All filters are optional query parameters and
  combine with AND:
  - `role` — exact match against the requesting role recorded on the hit,
    case-insensitive (no substring or other fuzzy matching).
  - `field` — exact match against the hit field name, case-insensitive.
  - `start` / `end` — closed write-time interval: a record matches when its
    `created_at` is between the bounds inclusive. Each bound is a
    timezone-bearing ISO-8601 date-time (offset or `Z`); either bound may be
    given alone. `start` later than `end` → `422`.

  The endpoint takes no request body; an unparseable or timezone-less
  `start`/`end`, or any query parameter other than `role`, `field`, `start`
  and `end`, → `422`. Unknown dataset/version → `404`, with the same
  404-before-422 precedence as the record list. It is strictly read-only:
  no record is written, modified or deleted and the summary/diff responses
  are unaffected.
- `GET /datasets/{dataset}/versions/{version}/privacy-policies/view/audit-records/summary`
  — read-only compliance summary computed fresh from the persisted records on
  every read (no caching, nothing is written or deleted). Response:
  `{"dataset", "version", "groups"}`; `groups` is an empty array when the
  version has no records. Each group merges records with the same `field`,
  `policy_id`, `role` and `masking` (different roles or masking modes stay
  separate) and lists `field`, `policy_id`, `role`, `masking`, `hit_count`
  (one record counts as one hit), `first_hit_at` and `last_hit_at` (the
  earliest and latest record `created_at` in the group). Groups sort by
  `field`, then `policy_id`, `role` and `masking`, all ascending. The
  endpoint takes no request body and no query parameters (`422`); unknown
  dataset/version → `404`, with the same 404-before-422 precedence as the
  record list.
- `GET /datasets/{dataset}/versions/{version}/privacy-policies/view/audit-records/trend`
  — read-only hit trend aggregated by privacy policy, computed fresh from the
  persisted records on every read (no caching, nothing is written or deleted).
  Hits of the same policy merge across roles (a disabled policy's historical
  hits still count). Response: `{"dataset", "version", "policies", "totals"}`;
  `policies` is an empty array when the version has no records. Each policy
  row carries `policy_id`, `field`, `classification`, `masking`, `total_hits`
  and `days`; policies sort by `policy_id` ascending. `days` lists only UTC
  calendar days with hits (a write time with a non-zero offset buckets by its
  UTC day), sorted ascending with no zero-filled gaps; each entry has `day`
  (`YYYY-MM-DD`), `hit_count` (one record counts once), `hit_count_delta`
  (difference from the previous listed day, `null` on the first) and `trend`
  (`up`/`down`/`flat`, or `none` on the first day). `totals` gives
  `total_hits` (the sum of the row totals), `policy_count` (policies with
  hits) and `day_count` (distinct hit days over the whole version, the
  per-policy day union). The endpoint takes no request body and no query
  parameters (`422`); unknown dataset/version → `404`, with the same
  404-before-422 precedence as the record list.
- `POST /datasets/{dataset}/versions/{version}/privacy-policies/view/audit-records/cleanup-requests`
  — open a two-stage retention cleanup request for the version's masking-hit
  records. Body contains exactly `reason` and `before`:
  `{"reason": "legal hold expired", "before": "2026-01-01T00:00:00Z"}`.
  `reason` must be a string that is non-empty after trimming whitespace;
  `before` must be an ISO-8601 date-time carrying a timezone (offset or `Z`),
  and a record belongs to the target set only when its hit write time is
  strictly earlier than that instant. Creating a request only returns a
  preview — no hit record is deleted or modified, and the record list,
  search, summary, diff, reconcile and trend responses stay unchanged.
  Returns `201` with `id`, `reason` (trimmed), `before` (echoed as
  submitted), `status` (`pending`), `created_at` and a `preview` block:
  `hit_count` (the number of records that would be cleaned up),
  `first_hit_at` / `last_hit_at` (the earliest and latest hit times in the
  target set, `null` when the set is empty) and `fields` (the distinct field
  names involved, sorted ascending; `[]` when empty). The target set is
  fixed at creation time: masking-hit records written afterwards never enter
  the request even when they precede the cutoff by clock time. At most one
  pending request may exist per version; creating another while one is open
  returns `409` and writes nothing (confirmed requests never block a new
  one). Unknown dataset/version → `404`; missing/non-string/blank `reason`,
  missing/unparseable/timezone-less `before`, an extra body field, a
  non-JSON/non-object body or any query parameter → `422` and nothing is
  written, with 404 taking precedence.
- `GET .../privacy-policies/view/audit-records/cleanup-requests` — list the
  version's cleanup requests sorted by request `id` ascending (empty when
  there are none). Confirmed requests stay listed and requests survive
  process restarts. The endpoint takes no request body and no query
  parameters (`422`); unknown dataset/version → `404`.
- `POST .../privacy-policies/view/audit-records/cleanup-requests/{request_id}/confirm`
  — confirm a request with an empty request body and no query parameters.
  Confirmation atomically deletes exactly the target set frozen at creation
  and sets `status` to `confirmed`; the response is the request plus
  `confirmed_at` and `deleted_count` (the number of records actually
  deleted, possibly zero). Confirming an already-confirmed request returns
  `409` and never changes data again; concurrent confirmations have exactly
  one winner (the others get `409`) and can never leave the records half
  deleted. After a cleanup the surviving hit records keep their original
  `sequence` numbers and later hits continue the existing increasing run
  without reusing cleaned numbers; the record list and search return only
  surviving records, and summary, trend, diff and reconcile recompute from
  them. Access records are not cleaned up and the processing-task audit
  chain remains immutable. A request body or query parameter on the confirm
  endpoint → `422`; unknown dataset/version/request → `404` (precedence
  matches the hit-record reads), and confirming a confirmed request →
  `409`.
- `GET /datasets/{dataset}/privacy-compliance-export` — read-only
  cross-version export of the dataset's whole privacy compliance state,
  computed fresh on every read (no caching; nothing is written, modified or
  deleted, and no hit record, access record or cleanup request is touched).
  The path carries only the dataset name; the request takes no body and no
  query parameters (`422`); an unknown dataset → `404`, with the same
  404-before-422 precedence as the hit-record reads. A dataset without
  schema versions returns `200` with an empty `versions` array and all-zero
  totals, never an error. The JSON document is deterministic (fixed key
  order, compact whitespace, exactly one trailing newline):

  ```json
  {"dataset":"orders","versions":[{"version":1,"policies":[...],"hit_count":0,"masked_count":1,"view_count":2,"cleanup_requests":[...]}],"totals":{...}}
  ```

  The top-level keys are exactly `dataset`, `versions`, `totals` in that
  order. `versions` is ordered by version number ascending and each entry
  has exactly `version`, `policies`, `hit_count`, `masked_count`,
  `view_count`, `cleanup_requests` in that order. `policies` lists the
  version's registered policies ordered by policy id; each policy has `id`,
  `field`, `classification`, `masking`, `allowed_roles` and `enabled`.
  `cleanup_requests` lists the version's cleanup requests ordered by request
  id (both pending and confirmed are retained); each has `id`, `reason`,
  `status` and `created_at`. `hit_count` is the number of masking-hit
  records currently stored for the version, `masked_count` is the sum of the
  access records' masked-value counts and `view_count` is the number of
  access records; all three reflect the records surviving a confirmed
  cleanup. `totals` has exactly `policy_count`, `hit_count`, `masked_count`,
  `view_count` and `cleanup_request_count`, each the sum of the per-version
  values over the whole dataset.
- `GET /datasets/{dataset}/privacy-policy-coverage` — read-only cross-version
  check of the dataset's privacy policy coverage, computed fresh on every
  read (no caching; nothing is written, modified or deleted, and no policy
  or identification record is touched). The path carries only the dataset
  name; the request takes no body and no query parameters (`422`); an
  unknown dataset → `404`, with the same 404-before-422 precedence as the
  compliance export. A dataset without schema versions returns `200` with an
  empty `versions` array and all-zero totals, never an error. The JSON
  document is deterministic (fixed key order, compact whitespace, exactly
  one trailing newline):

  ```json
  {"dataset":"orders","versions":[{"version":1,"fields":[...],"candidates":[...]}],"totals":{...}}
  ```

  The top-level keys are exactly `dataset`, `versions`, `totals` in that
  order. `versions` is ordered by version number ascending and each entry
  has exactly `version`, `fields`, `candidates` in that order. `fields`
  lists every field of the version ordered by field name; each entry has
  `field`, `coverage`, `classification`, `masking` and `enabled`, where
  `coverage` is `enabled` (an enabled policy is registered), `disabled` (a
  policy is registered but disabled) or `unregistered` (no policy), and the
  last three keys report the registered policy's classification, masking and
  enabled state (all `null` when no policy is registered). `candidates`
  lists the advisory suggestions for fields that were identified with at
  least one name or sample hit but carry no privacy policy yet, ordered by
  identification record id ascending; each candidate has exactly `field`,
  `classification` and `masking`, and an identified field whose name and
  samples both missed does not appear. The candidates are a compliance
  self-check reference only: they never register a policy and never rewrite
  an identification record. `totals` has exactly `version_count`,
  `field_count`, `enabled_count`, `disabled_count`, `unregistered_count`
  and `candidate_count`, each the sum of the per-version values over the
  whole dataset.

### Sensitive-field identification

Sensitive-field identification produces candidate annotations for the fields of
a schema version. It complements the manual privacy policies but never creates
or modifies one: an identification is advisory only. Records (including their
stable ids) persist across restarts.

- `POST /datasets/{dataset}/versions/{version}/sensitive-identifications` —
  identify one field. The body contains exactly `field` and `samples`:
  `{"field": "contact_email", "samples": ["alice@example.com"]}`. `field` must
  name an existing field of the version; its name and type are taken from the
  version definition. `samples` is an array of JSON scalars (strings, numbers,
  booleans or `null`) and may be empty; objects or arrays reject the whole
  request. The samples are never stored or echoed back. Returns the record;
  the first submission for a field returns `201`, re-running the same field
  refreshes the record in place (the id is unchanged) and returns `200`.
- `GET /datasets/{dataset}/versions/{version}/sensitive-identifications` —
  return every identification record of the version sorted by id ascending
  (empty when there are none). The endpoint takes no request body and no query
  parameters (`422`); unknown dataset/version → `404`.

  Each record has `id`, `field`, `field_type`, `evidence`, `confidence`,
  `source` and `created_at`. `source` references the identified field as
  `{"dataset", "version", "field"}`. `evidence` is the ordered, de-duplicated
  list of hit kinds, name hits before sample hits:

  - **Name hits** — the field name contains a sensitive word case-insensitively
    as a substring. The words are `email`, `phone`, `id_card`, `password`,
    `token` and `birth`, emitting `name:<word>` in that order.
  - **Sample hits** — only string sample values are inspected (numbers,
    booleans and `null` never hit), and only when the field's declared type is
    `string`; a field declared with any other type is matched by name alone,
    even when a sample looks like an email. `sample:email` requires exactly
    one `@` with non-empty text on both sides and a dot in the domain;
    `sample:phone` requires an 11-digit string starting with `1`. Either format
    matching is sufficient.

  `confidence` is `high` when both the name and the samples hit, `medium` for
  samples only, `low` for the name only and `none` when neither side hits
  (`evidence` is then an empty list but the record is still generated).

  One record is kept per field: a re-run overwrites the evidence and confidence
  in place with a stable id. Unknown dataset/version → `404`; a field that does
  not exist in the version → `404`; missing or extra body fields, a
  non-string or blank field name, a `samples` value that is not an array of
  scalars (objects and arrays included), an empty body, malformed JSON or any
  query parameter → `422` and nothing is written.

#### Masking strategy suggestions

- `GET /datasets/{dataset}/versions/{version}/sensitive-identifications/masking-suggestions`
  — read-only advisory masking-strategy candidates derived from the version's
  identification records. The endpoint takes no request body and no query
  parameters (`422` if either is present, with nothing written); unknown
  dataset/version → `404`. A version without identification records returns an
  empty list rather than an error.

  Each candidate has exactly `field`, `classification`, `masking` and
  `allowed_roles`. Only records whose name or samples hit produce a candidate;
  a record with empty evidence is omitted. Candidates follow the identification
  records' `id` ascending and are recomputed on every read, so refreshing an
  identification changes the suggestion on the next request. The endpoint
  writes nothing, never registers a privacy policy and leaves the
  identification records' fields, ordering and refresh semantics untouched;
  the privacy view continues to mask according to registered policies only.

  - `classification` is `PII` for email, phone, id-card and birthday hits, and
    `CREDENTIAL` for password and token hits. A field hitting both is `PII`.
  - `masking` reuses the privacy-view vocabulary: `partial` for `PII`
    candidates and `redact` for `CREDENTIAL` candidates (a both-kinds field
    takes `partial`); no new masking format is introduced.
  - `allowed_roles` is always an empty list: the suggested masking applies to
    every role, granting none an unmasked view.

- `POST /datasets/{dataset}/versions/{version}/sensitive-identifications/masking-suggestions/register`
  — turn the current advisory candidates into registered privacy policies. The
  body contains only `{"fields": ["<field>", ...]}`: a non-empty array of
  distinct field names, each non-empty after trimming and naming an existing
  field of the version that currently has a masking suggestion candidate.
  Returns `201` with one privacy policy record per requested field, in the
  request's field-name order; each record has the same fields as the manual
  policy creation response (`id`, `field`, `classification`, `masking`,
  `allowed_roles`, `enabled`, `created_at`), is enabled by default and gets a
  service-generated id and timestamp. Each policy copies its candidate's
  `classification` and `masking` verbatim with the candidate's empty
  `allowed_roles`, so after registration every role sees those fields masked;
  the next privacy view read masks according to the new policies. The whole
  batch is validated before anything is written, so any rejection writes no
  policy and no timestamp. Unknown dataset/version, or a named field that does
  not exist in the version → `404`; a field without an available candidate
  (never identified, or whose identification record currently yields no
  candidate) or one that already has a privacy policy → `409`; two concurrent
  registrations of the same field leave exactly one winner. Missing or extra
  body fields, an empty `fields` array, duplicate, non-string or blank names,
  an empty body, a whitespace-only body, malformed JSON or any query parameter
  → `422` and nothing is written. Registration only reads the identification
  records and field definitions; it never writes or auto-refreshes an
  identification, and the read-only suggestions endpoint stays unchanged.

#### Whole-dataset identification summary

- `GET /datasets/{dataset}/sensitive-identification-summary` — read-only
  cross-version summary of the dataset's sensitive identifications, computed
  fresh on every read (no caching; nothing is written, modified or deleted,
  and no identification record is touched). It is mounted under the dataset
  resource rather than a version and accepts GET only. The path carries only
  the dataset name; the request takes no body, no query parameters and no
  other request control — any body bytes, including a purely whitespace or
  single-space body, or any query parameter → `422`; an unknown dataset →
  `404`, checked before the request shape, with the same 404-before-422
  precedence as the other cross-version summaries. A dataset without schema
  versions returns `200` with an empty `versions` array and all-zero/null
  totals, never an error. The suggestions are the same read-time
  recomputation as the read-only suggestions endpoint; the summary never
  registers a privacy policy, and identification, masking suggestion and
  privacy view behavior are otherwise unchanged. The JSON document is
  deterministic (fixed key order, compact whitespace, lowercase booleans,
  exactly one trailing newline; the same data renders byte-identically after
  a process restart and ordering never depends on the database's natural
  order).

  The top-level keys are exactly `dataset`, `versions` and `totals` in that
  order. `versions` is ordered by version number ascending and each entry has
  exactly `version`, `identifications`, `suggestions` and `stats` in that
  order; a version without identification records keeps empty
  `identifications` and `suggestions` arrays and the zero/null stats rather
  than being omitted.

  - `identifications` lists the version's records ordered by record `id`
    ascending; each entry has exactly `id`, `field`, `field_type`,
    `confidence`, `source_dataset`, `source_version`, `source_field` and
    `created_at` in that order (the source is flattened, and `evidence` is
    not part of the summary).
  - `suggestions` lists the advisory masking candidates in identification
    record `id` ascending order, exactly as the read-only suggestions
    endpoint recomputes them (a record whose name and samples both missed
    does not appear); each candidate has exactly `field`, `classification`,
    `masking` and `allowed_roles` in that order, and `allowed_roles` is
    always an empty list, including when the version has no suggestions.
  - `stats` has exactly `identification_count`, `suggestion_count`,
    `high_count`, `medium_count`, `low_count`, `none_count`, `max_id`,
    `first_created_at` and `last_created_at` in that order: the four
    confidence counters partition the version's records, and `max_id` and
    the earliest/latest write times are `null` for a version without
    records — the keys are never omitted. Same-instant write times are
    ordered by record id ascending.

  `totals` has exactly `version_count`, `identified_version_count`,
  `identification_count`, `suggestion_count`, `high_count`, `medium_count`,
  `low_count`, `none_count`, `max_id`, `first_created_at` and
  `last_created_at` in that order: `version_count` counts every schema
  version and `identified_version_count` the versions with at least one
  identification record; every other counter is the sum of the matching
  per-version values, while `max_id` and the time range are taken over every
  identification record of the whole dataset (all `null` when no record
  exists). Errors keep the stable `{"error", "detail"}` shape and never
  expose SQL, stack traces or internal objects.

### Row snapshots

A row snapshot persistently records the rows of a schema version at one point in
time. Snapshots (including their rows) survive restarts.

- `POST /datasets/{dataset}/versions/{version}/snapshots` — persist a snapshot.
  Body: `{"rows": [{...}, ...]}` where `rows` must be an array of JSON objects
  (it may be empty). The JSON values and the order of rows/object keys are deep
  copied verbatim. A SHA-256 content fingerprint of the saved row sequence is
  computed at creation and written together with the rows in one atomic write,
  so it can later attest that the persisted rows are unchanged; the fingerprint
  is not part of the response, which stays `id`, `dataset`, `version`,
  `created_at` and `row_count` (no rows). In the same transaction the service
  also maintains the snapshot diff cache incrementally (see "Snapshot diff
  cache" below): the canonical form of every row (key-sorted compact JSON text
  in stored row order) and the sorted set of top-level field names are written
  together with the snapshot, and one `created` record is appended to the
  version's cache trail. Unknown dataset/version → `404`; malformed or
  non-object rows → `422` and nothing is written.
- `GET /datasets/{dataset}/versions/{version}/snapshots` — list snapshot
  metadata sorted by `id` ascending (no `rows`).
- `GET /datasets/{dataset}/versions/{version}/snapshots/{snapshot_id}` — return
  the metadata together with the saved `rows`. Unknown dataset/version/snapshot
  → `404`.
- `GET /datasets/{dataset}/versions/{version}/snapshots/{snapshot_id}/verify` —
  read-only content-fingerprint verification, appended one segment after the
  single-snapshot read address and accepting GET only. On every read the
  fingerprint of the currently persisted row sequence is recomputed and
  compared with the fingerprint stored with the snapshot at creation; the
  snapshot and its rows are only read, never written or modified. The JSON
  document is deterministic (fixed key order, compact whitespace, lowercase
  booleans, exactly one trailing newline) with top-level keys `dataset`,
  `version`, `snapshot_id`, `row_count`, `stored_hash`, `computed_hash` and
  `valid` in that order. `stored_hash` is the fingerprint written at creation,
  `computed_hash` the freshly recomputed one and `valid` is `true` exactly
  while both agree; directly changing, adding, deleting or replacing a saved
  row makes them differ and changes `computed_hash`. Both verdicts return
  `200`. The fingerprint hashes the saved row sequence as a JSON array in row
  order with each object's keys sorted by Unicode code point, compact text,
  non-ASCII characters unescaped, UTF-8 encoded and SHA-256 digested to
  hexadecimal; array order and value types are significant (integers, floats,
  strings and booleans never compare equal and negative zero is represented
  distinctly), an empty snapshot is fingerprinted normally and the same rows
  yield the same digest across restarts. The verification reads only the
  snapshot and its rows — no lineage, quality, privacy or task state — and
  does not affect retention or deletion; a confirmed-deleted snapshot is no
  longer addressable here either (`404`). The endpoint takes no request body
  and no query parameters: any body bytes — including whitespace-only ones —
  or any query parameter are a `422`, checked after the path
  dataset/version/snapshot resolves, so an unknown dataset, version or
  snapshot is a `404` first. Every rejection writes nothing.
- `GET /datasets/{dataset}/versions/{version}/snapshots/at?timestamp=<ISO-8601>` —
  return the most recent snapshot whose `created_at` is not later than
  `timestamp`, including its rows. The timestamp must be a timezone-aware
  ISO-8601 date-time (an offset or trailing `Z`): missing, invalid or
  timezone-less values return `422`; unknown dataset/version → `404`; when no
  snapshot exists at or before the timestamp the response is `404`.
- `GET /datasets/{dataset}/versions/{version}/snapshots/at/diff?from=<ISO-8601>&to=<ISO-8601>`
  — read-only time-travel row diff, appended one segment after the bare-row
  time lookup and accepting GET only. Each of `from` (the baseline) and `to`
  (the target) must be a timezone-aware ISO-8601 date-time; each side
  independently selects the latest snapshot whose `created_at` is not later
  than its own timestamp (the same selection as the bare-row lookup), so the
  two times may resolve to the same snapshot and a `to` earlier than `from` is
  valid — the two snapshots are simply compared in their named order. The
  comparison reads snapshots and rows and writes nothing. The JSON document
  is deterministic (fixed key order, compact whitespace, exactly one trailing
  newline) with top-level keys `from_timestamp`, `to_timestamp`,
  `from_snapshot_id`, `to_snapshot_id`, `added`, `removed`, `fields_added`
  and `fields_removed` in that order; the timestamps echo the submitted query
  values. `added` and `removed` use the exact comparison semantics of the
  snapshot-id diff (JSON-object multisets, key order irrelevant, array order
  and value types significant, duplicates counted; entries
  `{"row", "count"}` sorted by canonical row text). `fields_added` and
  `fields_removed` compare the top-level field names appearing on the
  snapshots' row objects: names only on the target side are added, names only
  on the baseline side are removed, names on both enter neither set, and each
  list sorts by field name ascending. When both times select the same
  snapshot all four collections are empty and the request succeeds. Unknown
  dataset/version → `404`, checked before every request-shape check; a missing,
  blank, repeated, unparseable or timezone-less `from`/`to`, an extra query
  parameter or any request body bytes (whitespace-only included) → `422`; a
  side with no snapshot at or before its timestamp → `404` and nothing is
  written.
- `POST /datasets/{dataset}/versions/{version}/snapshots/at/diff/masked` —
  role-masked time-travel row diff, appended one segment after the bare-row
  time diff and accepting POST only. The body contains exactly `role`,
  `from` and `to`: `{"role": "guest", "from": "<ISO-8601>", "to":
  "<ISO-8601>"}`; `role` must be non-empty after trimming and each time must
  be a timezone-aware ISO-8601 date-time (offset or `Z`). Each side
  independently selects the newest snapshot whose `created_at` is not later
  than its own timestamp, with exactly the same selection as the bare-row
  time lookup. Added and removed entries are first judged and counted from
  the raw, unmasked rows with the bare-row diff's JSON-object multiset
  semantics (key order irrelevant, array order and value types significant,
  duplicates counted): a row appearing several times stays one entry with
  its multiplicity, and masking only rewrites the entry's row data — entries
  are never merged or split. Entries sort by the canonical (key-sorted) JSON
  text of the raw row, so the output order is unaffected by masking. Each
  emitted row is then masked exactly as the row-submission privacy view masks
  it, applying only the version's currently enabled policies
  (`redact`/`partial` semantics, `allowed_roles`, nulls and uncovered fields
  behave identically). The response is the same deterministic document as
  the bare-row time diff (fixed key order, compact whitespace, exactly one
  trailing newline) with the same keys in the same order: `from_timestamp`,
  `to_timestamp`, `from_snapshot_id`, `to_snapshot_id`, `added`, `removed`,
  `fields_added` and `fields_removed`; the timestamps echo the submitted
  body values and the field-name sets carry no values, so masking can never
  change them. Every successful read appends one masking-hit record per
  value it actually masks, counted per occurrence across both snapshots'
  diff entries (an entry of count `n` whose covered value is masked yields
  `n` hit records; added-side entries first, then removed-side, each side in
  canonical row order), structurally identical to the regular view's hit
  records and continuing the same per-version sequence run, plus exactly one
  access record whose `row_count` is the total number of rows expanded from
  both sides' entries and whose `masked_count` equals the number of hit
  records the read wrote. The records enter all existing masking-hit/access
  reads, filters, summaries, diffs, reconciliation, trends and cleanup
  previews; records of one read share a write timestamp, sequences stay
  continuous across processes, and a write obstruction never fails the read.
  The read never modifies snapshots, rows, policies or identification
  records. Unknown dataset/version → `404`; each side with a usable
  timestamp is resolved independently, so a side with no snapshot at or
  before its timestamp is likewise `404` and precedes every shape check
  (including a missing or invalid timestamp on the other side); a missing or
  wrong-typed `role`/`from`/`to`, a role blank after trimming, an
  unparseable or timezone-less time, an extra body field, an empty,
  whitespace-only or non-JSON body, a non-object body and any query
  parameter are `422` and write nothing.
- `POST /datasets/{dataset}/versions/{version}/snapshots/at/masked-view` —
  role-masked time travel, appended one segment after the bare-row time
  lookup and accepting POST only. The body contains exactly `role` and
  `timestamp`: `{"role": "guest", "timestamp": "2026-01-01T00:00:00Z"}`; the
  service selects the latest snapshot whose `created_at` is not later than
  `timestamp` (the same selection as the bare-row lookup). On success it
  returns `{"dataset", "version", "snapshot_id", "created_at", "rows"}`; the
  `rows` are an order-preserving copy of the snapshot's rows masked exactly
  as the row-submission privacy view masks them, applying only the version's
  currently enabled policies (`redact`/`partial` semantics, `allowed_roles`,
  nulls and uncovered fields behave identically). Every successful read
  appends one masking-hit record per value actually masked, structurally
  identical to the regular view's hit records and continuing the same
  per-version sequence run, plus exactly one access record whose
  `row_count` is the snapshot's row count and whose `masked_count` equals
  the number of hit records written by the read. An empty snapshot, an
  allowed role, all-null values and a version without policies all succeed
  and leave only the access record. The records enter all existing
  masking-hit/access reads, filters, summaries, diffs, reconciliation,
  trends and cleanup previews; records of one read share a write timestamp,
  sequences stay continuous across processes, and a write obstruction never
  fails the read. The read never modifies or deletes the snapshot or its
  rows and never changes policies or identification records. Unknown
  dataset/version → `404`, and no snapshot existing at or before the
  timestamp is likewise `404`, both checked before every shape check (the
  latter whenever the body carries a usable timestamp); a missing or
  wrong-typed `role`/`timestamp`, a role blank after trimming, an
  unparseable or timezone-less timestamp, an extra body field, an empty,
  whitespace-only or non-JSON body, a non-object body and any query
  parameter are `422` and write nothing.
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
- `GET /datasets/{dataset}/snapshots/{base_snapshot_id}/diff/{target_snapshot_id}` —
  read-only comparison of two snapshots belonging to two **different** schema
  versions of the path dataset; the baseline snapshot is named first and the
  target second, and either version order is allowed. It takes no request
  body and no query parameters (`422`). Unknown dataset or snapshot → `404`,
  checked ahead of every request-shape check; a snapshot owned by another
  dataset, or two snapshots of the same schema version (same-version
  comparisons still go through the snapshot-id diff endpoint) → `422`. The
  JSON document is deterministic (fixed key order, compact whitespace,
  exactly one trailing newline) with top-level keys `base_snapshot_id`,
  `base_version`, `target_snapshot_id`, `target_version`, `field_changes`,
  `added` and `removed` in that order. `field_changes` lists only additions,
  removals, type changes and nullability changes of the two versions' field
  definitions, reusing the compatibility/trajectory literals (`added`,
  `removed`, `type_changed`, `nullable_tightened`, `nullable_loosened`);
  several changes of one field collapse into a single entry (a type change
  wins over a nullability change), entries sort by field name and each entry
  has `field`, `kind`, `before` and `after`, with the missing side `null`
  (never omitted). Rows are projected onto exactly the field names defined
  in both versions — other stored keys and fields absent from a row do not
  participate — and the projections are compared with the same-version
  snapshot diff's multiset semantics (object key order irrelevant, array
  order and value types significant, duplicates counted). `added` lists
  rows present more often or only on the target side, `removed` the
  converse, each entry `{"row", "count"}` sorted by the canonical
  (key-sorted) JSON text of the projected row. The comparison is fully
  read-only: snapshots, rows, field definitions, the lineage impact cache
  and every privacy trail are never touched, and the result is identical
  across process restarts.
- `POST /datasets/{dataset}/versions/{version}/snapshots/{snapshot_id}/quality-rules/evaluate`
  — evaluate the version's enabled quality rules over the snapshot's persisted
  rows. The endpoint takes no request body and no query parameters (`422`);
  unknown dataset/version/snapshot → `404` (checked first), and a snapshot
  owned by another version → `422`. The response and the rule semantics are
  exactly those of the row-submission evaluation (`{"dataset", "version",
  "results"}`), the snapshot and its rows are never modified, and every
  successful evaluation appends the same immutable history summary (with
  `row_count` set to the snapshot's actual row count) that also feeds the
  evaluation diff and anomaly detection. An empty snapshot is recorded with
  zero violations.

### Snapshot diff cache (incremental, maintained at write time)

Every snapshot carries an incrementally maintained diff-cache record: when a
snapshot is created, the canonical form of its rows (one key-sorted compact
JSON text per row, in stored row order) and the sorted set of its top-level
field names are written atomically together with the snapshot and its
fingerprint. The same-version snapshot diff, the bare and masked time diffs
and the two cross-version snapshot comparisons synthesize their result from
the cached canonical forms whenever both sides' records are complete and
agree with the rows currently persisted; when a record is missing (a
snapshot saved before the cache existed) or disagrees with the extant rows,
that side is recomputed from the extant rows instead. The two paths produce
the exact same comparison down to every byte, and the comparisons
themselves stay fully read-only (the cache is never written, repaired or
voided by a read). When a snapshot is confirmed deleted, its cache entry is
voided in the same transaction that removes the snapshot and writes the
deletion proof. The cache is persisted across restarts.

- `GET /datasets/{dataset}/versions/{version}/snapshots/cache-audit` —
  read-only consistency audit, appended one segment after the version
  snapshot collection and accepting GET only. Every snapshot currently
  stored in the version is compared against its cache record and the
  canonical form recomputed fresh from the snapshot's extant rows; a
  snapshot without a record is `missing` — a normal state, never an error —
  a stored record equal to the recomputation is `cached` and any
  disagreement is `mismatch`. Entries are sorted by snapshot id ascending
  and the sort never depends on database order. The JSON document is
  deterministic (fixed key order, compact whitespace, lowercase booleans,
  exactly one trailing newline) with top-level keys `dataset`, `version`,
  `entries` and `counts` in that order; each entry has exactly `snapshot_id`
  and `status`, and `counts` gives `cached_count`, `missing_count` and
  `mismatch_count`, each equal to the number of entries with that status. A
  version without snapshots returns an empty entry list and all-zero
  counts. The audit recomputes on every read and never writes: no cache
  record is inserted, voided or repaired, so snapshot creation, reads,
  diffs, masking, verification, retention cleanup and deletion behave
  exactly as before. The endpoint takes no request body and no query
  parameters; any body bytes — including whitespace-only ones — or any query
  parameter are a `422`, checked only after the path dataset/version
  resolves, so an unknown dataset or version is a `404` first, like the
  existing snapshot reads.
- `GET /datasets/{dataset}/versions/{version}/snapshots/cache-trail` —
  read-only cache lifecycle trail, appended one segment after the version
  snapshot collection and accepting GET only. Every record of the version is
  returned in ascending `sequence` order as a bare JSON array (an empty
  version is `[]`, never an error); each record has exactly `sequence`,
  `cause`, `snapshot_id` and `created_at` in that order. `sequence` numbers
  the version's records from 1 in write order — continuous, never reused and
  never skipped — `cause` is `created` (the snapshot wrote its cache record)
  or `deleted` (a confirmed deletion voided it), `snapshot_id` names the
  snapshot and `created_at` is the timezone-bearing write time (the snapshot
  creation time for `created`, the deletion commit time for `deleted`). The
  trail is append-only (the database itself refuses updates and deletes), is
  written in the same transaction as the snapshot creation or confirmed
  deletion it records and survives restarts unchanged. The read only reads
  persisted records and never writes, voids or repairs anything. The
  endpoint takes no request body and no query parameters; any body bytes
  (whitespace-only included) or any query parameter are a `422`, checked
  only after the path dataset/version resolves, so an unknown dataset or
  version is a `404` first.

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
  the status change to `confirmed`, and the response adds `confirmed_at`. In
  the same transaction exactly one immutable deletion proof is appended to the
  version's proof chain (see "Snapshot deletion proofs" below). Once deleted,
  the snapshot no longer appears in snapshot read, list, `at` or diff
  responses.
- `POST .../snapshots/{snapshot_id}/deletion-requests/{request_id}/recheck` —
  recompute the blocked state of an open request against the current lineage
  graph, appended one segment after the individual deletion-request resource
  and accepting POST only. The impacted baseline is computed once at creation
  time; this recomputes the direct and indirect downstream set of every field
  of the snapshot's version with the exact creation semantics (deduplicated,
  cycles terminating, never containing a start field, sorted by dataset,
  version and field ascending) and, while the request is still open, replaces
  `impacted` and derives `status` afresh — `blocked` when any downstream field
  exists, `pending` otherwise. A newly added downstream therefore moves a
  `pending` request back to `blocked` (a still-depended-on snapshot is not
  deleted by mistake), and a removed downstream moves a `blocked` request back
  to `pending` so it can later be confirmed under the existing age rule. The
  response is the updated request record in the creation-response shape with
  the recomputed status and impacted set and every other field unchanged. The
  recomputation never deletes the snapshot, creates a request or changes the
  retention policy, the age rule or the lineage mappings, and the updated
  record survives restarts while the request list stays ordered by id. The
  endpoint takes no request body and no query parameters: any body bytes —
  whitespace-only or malformed JSON included — or any query parameter are a
  `422`. Unknown dataset, version or request → `404`, judged ahead of every
  request-shape check; a request that exists but does not belong to the
  snapshot named in the path is a `422` and writes nothing. A confirmed
  request can never be rechecked — even after its snapshot has been deleted
  the response is `409` and no field changes — and concurrent rechecks, or a
  recheck racing a confirmation or a batch sweep, have exactly one winner: the
  loser receives `409` and writes nothing.

  Unknown dataset/version/snapshot/policy/request → `404`; other invalid input
  → `422` and nothing is written.
- `POST /datasets/{dataset}/versions/{version}/snapshots/retention-sweep` —
  batch retention cleanup of the version's snapshots, appended one segment
  after the version's snapshot collection and accepting POST only. The body
  contains only a non-empty `reason`: `{"reason": "retention reached"}` (the
  trimmed reason is stored on every request the sweep opens). Every snapshot
  of the version is scanned in snapshot-id order: a snapshot whose age has
  reached the version's retention days (the same age judgment as the
  deletion-request confirmation) and that carries no open (`pending`/
  `blocked`) deletion request gets a new request with the submitted reason —
  `blocked` when the version feeds downstream fields and `pending` otherwise,
  the same status judgment as the single-snapshot entry. A snapshot with an
  open request is skipped (never duplicated, never modified) and a snapshot
  younger than the retention age is skipped and only counted; a version
  without snapshots sweeps successfully with empty collections. The whole
  scan runs in a single transaction — either every new request is written or
  none is — and concurrent sweeps of the same version have a single winner,
  the loser receiving `409` and changing nothing. The response is a
  deterministic JSON document (fixed key order, compact whitespace, exactly
  one trailing newline) with top-level keys `dataset`, `version`, `created`,
  `skipped` and `counts` in that order. `created` and `skipped` are sorted by
  snapshot id ascending; each entry has exactly `snapshot_id`, `request_id`,
  `status` and `reason` (skipped entries echo the existing open request).
  `counts` gives `created_count`, `skipped_count` and `not_due_count`, each
  equal to the number of snapshots in that category. The opened requests are
  ordinary deletion requests: they enter the existing deletion-request list
  and confirm flow, are deleted atomically on confirmation and persist across
  restarts. Unknown dataset/version → `404` (judged before every body/query
  shape check); a version without a registered retention policy → `404`; a
  missing, non-string or blank `reason`, an extra body field, an empty or
  whitespace-only body, malformed JSON, a non-object body or any query
  parameter → `422` and nothing is written.
- `GET /datasets/{dataset}/versions/{version}/snapshots/retention-sweep/preview` —
  read-only preview of the batch retention sweep, appended one segment after
  the sweep address and accepting GET only with no request body and no query
  parameters. The version's existing snapshots are scanned in snapshot-id
  order with exactly the sweep's classification and age judgment (a snapshot
  whose age has reached the version's retention days is due), but nothing is
  ever written. A due snapshot without an open (`pending`/`blocked`) request is
  placed in `would_create` with its snapshot id and the status the new request
  would carry — `blocked` when the version feeds downstream fields, `pending`
  otherwise — the downstream set being computed from the current committed
  lineage graph with the single-snapshot semantics (deduplicated, cycles
  terminating, never containing a start field). A snapshot with an open
  request is placed in `skipped`, echoing that request's id and status; a
  snapshot younger than the retention age and without an open request is
  placed in `not_due` by snapshot id alone (a not-yet-due snapshot that
  already carries an open request is skipped instead). Each snapshot appears
  exactly once and every collection is sorted by snapshot id ascending. The response is a deterministic JSON document (fixed key order,
  compact whitespace, lowercase booleans, exactly one trailing newline) with
  top-level keys `dataset`, `version`, `would_create`, `skipped`, `not_due`
  and `counts` in that order; `counts` gives `would_create_count`,
  `skipped_count` and `not_due_count`, whose sum equals the snapshot total.
  The preview's classification and statuses match the requests a subsequent
  real sweep opens on the same committed state. Unknown dataset/version →
  `404` and a version without a registered retention policy → `404`, both
  judged before every request-shape check; any request body (whitespace-only
  bytes included) or any query parameter → `422`, and no rejection writes
  anything.

### Snapshot deletion proofs (append-only proof chain)

Every schema version carries an independent, tamper-evident chain of deletion
proofs. Exactly one proof is appended in the same transaction as a successful
snapshot deletion, so the deletion and its evidence commit together or not at
all. Proofs are append-only (the database rejects updates and deletes, and
there are no per-proof HTTP routes), numbered from `1` per version and linked
through SHA-256 evidence hashes. They persist across restarts and stay
readable after every snapshot they attest has been deleted. Snapshots deleted
before this feature existed get no backfilled proof and do not appear in the
list; snapshot numbers are never reused.

- `GET /datasets/{dataset}/versions/{version}/snapshots/deletion-proofs` —
  list every proof of the version ordered by `sequence` ascending (empty when
  none exist, never an error). Each proof has exactly `sequence`,
  `snapshot_id`, `row_count`, `stored_hash` and `reason`, then `confirmed_at`,
  `previous_hash` and `evidence_hash`, in that fixed order. `row_count` and
  `stored_hash` are the deleted snapshot's on-disk row count and content
  fingerprint captured at the deletion instant (identical to the snapshot
  verify endpoint's `stored_hash`), `reason` is the reason on the confirmed
  request and `confirmed_at` is the deletion commit time (the same value the
  confirmation returns). The first proof's `previous_hash` is `null`; every
  later one equals the preceding proof's `evidence_hash`.
- `GET /datasets/{dataset}/versions/{version}/snapshots/deletion-proofs/verify`
  — read-only verification of the whole chain, recomputed fresh on every read.
  Every `evidence_hash` is recomputed, `sequence` values are checked to run
  continuously from `1` and each `previous_hash` to equal the preceding
  proof's `evidence_hash` (the first must be `null`). Returns exactly
  `dataset`, `version`, `valid` and `checked_count`; an intact chain reports
  `valid` `true`, a rewritten proof or a broken/gapped chain reports `false`
  as a normal `200` rather than an error, and an empty chain is valid with a
  zero count. Nothing is written.

`evidence_hash` uses the same digest convention as the processing-run audit
chain: the hexadecimal SHA-256 of a canonical JSON document built from every
stored field except `confirmed_at` and the hash fields — `sequence`,
`snapshot_id`, `row_count`, `stored_hash`, `reason` and `previous_hash` — with
keys sorted by Unicode code point, compact whitespace, non-ASCII characters
unescaped and UTF-8 encoded.

Both endpoints are read-only: they take no request body and no query
parameters. Any body bytes (whitespace-only included) or any query parameter
→ `422`, checked only after the path dataset/version resolves, so an unknown
dataset or version is a `404` first. Both return deterministic JSON documents
(fixed key order, compact whitespace, lowercase booleans, exactly one trailing
newline), and errors keep the stable `{"error", "detail"}` shape without
exposing SQL, stack traces or internal objects.

A failed deletion, a rejected request or a race that loses confirmation
writes no proof: a too-young or `blocked` confirmation and a repeated
confirmation are `409` with zero writes. Concurrent confirmations of
*different* snapshots of one version both succeed — serialized in some order —
and their proof sequences are continuous, never repeated and never skipped, so
the chain has no gap; a recheck or a sweep racing a confirmation remains
single-winner, the loser receiving `409` and changing nothing. Existing
snapshot reads, fingerprint verification, diffs, masked reads, the retention
sweep and the deletion-request collection are otherwise unchanged.

- `GET /datasets/{dataset}/deletion-compliance-export` — read-only
  cross-version export of the dataset's whole snapshot deletion compliance
  state, computed fresh on every read (no caching; nothing is written,
  modified or deleted, and no deletion request, deletion proof, snapshot or
  retention policy is touched). The path carries only the dataset name; the
  request takes no body and no query parameters — any body bytes, including a
  purely whitespace or single-space body, or any query parameter → `422`; an
  unknown dataset → `404`, with the same 404-before-422 precedence as the
  privacy compliance export. A dataset without schema versions returns `200`
  with an empty `versions` array and all-zero totals, never an error. The JSON
  document is deterministic (fixed key order, compact whitespace, exactly one
  trailing newline):

  ```json
  {"dataset":"orders","versions":[{"version":1,"retention_days":30,"deletion_requests":[...],"proof_chain":{"count":2,"sequence_range":[1,2],"valid":true}}],"totals":{"version_count":1,"deletion_request_count":2,"confirmed_request_count":1,"proof_count":2}}
  ```

  The top-level keys are exactly `dataset`, `versions`, `totals` in that
  order. `versions` is ordered by version number ascending and each entry has
  exactly `version`, `retention_days`, `deletion_requests` and `proof_chain`
  in that order. `retention_days` is the registered retention policy's day
  count or `null` when no retention policy is registered (the key is never
  omitted). `deletion_requests` lists every deletion request of the version
  across all of its snapshots, sorted by request `id` ascending; pending,
  blocked and confirmed requests are all retained, each with exactly the same
  fields as the per-snapshot deletion-request read (`id`, `snapshot_id`,
  `policy_id`, `reason`, `status`, `impacted`, `created_at`; no
  `confirmed_at`). `proof_chain` has exactly `count`, `sequence_range` and
  `valid`: `count` is the number of proofs already written, `sequence_range`
  is `[first, last]` over the stored proof sequences and is `[null, null]`
  when the chain is empty, and `valid` follows exactly the deletion-proof
  verification read's criteria (an empty chain is valid). `totals` has
  exactly `version_count`, `deletion_request_count`,
  `confirmed_request_count` and `proof_count`, each the sum of the
  per-version values over the whole dataset.
- `GET /datasets/{dataset}/snapshot-scale-summary` — read-only cross-version
  summary of the current row-count scale of the dataset's snapshots, computed
  fresh on every read (no caching; nothing is written, modified or deleted,
  and no snapshot, row, fingerprint, diff cache or deletion-proof record is
  touched). Only currently persisted snapshots are counted: a confirmed-deleted
  snapshot no longer appears and its rows count toward no statistic. The path
  carries only the dataset name; the request takes no body and no query
  parameters — any body bytes, including a purely whitespace or single-space
  body, or any query parameter → `422`; an unknown dataset → `404`, checked
  before the request shape, with the same 404-before-422 precedence as the
  deletion compliance export. A dataset without schema versions, or one whose
  versions have no snapshots, returns `200` normally, never an error. The
  JSON document is deterministic (fixed key order, compact whitespace,
  lowercase booleans, exactly one trailing newline; the same data renders
  byte-identically after a process restart and ordering never depends on the
  database's natural order):

  ```json
  {"dataset":"orders","versions":[{"version":1,"snapshots":[{"snapshot_id":1,"row_count":12,"created_at":"2026-01-01T00:00:00+00:00"},{"snapshot_id":2,"row_count":3,"created_at":"2026-01-02T00:00:00+00:00"}],"stats":{"snapshot_count":2,"total_row_count":15,"min_row_count":3,"max_row_count":12,"first_created_at":"2026-01-01T00:00:00+00:00","last_created_at":"2026-01-02T00:00:00+00:00"}}],"totals":{"version_count":1,"snapshot_count":2,"total_row_count":15,"min_row_count":3,"max_row_count":12,"first_created_at":"2026-01-01T00:00:00+00:00","last_created_at":"2026-01-02T00:00:00+00:00"}}
  ```

  The top-level keys are exactly `dataset`, `versions` and `totals` in that
  order. `versions` is ordered by version number ascending and each entry has
  exactly `version`, `snapshots` and `stats` in that order. `snapshots`
  lists the version's currently persisted snapshots ordered by snapshot id
  ascending; each entry has exactly `snapshot_id`, `row_count` (the currently
  persisted number of rows) and `created_at` (the timezone-bearing write
  time). `stats` has exactly `snapshot_count`, `total_row_count`,
  `min_row_count`, `max_row_count`, `first_created_at` and
  `last_created_at` in that order: the snapshot count and the row-count sum
  are zero for a version without snapshots, and the minimum/maximum row
  counts and earliest/latest write times are `null` then — the keys are never
  omitted. Several snapshots written at the same instant are ordered by
  snapshot id ascending, so the earliest/latest write times never depend on
  database order. `totals` has exactly `version_count`, `snapshot_count`,
  `total_row_count`, `min_row_count`, `max_row_count`, `first_created_at` and
  `last_created_at`: the first three are the sums of the matching
  per-version values, while the extrema and the time range are taken over
  every currently persisted snapshot of the whole dataset (all `null` when no
  snapshot exists).

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
- `POST /datasets/{dataset}/versions/{version}/processing-tasks/lease-dispatch` —
  leased worker claim. Body: `{"worker_id": "<non-empty>", "lease_seconds":
  <positive integer>, "limit": <optional positive integer, default 1>}`.
  Selection and eligibility mirror the plain `dispatch` (task id ascending,
  `pending` or `failed` with attempts left, all dependencies `succeeded` at
  selection time, no same-request chaining); within a single transaction up to
  `limit` startable tasks get their next-attempt `running` run, each carrying
  `lease_id` (unique within the dataset), `worker_id` and `lease_expires_at`
  (exactly the request time plus `lease_seconds`). Returns `201` with
  `{"dataset", "version", "runs"}` (empty `runs` when nothing is startable);
  each run has the usual run fields plus the three lease fields. Concurrent
  lease dispatches (and interleavings with the lease-free starts) never
  duplicate or skip an attempt and never leave two running runs of one task.
  Unknown dataset/version → `404`; a missing/blank/non-string `worker_id`, a
  non-integer (including boolean) or non-positive `lease_seconds`/`limit`,
  extra body fields or any query parameter → `422` and nothing is written.
- `POST /datasets/{dataset}/versions/{version}/processing-tasks/runs/{run_id}/lease-heartbeat` —
  extend a running run's lease. Body contains only `lease_id` and
  `lease_seconds` (a positive integer); the new `lease_expires_at` is the
  request time plus `lease_seconds`. Only the non-expired holder of the run's
  lease may renew: an ended run, a lease id that does not match the run's
  active lease (unknown, missing or another worker's) or an already expired
  lease → `409` and nothing is written. Unknown dataset/version/run → `404`
  (a run of another dataset/version is out of scope); invalid body fields or
  any query parameter → `422`. Returns the run with its lease fields.
- `POST /datasets/{dataset}/versions/{version}/processing-tasks/runs/{run_id}/lease-complete` —
  finish a leased run. Body contains only `lease_id`, `status`
  (`succeeded`/`failed`) and the same `error` rules as the plain finish
  endpoint (a success must not carry one, a failure requires a non-empty one).
  Only the non-expired lease holder may write the terminal state: an ended
  run, a mismatched or expired lease → `409` and nothing is written. On
  success the run gets `finished_at` and its terminal status and the task
  moves to the same status atomically (a `failed` task stays retryable while
  attempts remain); the lease triple stays on the run as part of its record.
  Unknown dataset/version/run → `404`; invalid body fields or any query
  parameter → `422`. Returns the run with its lease fields.
- `POST /datasets/{dataset}/versions/{version}/processing-tasks/reclaim-leases` —
  fail every still-`running` run of the version whose lease expired by a given
  instant. Body contains only `as_of`, an ISO-8601 date-time with a timezone.
  Each expired run atomically becomes `failed` with `finished_at` written and
  `error` fixed to `lease expired`, and its task moves to `failed`: with
  attempts left the task stays dispatchable, once exhausted it is not. Runs
  without a lease (started through the lease-free entry points) never expire
  and are untouched, as are runs whose lease is still valid, so a repeated
  reclaim changes nothing. Returns `{"dataset", "version", "runs"}` with the
  reclaimed runs sorted by run id ascending, each carrying the run and lease
  fields. Unknown dataset/version → `404`; a missing/malformed/timezone-less
  `as_of`, extra body fields or any query parameter → `422` and nothing is
  written.
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
- `GET /datasets/{dataset}/processing-audit-export` — read-only cross-version
  export of the dataset's whole processing audit state, computed fresh on
  every read (no caching; nothing is written, modified or deleted, and no
  task, run or audit record is touched). The path carries only the dataset
  name; the request takes no body and no query parameters — any body bytes,
  including a purely whitespace or single-space body, or any query parameter
  → `422`; an unknown dataset → `404`, with the same 404-before-422
  precedence as the compliance exports. A dataset without schema versions
  returns `200` with an empty `versions` array and all-zero totals, never an
  error. The JSON document is deterministic (fixed key order, compact
  whitespace, exactly one trailing newline):

  ```json
  {"dataset":"orders","versions":[{"version":1,"task_count":3,"run_count":3,"invalid_audit_runs":0,"terminal_tasks":{"succeeded_tasks":1,"failed_tasks":1,"exhausted_tasks":1}}],"totals":{"version_count":1,"task_count":3,"run_count":3,"invalid_audit_runs":0,"succeeded_tasks":1,"failed_tasks":1,"exhausted_tasks":1}}
  ```

  The top-level keys are exactly `dataset`, `versions`, `totals` in that
  order. `versions` is ordered by version number ascending and each entry has
  exactly `version`, `task_count`, `run_count`, `invalid_audit_runs` and
  `terminal_tasks` in that order; only counts and conclusions are exported,
  never the per-task or per-run details of the per-version audit report.
  `invalid_audit_runs` counts runs whose audit chain fails re-verification
  under exactly the same criteria the per-version report and the per-run
  verify read use (recomputed evidence hashes, continuous sequences from `1`
  and record-by-record linkage); a tampered or broken chain counts the run as
  invalid while the task and run details stay readable through the existing
  endpoints. `terminal_tasks` has exactly `succeeded_tasks`, `failed_tasks`
  and `exhausted_tasks` — the succeeded and failed task counts plus the
  subset of failed tasks that have used up `max_attempts` (the report's
  exhausted criterion); pending and running tasks appear nowhere in the
  terminal distribution. `totals` has exactly `version_count`,
  `task_count`, `run_count`, `invalid_audit_runs`, `succeeded_tasks`,
  `failed_tasks` and `exhausted_tasks`, each the sum of the matching
  per-version values over the whole dataset.
- `GET /datasets/{dataset}/processing-blocker-summary` — read-only
  cross-version summary of what keeps each version's processing tasks from
  proceeding, computed fresh on every read (no caching; nothing is written,
  modified or deleted, and no task, run, dependency or audit record is
  touched). The path carries only the dataset name; the request takes no
  body and no query parameters — any body bytes, including a purely
  whitespace or single-space body, or any query parameter → `422`; an
  unknown dataset → `404`, checked before the body/query validation. A
  dataset without schema versions returns `200` with an empty `versions`
  array and all-zero totals, never an error. The JSON document is
  deterministic (fixed key order, compact whitespace, exactly one trailing
  newline). The top-level keys are exactly `dataset`, `versions` and
  `totals` in that order; `versions` is ordered by version number ascending
  and each entry has exactly `version`, `tasks`, `ready_count`,
  `running_count`, `succeeded_count`, `failed_count`, `exhausted_count` and
  `blocked_count` in that order. `tasks` is ordered by task `id` ascending;
  each task has exactly `id`, `name`, `status` (the stored task status),
  `schedule_state`, `blocking_task_ids`, `cause` and `causes` in that
  order. `schedule_state` uses the same seven literals and the same
  derivation as the per-version schedule view (`running`, `succeeded`,
  `retryable`, `exhausted`, `ready`, `blocked`, `upstream_failed`);
  `blocking_task_ids` likewise lists the not-yet-succeeded direct
  dependencies of a pending task, id-ascending and de-duplicated, and is an
  empty array for every non-pending task. The blocker categories are
  `direct_dependency` (a pending task still waiting on an unsucceeded
  direct dependency), `upstream_failed` (a dependency chain is exhausted or
  upstream-failed, at any distance) and `attempts_exhausted` (a `failed`
  task that has used up `max_attempts`); the categories are independent, so
  a pending task whose dependency chain failed hits both
  `direct_dependency` and `upstream_failed`. `causes` lists every category
  that applies in alphabetical order (an empty array when none applies),
  and `cause` is the single highest-priority category —
  `attempts_exhausted`, then `upstream_failed`, then `direct_dependency` —
  or `null` when no category applies. The six counts partition the seven
  schedule states into mutually exclusive buckets (the counts always sum to
  the version's task count): `ready`, `running`, `succeeded` and `exhausted`
  keep their own buckets, a `retryable` task is counted as `failed`, and
  `blocked` together with `upstream_failed` is counted as `blocked`.
  `totals` has exactly `version_count`, `task_count` and the six
  `*_count` counters, each the sum of the matching per-version values.

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

### Processing run snapshot evidence bindings (append-only proof chain)

A run can bind the snapshots it consumes and produces as evidence. Each
binding pins one existing, non-deleted snapshot of the run's dataset to the
run as `input` or `output`; bindings form an independent append-only,
tamper-evident hash chain per run (separate from the run's audit-record
chain, which they never read or modify), numbered from `1` per run, linked
through SHA-256 hashes and persisted across restarts. The database rejects
updates and deletes of bindings, and there are no per-binding HTTP routes.

- `POST /datasets/{dataset}/versions/{version}/processing-tasks/{task_id}/runs/{run_id}/snapshot-bindings` —
  bind one snapshot. Body contains exactly `role`, `version` and
  `snapshot_id`: `{"role": "input", "version": 1, "snapshot_id": 7}`. `role`
  is exactly `input` or `output` (no trimming); `version` and `snapshot_id`
  are integers (booleans rejected) naming a version of the path dataset and
  a snapshot that currently exists in it. Returns `201` with `id`,
  `sequence` (continuous from `1` within the run), `role`, `dataset`,
  `version`, `snapshot_id`, `run_status` (the run's status at bind time:
  `running`/`succeeded`/`failed`), `previous_hash` (`null` for the first
  binding, otherwise the previous binding's `evidence_hash`),
  `evidence_hash` and `created_at`. A run may carry many bindings; the same
  snapshot may be bound once per role, and a second binding of the same
  `(role, snapshot_id)` to the run returns `409` and writes nothing. The
  bound snapshot may belong to any version of the same dataset (the path
  version only names the task/run). The path dataset/version/task resolves
  first (`404`); a missing, extra or wrongly typed field, an illegal `role`,
  an empty/malformed/non-object body or any query parameter is then a `422`;
  the run must then exist (`404`) and belong to the path task (`422`,
  including a run of a task in another version); only afterwards is the
  body's version/snapshot resolved — a version missing from the dataset, a
  snapshot that does not exist in it or one already confirmed deleted is a
  `404`. Every rejection performs zero writes and never changes the run's
  audit-record chain. Concurrent binds never reuse or skip a sequence.
- `GET .../runs/{run_id}/snapshot-bindings` — list the run's bindings in
  ascending `sequence` order (an empty chain is `[]`). Same `404`/`422`
  path rules; the read takes no body and no query parameters (`422`,
  checked after the path resolves).
- `GET .../runs/{run_id}/snapshot-bindings/verify` — verify the chain,
  returning `{"dataset", "version", "task_id", "run_id", "valid",
  "checked_count", "problems"}`. Verification recomputes every
  `evidence_hash`, checks that `sequence` values are continuous from `1`,
  that the first `previous_hash` is `null` and every later one equals the
  preceding binding's `evidence_hash`, and that each bound snapshot still
  exists. `problems` is sorted by `sequence` then `code`; each item has
  exactly `sequence`, `binding_id` and `code`, and `code` is one of
  `sequence_gap`, `previous_hash_mismatch`, `hash_mismatch` and
  `snapshot_deleted`. Deleting a snapshot after it was bound never changes
  or removes the binding: the chain stays complete and verify reports
  `snapshot_deleted` with `"valid": false`. An empty chain is valid with
  `"checked_count": 0` and no problems. The endpoint takes no body and no
  query parameters (`422`); unknown dataset/version/task/run → `404`, a run
  owned by another task → `422`, and an unknown path → `404`.

A binding's `evidence_hash` is the lowercase hexadecimal SHA-256 of a
canonical JSON document built from `sequence`, `role`, `dataset`,
`version`, `snapshot_id`, `run_status` and `previous_hash` (excluding `id`,
`created_at` and `evidence_hash` itself): keys are sorted by Unicode code
point, no insignificant whitespace is emitted and the text is UTF-8 encoded.

