# Unsloth Studio Cloud Jobs — plan

Fork of Unsloth Studio where one central UI configures, launches, schedules and watches training jobs on
cloud GPUs (Google Cloud, AWS, or any Kubernetes cluster) through SkyPilot. Everything about a job
(config, live progress, loss curves, samples, logs, status, cost, outputs) is seen and controlled from that UI.

Develop on the laptop first; then run the same build on a normal VM as the main server.

## 1. Architecture

```
 Browser ──> Central server (normal VM, Linux)                      Cloud GPU (SkyPilot-managed)
             ├─ Forked Studio (frontend + FastAPI backend)          ├─ remote runner
             │    ├─ /api/cloud-jobs  (create/list/cancel/rerun)    │    ├─ Studio's unchanged worker
             │    ├─ scheduler loop   (now / at time / cron)        │    └─ event forwarder
             │    ├─ event ingest     (bucket poller)               │
             │    └─ job store        (SQLite)                      │
             └─ SkyPilot API server (same VM) ──launch/cancel──>    │
                                                                    │
             Storage bucket (GCS):  jobs/<id>/config.json  events/*.jsonl  checkpoints/  outputs/
```

- The central server never needs inbound access from the GPU: runners write events to the bucket,
  the server polls it (every ~5 s). Works the same for Google Cloud, AWS and Kubernetes runners.
- One storage bucket holds job configs, event logs, checkpoints and final outputs.

## 2. Components

### A. Central server (normal VM)
- Ubuntu VM in the `helektron` project (c2d-standard-4 Mumbai, ~$0.12/hr, 200 GB disk), stopped when idle.
- Runs the forked Studio and a SkyPilot API server on the same machine.
- Access from the browser via SSH tunnel (no public port) at first; later HTTPS behind Studio's own auth.
- Holds cloud credentials (GCP service account; AWS keys for `helektron` if AWS is used; kubeconfig for k8s).

