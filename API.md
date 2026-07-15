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

## Mutation endpoints

- `POST /scan` with `{ "source": "reference" | "inbox" }`.
- `PATCH /datasets/{id}` for reviewed metadata and canonical name.
- `POST /datasets/{id}/accept` to approve classification and, for inbox data,
  create a verified managed copy.
- `POST /datasets/{id}/defer` to keep the source indexed without copying.
- `POST /datasets/{id}/ai/analyze` to enqueue a manual, idempotent local AI
  suggestion. The body accepts `reason`, `priority` and `max_attempts`; it never
  accepts a path or model endpoint.

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
