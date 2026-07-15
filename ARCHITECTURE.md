# Architecture

## Safety boundary

The catalog and the filesystem cannot share a single transaction. Managed
copies therefore use a prepared/verified/committed protocol:

1. create an operation record;
2. copy to a temporary file inside the destination volume;
3. stream SHA-256 and compare with the source digest;
4. atomically rename the temporary file;
5. commit catalog paths and the operation result;
6. retain the source by default.

Reference scans never copy, move, rename or delete source files.

## Classification order

1. file signature and extension;
2. instrument header and column/unit signatures;
3. same-stem and acquisition-package relationships;
4. filename tokens;
5. local lightweight model;
6. human review / unknown.

Rules emit evidence and a version. Model scores never grant delete or overwrite
authority.

## Local AI provider boundary

`LocalModelProvider` is the only production-facing model interface. It accepts
a deterministic SHA-256 input fingerprint plus bounded evidence already held
in memory; it has no source-path or filesystem mutation API. Provider results
carry the model profile and prompt/taxonomy/schema versions, while the model
registry adds the locked model revision and runtime build.

The llama.cpp implementation is loopback-only and returns a strictly validated
classification. Timeout, unavailable service, rejected request and invalid
model output are distinct typed failures so the persistent worker can apply an
explicit retry or abstention policy. A deterministic fake provider supplies
the same contract for queue, recovery and API tests. Rules-only operation does
not depend on constructing a provider.

## Canonical taxonomy

- workstream: `REFERENCE`, `PA_ADR_RECYCLE`, `D_PA`, `UDC`, `UNKNOWN`
- material state: `VIRGIN`, `RECYCLED`, `UNKNOWN`
- modality: `SEM`, `TENSILE`, `FTIR`, `RHEOLOGY`, `TORQUE`, `IMPACT`, `GPC`,
  `OPTICAL`, `SIMULATION`, `REFERENCE`, `UNKNOWN`
- data level: `RAW`, `NATIVE_EXPORT`, `PROCESSED`, `SUMMARY`, `REPORT`, `UNKNOWN`
- file role: `MEASUREMENT`, `SIDECAR`, `IMAGE`, `WORKBOOK`, `REPORT`,
  `PROJECT_NATIVE`, `UNKNOWN`

## Database entities

- datasets: one scientific acquisition/logical dataset;
- assets: physical files belonging to a dataset;
- classification decisions: prediction, confidence, evidence and resolution;
- ingest jobs: scan and commit progress;
- operation log: append-only filesystem and catalog mutations;
- categories: stable codes and user-facing labels.

`original_path` is immutable. Paths and names are not identifiers; UUIDs and
SHA-256 provide stable identity and integrity.
