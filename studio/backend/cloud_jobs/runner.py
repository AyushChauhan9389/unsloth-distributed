"""Remote side of a cloud job: run Studio's unchanged training worker against a job folder.

usage (cwd = studio/backend):  python -m cloud_jobs.runner <job_dir>

The job folder is a local directory in development and a mounted bucket folder on a cloud GPU, so this
file only reads and writes files:
  config.json            worker config built by the central server          (read)
  events/NNNNNN.jsonl    worker events, one new file per flush              (written)
  heartbeat.json         {"ts", "seq"}, rewritten every few seconds         (written)
  stop.json              {"save": bool}, created by the central server      (read)  -> stops the worker
  output_dir.json        the run's output dir, kept for resume after a reclaim (written)
  result.json            {"exit_code", "ended", "attempt"}                  (written)
Only whole new files are written (no appends): bucket mounts handle those best.
"""
import json
import multiprocessing as mp
import os
import queue
import sys
import time
from pathlib import Path

FLUSH_SECONDS = 3.0
HEARTBEAT_SECONDS = 15.0
MAX_BATCH = 200


def write_file(path: Path, text: str) -> None:
    """Write via a temp name then rename, so a reader never sees half a file (readers skip *.tmp)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding = "utf-8")
    os.replace(tmp, path)


def next_seq(events: Path) -> int:
    return max((int(p.stem) for p in events.glob("*.jsonl") if p.stem.isdigit()), default = 0) + 1


def latest_checkpoint(output_dir: str | None) -> str | None:
    """Newest checkpoint-<step> folder of a previous attempt, for resume after a spot reclaim."""
    if not output_dir or not Path(output_dir).is_dir():
        return None
    ckpts = [p for p in Path(output_dir).glob("checkpoint-*") if p.name.split("-")[-1].isdigit()]
    return str(max(ckpts, key = lambda p: int(p.name.split("-")[-1]))) if ckpts else None


def encode_batch(batch: list) -> str:
    return "".join(json.dumps(e, default = str) + "\n" for e in batch)


def main(job_dir: str, target = None) -> int:
    job = Path(job_dir)
    events = job / "events"
    events.mkdir(parents = True, exist_ok = True)
    # "{JOB}/inputs/..." paths (uploaded datasets) point into this job folder wherever it is mounted.
    config = json.loads((job / "config.json").read_text(encoding = "utf-8")
                        .replace("{JOB}/", job.resolve().as_posix() + "/"))
    # Outputs and checkpoints land inside the job folder (the bucket on a cloud GPU).
    os.environ.setdefault("UNSLOTH_STUDIO_HOME", str((job / "studio").resolve()))

    attempt = 1
    prev = job / "output_dir.json"
    if prev.exists():  # a previous attempt ran (spot reclaim): resume from its newest checkpoint
        attempt = json.loads(prev.read_text()).get("attempt", 1) + 1
        ckpt = latest_checkpoint(json.loads(prev.read_text()).get("output_dir"))
        if ckpt and not config.get("resume_from_checkpoint"):
            config["resume_from_checkpoint"] = ckpt

    if target is None:
        from core.training.worker import run_training_process as target

    ctx = mp.get_context("spawn")
    event_q, stop_q = ctx.Queue(), ctx.Queue()
    proc = ctx.Process(target = target,
                       kwargs = {"event_queue": event_q, "stop_queue": stop_q, "config": config}, daemon = True)
    proc.start()

    seq, batch, last_flush, last_beat, stop_sent = next_seq(events), [], 0.0, 0.0, False
    max_hours = float(os.environ.get("UNSLOTH_JOB_MAX_HOURS", "12"))
    # absolute, so a spot-recovery restart does not get a fresh clock
    deadline = float(os.environ.get("UNSLOTH_JOB_DEADLINE") or time.time() + max_hours * 3600)

    def flush():
        nonlocal seq, batch, last_flush
        if batch:
            write_file(events / f"{seq:06d}.jsonl", encode_batch(batch))
            seq, batch = seq + 1, []
        last_flush = time.time()

    while True:
        try:
            event = event_q.get(timeout = 0.5)
            batch.append(event)
            if isinstance(event, dict) and event.get("type") == "output_dir":
                write_file(prev, json.dumps({"output_dir": event.get("output_dir"), "attempt": attempt}))
        except queue.Empty:
            if not proc.is_alive():
                break
        now = time.time()
        if batch and (now - last_flush >= FLUSH_SECONDS or len(batch) >= MAX_BATCH):
            flush()
        if now - last_beat >= HEARTBEAT_SECONDS:
            write_file(job / "heartbeat.json", json.dumps({"ts": now, "seq": seq, "attempt": attempt}))
            last_beat = now
        if not stop_sent and now >= deadline:  # runtime cap: stop and save, even if the server is unreachable
            batch.append({"type": "warning", "message": f"Max runtime ({max_hours:g} h) reached: stopping and saving."})
            stop_q.put({"type": "stop", "save": True})
            stop_sent = True
        stop_file = job / "stop.json"
        if not stop_sent and stop_file.exists():
            save = bool(json.loads(stop_file.read_text() or "{}").get("save", True))
            stop_q.put({"type": "stop", "save": save})
            stop_sent = True

    while True:  # drain anything the worker queued before exiting
        try:
            batch.append(event_q.get_nowait())
        except queue.Empty:
            break
    flush()
    proc.join()
    write_file(job / "result.json", json.dumps({"exit_code": proc.exitcode, "ended": time.time(), "attempt": attempt}))
    return 0 if proc.exitcode == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
