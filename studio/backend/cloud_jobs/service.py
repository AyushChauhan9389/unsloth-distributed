"""Cloud jobs: create, schedule, launch, watch, cancel. One RemoteTrainingBackend per running job."""
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from cloud_jobs import store
from cloud_jobs.folder import JOB_PREFIX, open_folder
from cloud_jobs.launchers import LocalLauncher
from cloud_jobs.remote_backend import RemoteTrainingBackend
from core.scheduled_tasks.schedule import next_run_after, parse_schedule
from core.training.training import _build_training_worker_config
from utils.paths import studio_root

logger = logging.getLogger(__name__)
ACTIVE = ("launching", "running")
LAUNCH_ATTEMPTS = 5
RETRY_BACKOFF_MS = 2 * 60 * 1000  # 2, 4, 8, 16 min between launch attempts


def jobs_root() -> str:
    """Local folder (development, local launcher) or gs://bucket (cloud launchers)."""
    return os.environ.get("UNSLOTH_CLOUD_JOBS_ROOT") or str(studio_root() / "cloud_jobs")


def local_gpu_busy() -> bool:
    """This machine's GPU is taken by a local cloud job or by Studio's own Train run."""
    from core.training.training import get_training_backend
    return (any(j["resources"].get("launcher") == "local" for j in store.list_jobs(ACTIVE))
            or get_training_backend().is_training_active())


def max_concurrent() -> int:
    return int(os.environ.get("UNSLOTH_CLOUD_JOBS_MAX_CONCURRENT", "2"))


def now_ms() -> int:
    return int(time.time() * 1000)


def remote_config(training_kwargs: dict, folder) -> tuple[dict, dict]:
    """Studio's worker config, minus everything tied to this machine. Returns (config, env secrets)."""
    config = _build_training_worker_config(training_kwargs)
    if config.get("model_local_path") and not config.get("actual_model_repo_id"):
        raise ValueError("This model is a local folder. Push it to Hugging Face to train it on a cloud GPU.")
    for key in ("model_known_cached", "dataset_known_cached", "require_exact_resume_resources",
                "require_exact_model_resource", "require_exact_dataset_resource", "require_validated_model_snapshot"):
        config[key] = False
    for key in ("model_local_path", "model_snapshot_path", "dataset_local_path", "dataset_snapshot_path",
                "gpu_ids", "offload_vram_gb", "offload_vram_gb_per_device"):
        config[key] = None
    env = {}
    if config.get("hf_token"):  # secrets travel as runner env vars, never in the bucket
        env["HF_TOKEN"] = config["hf_token"]
    if config.get("wandb_token"):
        env["WANDB_API_KEY"] = config["wandb_token"]
    config["hf_token"] = config["wandb_token"] = ""
    config["allow_ambient"] = True
    if config.get("enable_tensorboard"):
        config["tensorboard_dir"] = JOB_PREFIX + "tensorboard"
    for key in ("local_datasets", "local_eval_datasets"):
        paths = config.get(key)
        if not paths:
            continue
        if not all(isinstance(p, str) for p in paths):
            raise ValueError(f"Unsupported {key} entries for a cloud job.")
        rewritten = []
        for i, p in enumerate(paths):
            name = f"inputs/{key}/{i}_{Path(p).name}"
            folder.put_file(name, p)
            rewritten.append(JOB_PREFIX + name)
        config[key] = rewritten
    return config, env


def first_run_ms(schedule, timezone: str) -> int:
    if not schedule:
        return now_ms()
    nxt = next_run_after(parse_schedule(schedule), timezone or "UTC", now_ms() - 1)
    if nxt is None:
        raise ValueError("This schedule has no upcoming run.")
    return nxt


