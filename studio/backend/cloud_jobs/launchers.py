"""Where a cloud job's runner actually runs.

local     -- a subprocess on this machine (development, or a server with its own GPU).
skypilot  -- a SkyPilot managed job on Google Cloud / AWS / Kubernetes (see skypilot_launcher.py).
Every launcher: launch(job, folder, env) -> external id;  is_gone(job) -> bool;  cancel(job);  logs(job) -> str.
"""
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent  # studio/backend: the runner imports Studio's modules from here


class LocalLauncher:
    name = "local"

    def __init__(self):
        self._procs: dict[str, subprocess.Popen] = {}

    def launch(self, job: dict, folder, env: dict) -> str:
        log = open(Path(folder.uri) / "runner.log", "ab")
        proc = subprocess.Popen([sys.executable, "-m", "cloud_jobs.runner", folder.uri], cwd = BACKEND_DIR,
                                env = {**os.environ, **env}, stdout = log, stderr = subprocess.STDOUT)
        log.close()  # the child holds its own handle
        self._procs[job["id"]] = proc
        return str(proc.pid)

    def is_gone(self, job: dict) -> bool:
        proc = self._procs.get(job["id"])
        return proc is None or proc.poll() is not None  # None: the server restarted and lost the handle

    def cancel(self, job: dict) -> None:
        proc = self._procs.get(job["id"])
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def logs(self, job: dict, folder, tail: int = 200) -> str:
        p = Path(folder.uri) / "runner.log"
        return "\n".join(p.read_text(encoding = "utf-8", errors = "replace").splitlines()[-tail:]) if p.exists() else ""
