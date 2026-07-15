# Phase 3 gold-set workflow

The gold set is a dataset-level, human-labelled evaluation asset. Rule output,
AI output, filenames, folder names and canonical names are never accepted as
ground truth by the preparation tool.

All generated files contain catalog-derived identifiers and must remain outside
the Git repository. The CLI refuses repository, raw-data, catalog, model,
backup and export destinations. The Git safety hook independently rejects the
standard gold-set output names.

## 1. Prepare a blinded candidate bundle

```powershell
& "C:\research data\academic-vault\.venv\Scripts\python.exe" `
  .\scripts\gold-set.py prepare `
  --catalog "C:\research data\catalog\academic_vault.sqlite3" `
  --output-dir "C:\research data\evaluation\gold-set-phase3-v1" `
  --target 180 `
  --seed phase3-v1
```

Preparation opens the catalog immutable/read-only, refuses an active WAL,
runs SQLite integrity and foreign-key checks, requires valid asset digests, and
selects deterministically across modality/confidence/method/pixel strata. All
Unknown, conflict and very-low-confidence cases are mandatory when they fit the
declared target.

The output directory is created once and is never overwritten:

- `blind-review.csv` — the only file used during human labelling. It contains
  stable IDs, an evidence fingerprint and empty label fields; it contains no
  rule prediction, path or filename.
- `candidate-audit.json` — sealed sampling/provenance evidence. It contains
  rule predictions only to explain the strata, explicitly not as labels. Keep
  it closed while labelling.
- `README.txt` — review instructions.

## 2. Perform human review

For each row, inspect the dataset locally by `dataset_id` and set:

- `decision=INCLUDE`, a taxonomy-valid `gold_modality`, `reviewer`, and a
  timezone-aware `reviewed_at`; or
- `decision=EXCLUDE`, `reviewer`, `reviewed_at`, and `exclusion_reason`.

Do not infer the label from a directory, canonical name, rule prediction or AI
suggestion. The evidence fingerprint binds the review row to the selected asset
manifest without exposing a path.

## 3. Validate before benchmarking

```powershell
& "C:\research data\academic-vault\.venv\Scripts\python.exe" `
  .\scripts\gold-set.py validate `
  --review "C:\research data\evaluation\gold-set-phase3-v1\blind-review.csv" `
  --audit "C:\research data\evaluation\gold-set-phase3-v1\candidate-audit.json" `
  --minimum-included 150
```

Validation fails closed on pending rows, underfilled labels, invalid taxonomy,
missing reviewer/timestamp, duplicate IDs, exclusions without reasons, changed
evidence fingerprints or a modified audit manifest. Only a successful validator
result may be used by the production benchmark.
