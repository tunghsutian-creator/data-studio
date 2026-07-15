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

Local AI is opt-in (`ai_enabled=false` by default). The FastAPI lifespan owns a
single-concurrency worker service, wakes it after a durable enqueue, and closes
the provider on shutdown or configuration replacement. Enabling AI validates
the repository profile and model lock before swapping runtime state. The scan,
rules and manual review paths remain available when the provider is disabled or
offline. Browser health/task endpoints expose allowlisted fields only; worker
lease ownership and local model/source paths remain internal.

## Durable AI state

Local AI work uses three catalog tables. `model_registry` is an immutable
identity snapshot whose deterministic id includes the model/runtime/prompt
versions and public inference configuration; credential-shaped configuration
keys are rejected. `ai_tasks` owns queue priority, bounded attempts, retry
time, worker lease and terminal state. A partial unique index permits only one
active task per dataset and input fingerprint. `ai_runs` records every attempt,
model identity, request/response fingerprints, latency, validated result or a
sanitized typed error.

Claims run under `BEGIN IMMEDIATE`, so concurrent workers cannot take the same
task. An expired lease marks its unfinished run failed before the task is made
eligible for a later attempt. Completion also checks lease ownership, which
prevents a replaced worker from overwriting a newer result. Unknown model
outputs are persisted as `ABSTAINED`; AI output never changes dataset metadata
or grants filesystem authority directly.

Before enqueue and again before inference, the evidence builder resolves every
asset through its configured portable root, rejects symlinks and path-review
state, verifies size and SHA-256, and checks preview sources again after they
are read. The model receives only a bounded manifest summary, content previews
marked as untrusted data, and in-memory image data URLs. Filenames, relative
directories and absolute paths are deliberately omitted. A manifest digest
keeps arbitrarily large asset sets fingerprinted without putting the full list
into the model context. If bytes or catalog assessment change after enqueue,
the old task terminates without calling the model.

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
- model registry: immutable local model and inference configuration identity;
- AI tasks: durable queue, retry budget and worker lease;
- AI runs: append-only attempt outcomes and evidence-bearing suggestions;
- operation log: append-only filesystem and catalog mutations;
- categories: stable codes and user-facing labels.

`original_path` is immutable. Paths and names are not identifiers; UUIDs and
SHA-256 provide stable identity and integrity.
