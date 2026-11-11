"""Runner self-test with a fake worker (stdlib only).  run:  cd studio/backend && python -m pytest cloud_jobs/test_runner.py"""
import json
import queue
import threading
import time
from pathlib import Path

from cloud_jobs import runner


def fake_worker(*, event_queue, stop_queue, config):
    """Emits progress until it gets a stop message (or 40 steps), like the real worker's event shapes."""
    event_queue.put({"type": "output_dir", "output_dir": config["out"], "ts": time.time()})
    for step in range(1, 41):
        try:
            msg = stop_queue.get(timeout = 0.05)
            if msg.get("type") == "stop":
                event_queue.put({"type": "complete", "stopped": True, "save": msg.get("save")})
                return
        except queue.Empty:
            pass
        event_queue.put({"type": "progress", "step": step, "total_steps": 40, "loss": 1.0 / step})
    event_queue.put({"type": "complete", "stopped": False})


def read_events(job: Path) -> list:
    return [json.loads(l) for f in sorted((job / "events").glob("*.jsonl")) for l in f.read_text().splitlines()]


def test_runs_to_completion(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"out": str(tmp_path / "out")}))
    assert runner.main(str(tmp_path), target = fake_worker) == 0
    ev = read_events(tmp_path)
    assert ev[0]["type"] == "output_dir" and ev[-1] == {"type": "complete", "stopped": False}
    assert [e["step"] for e in ev if e["type"] == "progress"] == list(range(1, 41))
    assert json.loads((tmp_path / "result.json").read_text())["exit_code"] == 0
    assert (tmp_path / "heartbeat.json").exists() and not list(tmp_path.rglob("*.tmp"))


def test_stop_file_stops_worker(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"out": str(tmp_path / "out")}))
    threading.Timer(0.6, lambda: (tmp_path / "stop.json").write_text(json.dumps({"save": False}))).start()
    assert runner.main(str(tmp_path), target = fake_worker) == 0
    last = read_events(tmp_path)[-1]
    assert last == {"type": "complete", "stopped": True, "save": False}


def test_resume_picks_newest_checkpoint(tmp_path):
    out = tmp_path / "out"
    for step in (10, 200, 30):
        (out / f"checkpoint-{step}").mkdir(parents = True)
    (tmp_path / "output_dir.json").write_text(json.dumps({"output_dir": str(out), "attempt": 1}))
    assert runner.latest_checkpoint(str(out)).endswith("checkpoint-200")
    assert runner.next_seq(tmp_path) == 1


def test_deadline_stops_and_saves(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"out": str(tmp_path / "out")}))
    monkeypatch.setenv("UNSLOTH_JOB_DEADLINE", str(time.time() - 1))  # already past: e.g. a spot restart after the cap
    assert runner.main(str(tmp_path), target = fake_worker) == 0
    ev = read_events(tmp_path)
    assert any(e["type"] == "warning" and "Max runtime" in e["message"] for e in ev)
    assert ev[-1] == {"type": "complete", "stopped": True, "save": True}
