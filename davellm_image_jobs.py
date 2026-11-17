"""Single-GPU job queue for DaveLLM image generation.

One worker thread runs every image job, so at most one job is ever on the GPU
(the 8 GB envelope in benchmarks/imagegen-2026-11-17 assumes exactly that).
Jobs wait in submission order. Each job reports its status, its position in
line, step progress when ComfyUI sends it, and can be cancelled: a queued job
is dropped, a running job is interrupted in ComfyUI.

Jobs live in memory only. The images they produce go to the image store; the
queue keeps the last ``keep_finished`` finished jobs so the UI can show them.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

ACTIVE = ("queued", "running")


class QueueFull(RuntimeError):
    pass


class JobNotFound(KeyError):
    pass


# A runner receives (progress(value, max), cancel_event) and returns a result dict.
Runner = Callable[[Callable[[int, int], None], threading.Event], dict]


@dataclass
class Job:
    id: str
    kind: str
    label: str
    runner: Runner
    meta: dict = field(default_factory=dict)
    status: str = "queued"
    progress: dict = field(default_factory=lambda: {"value": 0, "max": 0})
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    finished: Optional[float] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)

    def public(self, position: Optional[int], full: bool = False) -> dict[str, Any]:
        result = self.result
        if result is not None and not full:
            result = {k: v for k, v in result.items() if k != "image"}
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "position": position, "progress": dict(self.progress), "meta": self.meta,
            "created": self.created, "started": self.started, "finished": self.finished,
            "result": result, "error": self.error,
        }


class JobQueue:
    def __init__(self, *, max_queued: int = 16, keep_finished: int = 50,
                 cancelled_error: type[BaseException] = Exception):
        self.max_queued = max_queued
        self.keep_finished = keep_finished
        self._cancelled_error = cancelled_error
        self._lock = threading.Condition()
        self._pending: deque[Job] = deque()
        self._running: Optional[Job] = None
        self._finished: deque[Job] = deque(maxlen=keep_finished)
        self._jobs: dict[str, Job] = {}
        self._worker: Optional[threading.Thread] = None

    # -- submission ------------------------------------------------------------
    def submit(self, kind: str, label: str, runner: Runner, meta: Optional[dict] = None) -> Job:
        job = Job(id=f"job-{secrets.token_hex(6)}", kind=kind, label=label[:200], runner=runner, meta=meta or {})
        with self._lock:
            if len(self._pending) >= self.max_queued:
                raise QueueFull(f"The image queue is full ({self.max_queued} waiting)")
            self._pending.append(job)
            self._jobs[job.id] = job
            self._ensure_worker()
            self._lock.notify_all()
        return job

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._work, name="davellm-image-gpu", daemon=True)
            self._worker.start()

    # -- worker ------------------------------------------------------------------
    def _work(self) -> None:
        while True:
            with self._lock:
                while not self._pending:
                    if not self._lock.wait(timeout=30):
                        if not self._pending:
                            self._worker = None
                            return
                job = self._pending.popleft()
                self._running = job
                job.status = "running"
                job.started = time.time()

            def progress(value: int, maximum: int, job=job) -> None:
                job.progress = {"value": max(0, int(value)), "max": max(0, int(maximum))}

            try:
                result = job.runner(progress, job.cancel)
                outcome, payload, error = "done", result, None
            except self._cancelled_error:
                outcome, payload, error = "cancelled", None, "Cancelled"
            except Exception as exc:  # reported on the job, never raised into the worker
                outcome, payload, error = ("cancelled", None, "Cancelled") if job.cancel.is_set() else ("failed", None, _message(exc))
            with self._lock:
                job.status, job.result, job.error = outcome, payload, error
                if outcome == "done" and job.progress["max"]:
                    job.progress["value"] = job.progress["max"]
                job.finished = time.time()
                self._running = None
                self._retire(job)
                job.done.set()
                self._lock.notify_all()

    def _retire(self, job: Job) -> None:
        if len(self._finished) == self._finished.maxlen:
            old = self._finished[0]
            self._jobs.pop(old.id, None)
        self._finished.append(job)

    # -- queries and control --------------------------------------------------
    def _position(self, job: Job) -> Optional[int]:
        if job.status == "running":
            return 0
        if job.status == "queued":
            return 1 + list(self._pending).index(job)
        return None

    def get(self, job_id: str, *, full: bool = False) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(job_id)
            return job.public(self._position(job), full)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            ordered = ([self._running] if self._running else []) + list(self._pending) + list(reversed(self._finished))
            return [job.public(self._position(job)) for job in ordered]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(job_id)
            if job.status == "queued":
                self._pending.remove(job)
                job.status, job.error, job.finished = "cancelled", "Cancelled", time.time()
                job.cancel.set()
                self._retire(job)
                job.done.set()
            elif job.status == "running":
                job.cancel.set()  # the runner interrupts ComfyUI and returns
            return job.public(self._position(job))

    def wait(self, job_id: str, timeout: Optional[float] = None) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        job.done.wait(timeout)
        with self._lock:
            return job.public(self._position(job), True)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {"running": 1 if self._running else 0, "queued": len(self._pending), "max_queued": self.max_queued}


def _message(exc: BaseException) -> str:
    text = str(exc).strip()
    return text[:300] if text else exc.__class__.__name__
