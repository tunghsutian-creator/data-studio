# Academic Vault

Academic Vault is a local-first research-data catalog for Windows. It indexes
heterogeneous instrument files, groups related assets into datasets, explains
classification decisions, and exposes a browser-based review workflow.

The first release is intentionally conservative:

- reference data is indexed in place and never renamed;
- inbox data is copied, not moved, after approval;
- raw files are never overwritten;
- every managed copy is verified with SHA-256;
- ambiguous classifications stay in the review queue.

## Default local layout

```text
C:\Research Data\
  data ref\                 # read-only reference corpus
  inbox\                    # drop new data here
  vault\                    # approved managed copies
  quarantine\               # reserved for failed/unknown imports
  catalog\academic_vault.sqlite3
  models\                   # local classifier artifacts
  academic-vault\           # installed application
```

The application binds to `127.0.0.1` only. It does not require a cloud API.

## Development layout

- `backend/`: FastAPI, SQLite catalog, scanners, ingestion and local classifier
- `frontend/`: React + Vite management UI
- `design/`: accepted product concept and design specification
- `scripts/`: Windows bootstrap, build and start scripts

Setup and run instructions are added by the bootstrap script once dependencies
have been installed.

## Start on this computer

The installed copy is self-contained under `C:\Research Data\academic-vault`.
Double-click `Start Academic Vault.cmd`; the service listens only on
`http://127.0.0.1:8765`. Close it with `Ctrl+C` in the launcher window.

See `QUICKSTART_CN.md` for the Chinese quick guide and safety boundaries.
