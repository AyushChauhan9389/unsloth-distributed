"""One TrainingBackend per cloud job, driven by the job folder's event files instead of a local worker.

Studio's backend talks to its worker through three handles: _proc (liveness, terminate), _event_queue and
_stop_queue. Here they are stand-ins, so Studio's own pump, event handling, stop logic, DB history writes
and the status/metrics/progress code all run unchanged for a remote run.
"""
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone

from core.training.training import TrainingBackend, TrainingProgress, _sanitize_db_config
from utils.account_context import account_thread

logger = logging.getLogger(__name__)
POLL_SECONDS = 2.0


class RemoteProc:
    """Stands in for the worker process. Alive until result.json exists AND every event file has been fed
    (else Studio's pump would finalize before the last events arrive). terminate/kill cancel the job."""

    pid = None

    def __init__(self, cancel):
        self.exitcode = None
        self._done = threading.Event()
        self._cancel = cancel

    def start(self):
        pass

    def is_alive(self) -> bool:
        return not self._done.is_set()

    def join(self, timeout = None):
        self._done.wait(timeout)

    def terminate(self):
        self._cancel()

    kill = terminate

    def finish(self, exitcode):
        self.exitcode = exitcode
        self._done.set()


class StopFile:
    """_stop_queue stand-in: Studio's stop_training() puts {"type": "stop", "save": ...}; the runner polls stop.json."""

    def __init__(self, folder):
        self.folder = folder

    def put(self, msg, *args, **kwargs):
        if isinstance(msg, dict) and msg.get("type") == "stop":
            self.folder.write_text("stop.json", json.dumps({"save": bool(msg.get("save", True))}))


class RemoteTrainingBackend(TrainingBackend):
    def __init__(self, job_id: str, folder, config: dict, is_gone, cancel, already_in_db: bool = False):
        """is_gone(): the launcher says the job ended without writing result.json (crash, cancelled).
        cancel(): ask the launcher to cancel the job."""
        super().__init__()
        self.folder = folder
        self._is_gone = is_gone
        with self._lock:
            self.current_job_id = job_id
            self._should_stop = self._cancel_requested = False
            self._cancel_cleanup_output_dir = None
            self._complete_seen.clear()
            self._progress = TrainingProgress(is_training = True, status_message = "Waiting for a cloud GPU...")
            self._db_config = _sanitize_db_config(config)
            self._db_started_at = datetime.now(timezone.utc).isoformat()
            self._db_run_created = already_in_db  # re-attached after a server restart: the run row exists
            self._xet_fallback_used = True  # never try to respawn a local worker on a download stall
            self._last_full_config = config
            self._event_queue = queue.Queue()
            self._stop_queue = StopFile(folder)
            self._proc = RemoteProc(cancel)
        self._ensure_db_run_created()
        self._feeder = threading.Thread(target = self._feed, daemon = True, name = f"cloud-feed-{job_id}")
        self._feeder.start()
        pump = account_thread(target = self._pump_loop, account = self._result_account, daemon = True)
        with self._lock:
            self._pump_running = False
            self._pump_thread = pump
            pump.start()

    def _feed(self):
        """Feed new event files to Studio's pump in order; finish the stand-in process when the job ends."""
        seen = set()

        def feed_new() -> None:
            for name in self.folder.list("events"):
                if name.endswith(".jsonl") and name not in seen:
                    for line in self.folder.read_text(f"events/{name}").splitlines():
                        if line.strip():
                            self._event_queue.put(json.loads(line))
                    seen.add(name)

        while True:
            try:
                feed_new()
                if self.folder.exists("result.json"):
                    feed_new()  # the runner writes result.json after its final event file
                    self._proc.finish(json.loads(self.folder.read_text("result.json")).get("exit_code"))
                    return
                if self._is_gone():
                    feed_new()
                    self._proc.finish(None)
                    return
            except Exception:
                logger.exception("cloud job %s: reading events failed; retrying", self.current_job_id)
            time.sleep(POLL_SECONDS)
