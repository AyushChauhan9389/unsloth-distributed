"""A job's folder as seen by the central server: a local directory (development, local launcher) or a
bucket prefix (cloud GPUs mount the same prefix as a directory). Same layout as cloud_jobs/runner.py."""
import os
import shutil
from pathlib import Path

JOB_PREFIX = "{JOB}/"  # in config.json paths; the runner swaps it for the job folder's real path


class LocalFolder:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents = True, exist_ok = True)
        self.uri = str(self.path)

    def read_text(self, name: str) -> str:
        return (self.path / name).read_text(encoding = "utf-8")

    def write_text(self, name: str, text: str) -> None:
        p = self.path / name
        p.parent.mkdir(parents = True, exist_ok = True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding = "utf-8")
        os.replace(tmp, p)

    def read_bytes(self, name: str) -> bytes:
        return (self.path / name).read_bytes()

    def write_bytes(self, name: str, data: bytes) -> None:
        p = self.path / name
        p.parent.mkdir(parents = True, exist_ok = True)
        p.write_bytes(data)

    def put_file(self, name: str, src) -> None:
        p = self.path / name
        p.parent.mkdir(parents = True, exist_ok = True)
        shutil.copyfile(src, p)

    def exists(self, name: str) -> bool:
        return (self.path / name).exists()

    def list(self, sub: str) -> list[str]:
        d = self.path / sub
        return sorted(p.name for p in d.iterdir() if not p.name.endswith(".tmp")) if d.is_dir() else []


def open_folder(root: str, job_id: str):
    """root: a local directory now; gs:// buckets are added with the SkyPilot launcher."""
    if root.startswith("gs://"):
        from cloud_jobs.gcs_folder import GcsFolder

        return GcsFolder(f"{root.rstrip('/')}/jobs/{job_id}")
    return LocalFolder(Path(root) / "jobs" / job_id)
