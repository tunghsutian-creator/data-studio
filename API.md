# Local API contract

All endpoints are served from `http://127.0.0.1:8765/api`.

## Read endpoints

- `GET /health` - service, catalog and local-mode status.
- `GET /config` - safe public paths and thresholds; no secrets.
- `GET /summary` - dataset/file counts, bytes and confidence/status totals.
- `GET /filters` - available projects, material states, modalities and formats.
- `GET /datasets` - paginated/searchable dataset rows.
- `GET /datasets/{id}` - dataset, grouped files, classification evidence and
  operation history.
- `GET /jobs` / `GET /jobs/{id}` - scan and commit status.
- `GET /rules` - human-readable local rule registry and versions.
- `GET /ai/health` - explicit enabled/available/worker state, queue counts and
  the public locked model identity. Rules-only mode returns `status=disabled`.
- `GET /ai/tasks` / `GET /ai/tasks/{id}` - durable AI queue state and versioned
  attempt history. Optional filters are `dataset_id`, `status` and `limit`.
- `GET /datasets/{id}/ai` - the latest AI tasks, suggestions, evidence and model
  versions for one dataset.
- `GET /collections` / `GET /collections/{id}` - named, library-scoped asset
  snapshots. Responses contain asset UUIDs and display metadata, never paths.
- `GET /exports` / `GET /exports/{id}` - durable queue state, selected item
  facts and the verified local output location.
- `GET /exports/{id}/manifest` - the authoritative manifest, returned only
  after all recorded checksums are reverified.

## Mutation endpoints

- `POST /scan` with `{ "source": "reference" | "inbox" }`.
- `PATCH /datasets/{id}` for reviewed metadata and canonical name.
- `POST /datasets/{id}/accept` to approve classification and, for inbox data,
  create a verified managed copy.
- `POST /datasets/{id}/defer` to keep the source indexed without copying.
- `POST /datasets/{id}/ai/analyze` to enqueue a manual, idempotent local AI
  suggestion. The body accepts `reason`, `priority` and `max_attempts`; it never
  accepts a path or model endpoint.
- `POST /collections`, `PATCH /collections/{id}`,
  `POST /collections/{id}/items` and
  `DELETE /collections/{id}/items/{asset_id}` manage ordered asset UUIDs. A
  create request may include a ready `selection_token` to save the complete
  immutable preview atomically without sending paths or re-expanding in the
  browser.
- `POST /exports/preview` expands explicit `asset_ids` and/or `dataset_ids`, or
  one normalized server-side filter, into an immutable selection snapshot.
  Explicit IDs may be mixed and are de-duplicated after dataset expansion;
  filter selections may include `excluded_asset_ids`.
- `POST /exports` consumes one unexpired, exportable `selection_token`. The body
  also supplies `name`, optional `purpose`/`collection_id`, `export_mode`
  (`FOLDER`, `ZIP64`, or `MANIFEST_ONLY`) and `duplicate_policy` (`PRESERVE` or
  `DEDUPLICATE`). It returns a durable `QUEUED` job with HTTP 202.

The API never accepts an arbitrary filesystem path from the browser. Source
roots are resolved from the local configuration allowlist.

AI endpoints expose only allowlisted task, run and model fields. Worker lease
owners, model paths, source paths, filenames and provider credentials are not
part of the browser contract. An AI run writes only `ai_tasks`/`ai_runs`; it
does not update dataset classification or perform a filesystem operation.

## Local AI analyze response

`POST /datasets/{id}/ai/analyze` returns `202` after the durable task has been
created (or an equivalent active input has been found):

```json
{
  "id": "task-uuid",
  "dataset_id": "dataset-uuid",
  "input_fingerprint": "64-lowercase-hex",
  "reason": "MANUAL_REQUEST",
  "status": "QUEUED",
  "attempt_count": 0,
  "max_attempts": 2,
  "created": true
}
```

The worker transitions the task through `QUEUED`, `RUNNING`, optional
`RETRY_WAIT`, and one of `COMPLETED`, `ABSTAINED`, `FAILED` or `CANCELLED`.
Repeated requests with the same dataset and evidence fingerprint reuse the
active task instead of starting concurrent inference.

When `ai_enabled` and `ai_auto_inbox_enabled` are both true, a completed Inbox
scan may also enqueue a lower-priority task for unknown, conflicting,
low-confidence or pixel-bearing `REVIEW` datasets. The threshold is
`ai_trigger_confidence_threshold` (default `0.8`). Automatic scheduling is
skipped for partial/error scans and never applies to reference or already
accepted datasets. Re-scanning unchanged input does not repeat a successful run
for the same registered model; manual **重新分析** remains an explicit new run.

## Export preview contract

Preview resolves the selection at one monotonic catalog revision, prefers a
verified managed copy, then rechecks existence, size and SHA-256 through the
configured root map. Missing, stale, unresolved or hash-mismatched assets make
the preview non-exportable. Duplicate hashes and filename collisions are
warnings; every explicitly selected asset remains present.

The response contains a random 15-minute `selection_token`, the catalog and
selection digests, counts, issue codes, and a path-free asset summary. Only the
token SHA-256 is stored in SQLite. If dataset or asset facts change while files
are being verified, preview returns HTTP 409 and persists no snapshot. A later
export must recheck every byte before consuming the token; this endpoint does
not write an export.

The single-concurrency export worker rechecks the selected catalog facts and
source SHA-256 before writing, verifies source stability after writing, and
verifies every output listed in `checksums.sha256`. Folder and manifest-only
outputs are committed by same-volume directory rename; ZIP uses ZIP64 and a
same-volume file rename. Existing destinations are never overwritten. A
process restart requeues `RUNNING` jobs and can reconcile an already-renamed,
fully verified output without rewriting it.

## Dataset row shape

```json
{
  "id": "uuid",
  "canonical_name": "PA_ADR_RECYCLE_VIRGIN_TENSILE_E0_260402_A7F2",
  "workstream": "PA_ADR_RECYCLE",
  "material_state": "VIRGIN",
  "sample_code": "E0",
  "modality": "TENSILE",
  "file_count": 5,
  "confidence": 0.99,
  "classification_method": "rule",
  "status": "REVIEW",
  "updated_at": "2026-07-14T16:00:00+08:00"
}
```
