"""The server's shared print queue (PLAN.md §3.1).

    queued -> waiting_for_plate -> printing -> done | failed | cancelled

A job only starts after an Operator confirms the build plate is clear, even
when the printer is free: the server can't see the plate reliably, people can.
When a job finishes, a camera snapshot is saved with it (if the printer has one).
"""

import asyncio
import contextlib
import datetime
import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

ACTIVE = ("queued", "waiting_for_plate", "printing")


@dataclass
class Job:
    id: str
    name: str
    path: str
    machine: str
    submitted_by: str
    submitted_at: str
    size: int
    state: str = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    progress: int = 0
    message: str = ""
    snapshot: str | None = None
    history: list = field(default_factory=list)

    def public(self):
        d = asdict(self)
        d.pop("path")
        d["has_snapshot"] = bool(self.snapshot)
        d.pop("snapshot")
        return d


def _now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


class JobQueue:
    def __init__(self, directory, backend_getter, *, confirm_required=True):
        """backend_getter() returns the current admin-level backend (or None if offline)."""
        self.dir = directory
        os.makedirs(os.path.join(directory, "jobs"), exist_ok=True)
        self.path = os.path.join(directory, "queue.json")
        self._backend = backend_getter
        self.confirm_required = confirm_required
        self.jobs: list[Job] = []
        self._listeners = []
        self._lock = asyncio.Lock()
        self._load()

    # ------------------------------------------------------------ persistence / events

    def _load(self):
        try:
            with open(self.path) as f:
                self.jobs = [Job(**d) for d in json.load(f)]
        except FileNotFoundError:
            return
        for j in self.jobs:          # a server restart can't know how a print ended
            if j.state == "printing":
                self._set(j, "failed", "server restarted during the print", save=False)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump([asdict(j) for j in self.jobs], f, indent=1)
        os.replace(tmp, self.path)

    def on_change(self, callback):
        self._listeners.append(callback)

    def _changed(self):
        self._save()
        for cb in list(self._listeners):
            try:
                cb(self.public())
            except Exception:  # noqa: BLE001
                log.exception("queue listener failed")

    def _set(self, job, state, message="", save=True):
        job.state = state
        job.message = message
        job.history.append({"at": _now(), "state": state, "message": message})
        if state == "printing":
            job.started_at = _now()
        if state in ("done", "failed", "cancelled"):
            job.finished_at = _now()
        if save:
            self._changed()

    def public(self):
        return [j.public() for j in self.jobs]

    def get(self, job_id):
        for j in self.jobs:
            if j.id == job_id:
                return j
        raise KeyError(job_id)

    # ------------------------------------------------------------ operations

    def add(self, name, data_path, machine, submitted_by):
        job = Job(id=secrets.token_hex(6), name=name, path=data_path, machine=machine,
                  submitted_by=submitted_by, submitted_at=_now(),
                  size=os.path.getsize(data_path))
        job.history.append({"at": job.submitted_at, "state": "queued",
                            "message": f"submitted by {submitted_by}"})
        self.jobs.append(job)
        self._changed()
        return job

    def job_file(self, job_id, filename):
        """jobs/<id>/<original name>: the printer shows the file name on its screen."""
        d = os.path.join(self.dir, "jobs", job_id)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, os.path.basename(filename))

    async def remove(self, job_id):
        async with self._lock:
            job = self.get(job_id)
            if job.state == "printing":
                raise PermissionError("that job is printing; cancel it on the printer first")
            self.jobs.remove(job)
            for p in (job.path, job.snapshot):
                if p and os.path.exists(p):
                    os.remove(p)
            with contextlib.suppress(OSError):
                os.rmdir(os.path.join(self.dir, "jobs", job.id))
            self._changed()

    async def cancel(self, job_id):
        async with self._lock:
            job = self.get(job_id)
            if job.state == "printing":
                backend = self._backend()
                if backend:
                    await backend.cancel()
            if job.state in ACTIVE:
                self._set(job, "cancelled", "cancelled from OpenBot")

    async def confirm_plate(self, job_id, by):
        """An Operator says the plate is clear: start this job now."""
        async with self._lock:
            job = self.get(job_id)
            if job.state != "waiting_for_plate":
                raise PermissionError(f"job is {job.state.replace('_', ' ')}, not waiting")
            backend = self._backend()
            if backend is None:
                raise PermissionError("the printer isn't connected")
            status = await backend.status()
            if status.process is not None:
                if status.process.complete:
                    await backend.acknowledge()        # clear the finished print first
                else:
                    raise PermissionError("the printer is busy")
            job.history.append({"at": _now(), "state": "plate_confirmed",
                                "message": f"plate cleared, confirmed by {by}"})
            await backend.start_print(job.path)
            self._set(job, "printing", f"started by {by}")

    # ------------------------------------------------------------ runner

    def status_changed(self, status):
        """Feed every printer status here; advances the queue."""
        printing = next((j for j in self.jobs if j.state == "printing"), None)
        if printing:
            p = status.process
            if p is None:
                return
            printing.progress = int(p.progress or 0)
            if p.complete or p.step in ("completed", "cancelled", "failed"):
                if p.cancelled or p.step == "cancelled":
                    self._set(printing, "cancelled", "cancelled on the printer")
                elif p.error or p.step == "failed":
                    msg = (p.error or {}).get("message", "") if isinstance(p.error, dict) \
                        else str(p.error)
                    self._set(printing, "failed", msg or "the printer reported an error")
                else:
                    printing.progress = 100
                    self._set(printing, "done", "finished")
                asyncio.ensure_future(self._snapshot(printing))
            return
        waiting = next((j for j in self.jobs if j.state == "waiting_for_plate"), None)
        if waiting is None:
            nxt = next((j for j in self.jobs if j.state == "queued"), None)
            if nxt is not None:
                self._set(nxt, "waiting_for_plate",
                          "clear the build plate, then confirm to start")

    async def _snapshot(self, job):
        backend = self._backend()
        if backend is None or not backend.supports("camera"):
            return
        try:
            frame = await asyncio.wait_for(backend.snapshot(), 15)
        except Exception as e:  # noqa: BLE001 - a missing photo never blocks the outcome
            job.history.append({"at": _now(), "state": "snapshot_failed", "message": str(e)})
            self._changed()
            return
        path = os.path.join(os.path.dirname(job.path), "result.jpg")
        with open(path, "wb") as f:
            f.write(frame.data)
        job.snapshot = path
        self._changed()

    def __len__(self):
        return len(self.jobs)