### B. Launcher (`/api/cloud-jobs`, backend)
- `POST /api/cloud-jobs`: takes the same body as `POST /api/train/start` (Studio's full training request)
  plus `resources` and `schedule`:
  - `resources`: `infra` (`gcp` | `aws` | `kubernetes`), region (optional), `accelerator` (e.g. `H100:1`,
    `B200:1`), `use_spot`, disk size.
  - `schedule`: `now` | ISO time | cron string (parsed by Studio's existing `core.scheduled_tasks.schedule`).
- Builds the worker config with Studio's own `_build_training_worker_config`, uploads it to
  `jobs/<id>/config.json`, then calls SkyPilot to launch a managed job (spot recovery on).
- `GET /api/cloud-jobs`, `GET /api/cloud-jobs/{id}`, `POST .../{id}/cancel`, `POST .../{id}/rerun`.
- Job store: SQLite table (id, name, request JSON, resources, schedule, state, SkyPilot job id,
  created/started/ended, cost estimate, output path). Reuse Studio's `storage/` patterns.

### C. Scheduler
- Background loop in the backend lifespan: launches due jobs, honours a max-concurrent-jobs setting,
  re-arms cron jobs, retries a failed launch (e.g. no capacity) with backoff, falls back across listed regions.

### D. Remote runner (runs on the GPU)
- SkyPilot `setup`: install Unsloth + this fork's backend package (pinned to the fork commit).
- `run`: `python -m cloud_jobs.runner --job <id>`:
  1. Reads `jobs/<id>/config.json` from the bucket.
  2. Starts Studio's unchanged training worker with that config and an event queue.
  3. Forwards every event (`progress`, `complete`, `error`, `warning`, `stall`, `model_load_*`, `status`,
     `output_dir`, `image`, `eval_configured`) as JSON lines to `jobs/<id>/events/<seq>.jsonl`
     (batched every few seconds) plus a heartbeat.
  4. Checkpoints to `jobs/<id>/checkpoints/` (bucket mount) and resumes from the newest one after a spot
     reclaim (Studio's `core/training/resume.py`).
  5. Final adapter/merged model to `jobs/<id>/outputs/`.

### E. Event ingest → existing UI
- Poller reads new event files per running job and stores them in a per-job event log (SQLite/JSONL).
- Each event is applied to the job's state through the same handler code Studio uses for local runs
  (`TrainingBackend._safe_handle_event` logic), so progress, metrics, loss curves and samples come out in
  the exact shapes the existing training view expects.
- `GET /api/cloud-jobs/{id}/progress` (SSE) and `/metrics` mirror `/api/train/progress` and
  `/api/train/metrics`, so the existing charts/components render a cloud job by switching their data source
  to the job id.
- Finished jobs are written to Studio's training history so they show up with local runs.

### F. Frontend (Studio fork)
- Training page: a "Run on: This machine / Cloud" switch next to Train. Cloud shows: infra, GPU, spot,
  region, schedule (now / at / repeat). Submits to `/api/cloud-jobs` instead of `/api/train/start`.
- New "Cloud jobs" page: table of jobs (name, model, GPU, infra, state, progress %, elapsed, cost,
  scheduled time), actions (open, cancel, rerun, clone config).
- Opening a job: the existing training view (progress, loss chart, samples, logs) bound to that job.
- Live logs panel: SkyPilot job logs streamed through the backend.
- Settings: bucket, default infra/region/GPU, max concurrent jobs, cloud credentials status (`sky check`).

### G. Samples
- Use whatever the worker already emits (`image` events for vision/diffusion, eval results).
- Add (phase 4): text samples for LLM runs — at each checkpoint, generate answers to a few fixed prompts
  and emit them as a `sample` event shown in the job view.

## 3. Development (local first, on the laptop)

- Code: branch `cloud-jobs` in `Desktop/ob/unsloth-sched/unsloth`, changes confined to `studio/` plus a
  new `studio/backend/cloud_jobs/` package. Push to a GitHub repo of ours later.
- Studio runs on Windows (it ships `setup.ps1`). SkyPilot does not run on Windows, so locally it runs as an
  API server in Docker Desktop and the backend talks to it over HTTP.
- Local stand-ins so the whole loop is testable without cloud spend:
  - Storage: a local folder instead of the bucket (same layout, one setting switches to GCS).
  - Runner: started as a local process on the RTX 4050 with a tiny model, writing to that folder.

## 4. Phases and acceptance checks

| Phase | Builds | Done when |
|---|---|---|
| 0. Spikes | Verify: Studio runs on the laptop; worker runs headless from a config dict; SkyPilot API server in Docker + its HTTP API from Windows | All three work, or a workaround is chosen |
| 1. Bridge (local) | Runner + local-folder events + ingest + `/api/cloud-jobs/{id}/progress` | A local tiny-model run shows live progress, loss, logs, completion in the Studio UI via the bridge |
| 2. Launcher + store (local) | `/api/cloud-jobs` CRUD, SQLite store, SkyPilot launch via Docker API server, bucket mode | Submit from the UI → job runs on a cheap cloud GPU → events in the UI → cancel works |
| 3. Scheduler | now / at time / cron, max concurrency, retry + region fallback | Two jobs queued with limit 1 run in order; a cron job re-arms |
| 4. UI polish + samples | Cloud jobs page, settings, logs panel, LLM text samples, history integration | All job info visible and controllable from the UI only |
| 5. Main server | VM in `helektron`, Studio + SkyPilot API server, credentials, tunnel access | Same flows work from the VM; laptop not needed |
| 6. Real use | Qwen-concept-27B QLoRA on the 67K mix | Run completes from the UI, outputs in the bucket, benchmarks pass |

## 5. Costs

- Central VM: ~$0.12/hr (c2d-standard-4) + ~$20/month disk; stop when idle.
- Bucket: a few $/month for data, checkpoints, models.
- Test GPU runs in phase 2–3: a few dollars (small models, spot).
- Real training: per run (e.g. Qwen 27B one pass ≈ $25–60 depending on GPU).

## 6. Risks

- `core/training/training.py` is large and stateful (account ownership, single-job slot); the bridge must
  reuse its event handling without touching local-run behaviour.
- Upstream Unsloth moves fast; keep the fork's changes isolated to ease rebases.
- Studio's worker may assume local paths/caches; the runner must map them to the remote disk/bucket.
- SkyPilot client doesn't run on Windows; the HTTP API route must cover launch/cancel/logs/queue.
- Cross-cloud storage (AWS runner writing to a GCS bucket) needs credentials on the runner.
- License: Studio is AGPL-3.0; fine internally, source must be shared if offered as a service to outsiders.

## 7. Not in v1

Multi-user permissions, multi-GPU/multi-node jobs, auto hyperparameter sweeps, cost budgets per user,
serving/deploying trained models (done separately).
