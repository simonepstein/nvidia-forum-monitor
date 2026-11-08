from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .util import utcnow_iso


class LockBusy(Exception):
    def __init__(self, job: str | None = None, started_at: str | None = None) -> None:
        self.job = job
        self.started_at = started_at
        super().__init__(f"sparkwatch is busy: {job} since {started_at}")

    def as_dict(self) -> dict[str, object]:
        return {"busy": True, "job": self.job, "started_at": self.started_at}


def lock_path(db_path: str | Path) -> Path:
    return Path(db_path).expanduser().parent / "sparkwatch.lock"


def _read_holder(handle) -> dict[str, object]:
    handle.seek(0)
    text = handle.read().strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@contextmanager
def write_lock(job: str, db_path: str | Path) -> Iterator[None]:
    path = lock_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = _read_holder(handle)
            raise LockBusy(holder.get("job"), holder.get("started_at")) from None
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps({"job": job, "started_at": utcnow_iso(), "pid": __import__("os").getpid()})
        )
        handle.flush()
        try:
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
