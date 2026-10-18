"""Background worker pool with cancellation + progress tracking."""
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Any
from . import database


class JobManager:
    def __init__(self, max_workers: int = 3):
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="neon-job")
        self._cancel_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._max_workers = max_workers

    def start(self, job_id: str, worker: Callable[[Dict, threading.Event], Dict]) -> None:
        event = threading.Event()
        with self._lock:
            self._cancel_events[job_id] = event
        self.executor.submit(self._run, job_id, worker, event)

    def _run(self, job_id: str, worker: Callable[[Dict, threading.Event], Dict], event: threading.Event) -> None:
        import time
        database.update_job(job_id, status="running", started_at=time.time())
        try:
            result = worker(job_id, event)
            if event.is_set():
                database.update_job(job_id, status="cancelled", completed_at=time.time(),
                                    error="cancelled by user")
            elif result and result.get("ok"):
                database.update_job(job_id, status="completed", completed_at=time.time(),
                                    progress=100.0, output_path=result.get("path"))
            else:
                err = (result or {}).get("error", "unknown error")
                database.update_job(job_id, status="failed", completed_at=time.time(), error=err)
        except Exception as e:
            database.update_job(job_id, status="failed", completed_at=time.time(),
                                error=str(e))
            traceback.print_exc()
        finally:
            with self._lock:
                self._cancel_events.pop(job_id, None)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    def shutdown(self) -> None:
        with self._lock:
            for event in self._cancel_events.values():
                event.set()
            self._cancel_events.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)