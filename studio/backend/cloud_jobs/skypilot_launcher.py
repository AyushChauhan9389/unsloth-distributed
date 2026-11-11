"""Launch cloud jobs as SkyPilot managed jobs through a SkyPilot API server (plain HTTP, works from Windows).

The GPU runs the official unsloth/unsloth image (Studio + its Python stack) and this fork's studio/backend
code, uploaded once per code version to gs://<bucket>/code/<hash>.tar.gz. The bucket is mounted at /bucket,
so the runner's job folder is /bucket/jobs/<id>. Managed jobs recover from spot reclaims by themselves;
the runner then resumes from the newest checkpoint in the job folder.
"""
import hashlib
import io
import json
import os
import tarfile
import time
import urllib.request
from pathlib import Path

from cloud_jobs.gcs_folder import client, split_uri
from cloud_jobs.launchers import BACKEND_DIR

TERMINAL = {"SUCCEEDED", "FAILED", "FAILED_SETUP", "FAILED_PRECHECKS", "FAILED_NO_RESOURCE", "FAILED_CONTROLLER",
            "CANCELLED", "CANCELLING"}
IMAGE = os.environ.get("UNSLOTH_CLOUD_JOBS_IMAGE", "docker:unsloth/unsloth:latest")
STATE_CACHE_SECONDS = 20

RUN = """set -e
PY=$(ls -d /opt/unsloth-studio/*/bin/python 2>/dev/null | head -1); PY=${PY:-python3}
mkdir -p ~/code && tar xzf /bucket/code/__CODE__.tar.gz -C ~/code
cd ~/code/backend && exec $PY -m cloud_jobs.runner /bucket/jobs/__JOB__
"""


def code_archive() -> tuple[str, bytes]:
    """studio/backend as a tar.gz (no caches/tests), named by a hash of its contents."""
    files = sorted(p for p in BACKEND_DIR.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and "tests" not in p.parts and p.suffix != ".pyc")
    digest = hashlib.sha256()
    for p in files:
        digest.update(str(p.relative_to(BACKEND_DIR)).encode())
        digest.update(p.read_bytes())
    buf = io.BytesIO()
    with tarfile.open(fileobj = buf, mode = "w:gz") as tar:
        for p in files:
            tar.add(p, arcname = "backend/" + p.relative_to(BACKEND_DIR).as_posix())
    return digest.hexdigest()[:16], buf.getvalue()


class SkyPilotLauncher:
    name = "skypilot"

    def __init__(self):
        self.url = os.environ.get("UNSLOTH_SKYPILOT_URL", "http://127.0.0.1:46580").rstrip("/")
        self._states: dict[str, tuple[float, str]] = {}
        self._code: str | None = None

    # ---- SkyPilot API server
    def _request(self, path: str, body: dict) -> str:
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout = 60) as r:
            return r.headers.get("X-Skypilot-Request-ID") or json.loads(r.read() or b"null")

    def _result(self, request_id: str):
        with urllib.request.urlopen(f"{self.url}/api/get?request_id={request_id}", timeout = 3600) as r:
            d = json.load(r)
        if d.get("status") != "SUCCEEDED":
            raise RuntimeError(str(d.get("error") or d.get("status")))
        rv = d.get("return_value")
        return json.loads(rv) if isinstance(rv, str) else rv

    def _text(self, request_id: str) -> str:
        with urllib.request.urlopen(f"{self.url}/api/stream?request_id={request_id}&format=plain", timeout = 120) as r:
            return r.read().decode("utf-8", "replace")

    # ---- launcher interface
    def _ensure_code(self, bucket: str) -> str:
        if self._code is None:
            digest, data = code_archive()
            blob = client().bucket(bucket).blob(f"code/{digest}.tar.gz")
            if not blob.exists():
                blob.upload_from_string(data)
            self._code = digest
        return self._code

    def task(self, job: dict, bucket: str, code: str, env: dict) -> dict:
        r = job["resources"]
        res = {"accelerators": r.get("accelerator", "H100:1"), "use_spot": bool(r.get("use_spot", True)),
               "disk_size": int(r.get("disk_size", 200)), "image_id": IMAGE}
        regions = r.get("regions") or []
        if regions:
            res["any_of"] = [{"infra": f"{r.get('infra', 'gcp')}/{reg}"} for reg in regions]
        else:
            res["infra"] = r.get("infra", "gcp")
        return {"name": job["id"], "resources": res, "secrets": env,
                "envs": {"UNSLOTH_CLOUD_JOB_ID": job["id"]},
                "file_mounts": {"/bucket": {"source": f"gs://{bucket}", "mode": "MOUNT"}},
                "run": RUN.replace("__CODE__", code).replace("__JOB__", job["id"])}

    def launch(self, job: dict, folder, env: dict) -> str:
        bucket, _ = split_uri(folder.uri)
        task = self.task(job, bucket, self._ensure_code(bucket), env)
        rv = self._result(self._request("/jobs/launch", {"task": json.dumps(task), "name": job["id"]}))
        job_ids = rv[0] if isinstance(rv, list) else rv
        return str(job_ids[0] if isinstance(job_ids, list) else job_ids)

    def state(self, job: dict) -> str | None:
        ext = job.get("external_id")
        if not ext:
            return None
        cached = self._states.get(ext)
        if cached and time.time() - cached[0] < STATE_CACHE_SECONDS:
            return cached[1]
        rv = self._result(self._request("/jobs/queue/v2", {"job_ids": [int(ext)], "skip_finished": False,
                                                            "all_users": True, "refresh": False}))
        records = rv.get("jobs", rv) if isinstance(rv, dict) else rv
        status = str(records[0].get("status", "")).split(".")[-1] if records else None
        self._states[ext] = (time.time(), status)
        return status

    def is_gone(self, job: dict) -> bool:
        try:
            return self.state(job) in TERMINAL
        except Exception:
            return False  # API server unreachable: keep waiting rather than failing the job

    def cancel(self, job: dict) -> None:
        if job.get("external_id"):
            self._result(self._request("/jobs/cancel", {"job_ids": [int(job["external_id"])]}))

    def logs(self, job: dict, folder, tail: int = 200) -> str:
        if not job.get("external_id"):
            return ""
        rid = self._request("/jobs/logs", {"job_id": int(job["external_id"]), "tail": tail, "follow": False})
        return self._text(rid)
