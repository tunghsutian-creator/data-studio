# Reference corpus audit

Source: `C:\Research Data\data ref`

Audit date: 2026-07-14. The audit was read-only.

## Inventory

- 822 files in 122 directories, 2.292 GiB.
- 272 CSV, 243 TIF/TIFF, 139 TXT, 44 PDF, 37 XLSX.
- 29 PNG/JPEG/BMP images, 27 native tensile project files, plus Origin and
  simulation formats.
- One empty native tensile file.
- 87 duplicate-name groups involving 214 files.
- 32 exact-content duplicate groups involving 65 files.

The 243 TIF/TIFF assets account for roughly 85% of storage. The corpus is small
enough for a local catalog and lightweight classifier; storage and compute are
not current bottlenecks.

## Normalized modality labels

| Label | Approximate files | Notes |
| --- | ---: | --- |
| SEM | 341 | Original TIF, TXT sidecars and processed derivatives |
| TENSILE | 262 | CSV, workbook, native project and report bundles |
| FTIR | 69 | Mostly two-column spectra plus summary outputs |
| RHEOLOGY | 50 | Frequency, relaxation, creep and temperature tests |
| SIMULATION | 32 | Multi-file solver/input/output bundles |
| REFERENCE | 25 | Documents and supporting data |
| IMPACT | 22 | Image, workbook and report bundles |
| TORQUE | 13 | Instrument TXT exports |
| OPTICAL | 4 | Too few independent groups for model training |
| GPC | 2 | Rule/manual review only for now |

SEM and tensile make up about 73% of files. Overall accuracy is therefore not a
valid success metric; evaluation must report per-class precision/recall,
macro-F1, automatic-accept precision, coverage and abstention rate.

## Strong signatures

- SEM sidecars: `[SemImageFile]`, `InstructName=TM4000`, same-stem TIF pairing.
- Tensile: `.is_tens` / `.id_tens`; CSV contains `结果表格`, 拉伸应力, 拉伸应变,
  模量 and rate/unit rows.
- Rheology: `Project:`, `Test:`, `Result:`, `Frequency Sweep`, `Storage Modulus`,
  `Stress Relaxation`.
- Torque: `Screw Torque`, `Screw Speed`, `Temp. Front`, `Melt Viscosity`.
- FTIR: long two-column numeric spectra with a plausible wavenumber axis.
- Simulation: stable native extensions and shared job prefixes.
- GPC: workbook sheets such as `Sample Details`, `Raw Data`, `MW Results`.

## Corpus caveats

- Directory names are weak labels, not ground truth.
- Full paths must not be classifier features; they contain the answer.
- Related files and exact duplicates must stay in the same validation fold.
- SEM original/sidecar/processed assets are one acquisition group.
- Tensile repetitions from one export directory are one experimental batch.
- `E2.csv` under a relaxation folder contains internal `Test: E4`; this conflict
  must remain reviewable rather than being silently corrected.
- Synonyms and typos need aliases: `SEM/sem`, `impact resistence`, `pinlv/freq`,
  `stresss relaxation/STREE RELAXATION/松弛`.
- Lifecycle folders such as `archive`, `old one`, `DATA collection` and
  `sem_processed_*` are not scientific modalities.

## Release policy

1. Index every file and preserve the original path.
2. Use rules to identify strong known formats.
3. Group acquisition packages before training or review.
4. Train only a lightweight local fallback model on filename/header/structure
   features, excluding target-bearing directories.
5. Keep GPC, optical and other low-sample categories in mandatory review.
6. Do not enable automatic managed copying for a category until its prospective
   high-confidence precision reaches at least 98%.