class CloudJobs:
    def __init__(self):
        self.backends: dict[str, RemoteTrainingBackend] = {}
        self.launchers = {"local": LocalLauncher()}
        self._wake = threading.Event()
        self._thread = None

    # ---- helpers
    def launcher(self, job: dict):
        name = (job.get("resources") or {}).get("launcher", "skypilot")
        if name not in self.launchers:
            from cloud_jobs.skypilot_launcher import SkyPilotLauncher

            self.launchers[name] = SkyPilotLauncher()
        return self.launchers[name]

    def folder(self, job: dict):
        return open_folder(jobs_root(), job["id"])

    def backend(self, job_id: str) -> RemoteTrainingBackend | None:
        return self.backends.get(job_id)

    # ---- create / rerun / cancel
    def create(self, *, name, request: dict, training_kwargs: dict, resources: dict, schedule, timezone: str) -> dict:
        job_id = f"cj_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        folder = open_folder(jobs_root(), job_id)
        config, env = remote_config(training_kwargs, folder)
        folder.write_text("config.json", json.dumps(config))
        store.insert({"id": job_id, "name": name or request.get("model_name"), "state": "scheduled",
                      "resources": resources, "schedule": schedule, "timezone": timezone, "request": request,
                      "config": config, "secrets": env, "folder": folder.uri,
                      "created_at": now_ms(), "next_run_at": first_run_ms(schedule, timezone)})
        self._wake.set()
        return store.get(job_id)

    def clone(self, job: dict, *, next_run_at: int, keep_schedule: bool) -> dict:
        """A new job with the same config and inputs (rerun, or the next occurrence of a repeating job)."""
        job_id = f"cj_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        src, dst = self.folder(job), open_folder(jobs_root(), job_id)
        for key in ("local_datasets", "local_eval_datasets"):
            for p in job["config"].get(key) or ():
                rel = p.removeprefix(JOB_PREFIX)
                dst.write_bytes(rel, src.read_bytes(rel))
        dst.write_text("config.json", json.dumps(job["config"]))
        store.insert({**{k: job[k] for k in ("name", "resources", "request", "config", "secrets", "timezone")},
                      "id": job_id, "state": "scheduled", "schedule": job["schedule"] if keep_schedule else None,
                      "folder": dst.uri, "created_at": now_ms(), "next_run_at": next_run_at, "parent_id": job["id"]})
        self._wake.set()
        return store.get(job_id)

    def rerun(self, job_id: str) -> dict:
        return self.clone(store.get(job_id), next_run_at = now_ms(), keep_schedule = False)

    def cancel(self, job_id: str) -> None:
        job = store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["state"] == "scheduled":
            store.update(job_id, state = "cancelled", ended_at = now_ms())
            return
        backend = self.backends.get(job_id)
        if backend is not None:  # Studio's own stop path: stop.json now, launcher cancel after the grace period
            backend.stop_training(save = False, expected_job_id = job_id)
        else:
            self.launcher(job).cancel(job)

    # ---- launch / watch
    def _launch(self, job: dict) -> None:
        store.update(job["id"], state = "launching", started_at = now_ms())
        try:
            hours = float(job["resources"].get("max_hours", 12))
            env = {**(job["secrets"] or {}), "UNSLOTH_JOB_MAX_HOURS": f"{hours:g}",
                   "UNSLOTH_JOB_DEADLINE": str(time.time() + hours * 3600)}
            external_id = self.launcher(job).launch(job, self.folder(job), env)
        except Exception as e:  # e.g. no GPU capacity anywhere right now: retry with backoff, then give up
            attempts = (job.get("attempts") or 0) + 1
            logger.exception("cloud job %s: launch attempt %d failed", job["id"], attempts)
            if attempts < LAUNCH_ATTEMPTS:
                store.update(job["id"], state = "scheduled", attempts = attempts, error = f"Launch failed, retrying: {e}",
                             next_run_at = now_ms() + RETRY_BACKOFF_MS * 2 ** (attempts - 1))
            else:
                store.update(job["id"], state = "failed", attempts = attempts, ended_at = now_ms(),
                             error = f"Launch failed after {attempts} attempts: {e}")
            return
        store.update(job["id"], state = "running", external_id = external_id)
        self._attach(store.get(job["id"]))
        if job.get("schedule") and job["schedule"].get("kind") != "once":  # queue the next occurrence
            nxt = next_run_after(parse_schedule(job["schedule"]), job["timezone"] or "UTC", now_ms(),
                                 anchor_ms = job["next_run_at"])
            if nxt:
                self.clone(job, next_run_at = nxt, keep_schedule = True)

    def _attach(self, job: dict, already_in_db: bool = False) -> None:
        launcher = self.launcher(job)
        backend = RemoteTrainingBackend(job["id"], self.folder(job), job["config"],
                                        is_gone = lambda: launcher.is_gone(job), cancel = lambda: launcher.cancel(job),
                                        already_in_db = already_in_db)
        self.backends[job["id"]] = backend
        threading.Thread(target = self._watch, args = (job["id"], backend), daemon = True).start()

    def _watch(self, job_id: str, backend: RemoteTrainingBackend) -> None:
        backend._proc.join()
        if backend._pump_thread is not None:
            backend._pump_thread.join(timeout = 120)
        p = backend._progress
        state = ("cancelled" if backend._cancel_requested else
                 "succeeded" if p.is_completed and not p.error else "failed")
        store.update(job_id, state = state, ended_at = now_ms(), error = p.error)
        self._wake.set()

    # ---- scheduler
    def start(self) -> None:
        """Re-attach jobs that were running when the server stopped, then run the scheduler loop."""
        if self._thread is not None:
            return
        for job in store.list_jobs(ACTIVE):
            try:
                self._attach(job, already_in_db = True)
            except Exception:
                logger.exception("cloud job %s: re-attach failed", job["id"])
        self._thread = threading.Thread(target = self._loop, daemon = True, name = "cloud-jobs-scheduler")
        self._thread.start()

    def tick(self) -> None:
        for job in store.list_jobs(ACTIVE):  # backstop if the runner never got to enforce its own deadline
            hours = float(job["resources"].get("max_hours", 12))
            if job["started_at"] and now_ms() > job["started_at"] + (hours * 3600 + 15 * 60) * 1000:
                logger.warning("cloud job %s: past its %g h limit, cancelling", job["id"], hours)
                self.cancel(job["id"])
        free = max_concurrent() - len(store.list_jobs(ACTIVE))
        due = [j for j in store.list_jobs(("scheduled",)) if (j["next_run_at"] or 0) <= now_ms()]
        local_busy = local_gpu_busy()
        for job in sorted(due, key = lambda j: j["next_run_at"]):
            if free <= 0:
                break
            if job["resources"].get("launcher") == "local":
                if local_busy:  # one training per local GPU; it waits in the queue
                    continue
                local_busy = True
            self._launch(job)
            free -= 1

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("cloud jobs scheduler tick failed")
            self._wake.wait(10)
            self._wake.clear()


cloud_jobs = CloudJobs()
