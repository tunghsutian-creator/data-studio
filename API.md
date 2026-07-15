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

## Mutation endpoints

- `POST /scan` with `{ "source": "reference" | "inbox" }`.
- `PATCH /datasets/{id}` for reviewed metadata and canonical name.
- `POST /datasets/{id}/accept` to approve classification and, for inbox data,
  create a verified managed copy.
- `POST /datasets/{id}/defer` to keep the source indexed without copying.

The API never accepts an arbitrary filesystem path from the browser. Source
roots are resolved from the local configuration allowlist.

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

