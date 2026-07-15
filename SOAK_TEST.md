# Phase 3 durable AI soak

The soak exercises the real Q8 provider through the durable `ai_tasks` and
`ai_runs` state machine. It never runs against the formal catalog directly:
the source catalog is opened read-only and copied with SQLite Online Backup,
then all migrations, task rows and run rows are written only to a disposable
catalog under `C:\research data\evaluation`.

The runner verifies the locked model and vision projector SHA-256, preflights
bounded evidence without persisting paths or filenames, keeps a multi-dataset
queue populated, restarts the worker while queued work exists, drains the queue,
and compares a digest of datasets/assets/decisions/operations before and after.
It also records typed failures, p50/p95 latency and peak GPU memory.

## 1. Start the locked loopback llama.cpp server

Use the exact paths and arguments in `profiles/windows-model-lock.json`. The
server must bind only to `127.0.0.1:8877`, use one parallel request, and load the
Q8 model plus Q8 vision projector from the locked revision. Keep the locked
`--cors-origins localhost --no-cors-credentials` arguments: loopback binding
alone does not prevent an unrelated web page from attempting a cross-origin
request to a local service. Server logs belong under
`C:\research data\evaluation`, never in the repository.

Verify before testing:

```powershell
Invoke-RestMethod http://127.0.0.1:8877/health
```

## 2. Run a bounded smoke first

```powershell
& "C:\research data\academic-vault\.venv\Scripts\python.exe" `
  .\scripts\soak-ai-worker.py `
  --output-dir "C:\research data\evaluation\ai-soak\smoke-v1" `
  --duration-seconds 600 `
  --max-tasks 10 `
  --cases 10 `
  --queue-depth 3 `
  --restart-after 4 `
  --checkpoint-every 1
```

`--max-tasks` makes this a task-count smoke. The output directory is created
once and never overwritten.

## 3. Run the formal two-hour queue gate

```powershell
& "C:\research data\academic-vault\.venv\Scripts\python.exe" `
  .\scripts\soak-ai-worker.py `
  --output-dir "C:\research data\evaluation\ai-soak\formal-v1" `
  --duration-seconds 7200 `
  --cases 30 `
  --queue-depth 5 `
  --restart-after 25 `
  --checkpoint-every 10
```

With no positive `--max-tasks`, the runner must keep feeding work for the full
duration and then drain. `soak-report.json` is checkpointed atomically outside
Git. A passing report requires:

- the duration/task target was met and every task is accounted for;
- the queue drained with no failed run or terminal task;
- the worker restarted and resumed the persisted queue with the same registered
  model identity;
- dataset-, asset-, decision- and operation-owned state did not change;
- p95 latency is at most 30 seconds;
- peak GPU memory is at most 90% of detected VRAM;
- GPU metrics are available, and the final steady-state median has grown by no
  more than the larger of 256 MiB or 3% of VRAM versus the post-warmup median;
- the provider is still available at the end.

This is a stability gate only. Accuracy and Unknown/conflict recall remain
blocked until the separate blinded review contains at least 150 human labels.
