"""Durable records for detached run_task calls (issue #2).

A detached task's result must never depend on the HTTP connection that started
it. Each task gets one small JSON record — status, timestamps, and eventually
the final result text — written to local disk on every transition and, when
file transfer is configured (``file_io.enabled()``), mirrored to GCS under
``{AGENT_NAME}/tasks/{task_id}.json``. The mirror is what makes results survive
Cloud Run recycling an instance between the run and the poll; without a bucket
the store still works, but only for the lifetime of the instance.

Reads check local disk first (the common warm-instance case), then fall back
to GCS and re-cache locally. All functions are blocking — callers on the event
loop wrap them in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid

import file_io

# Records live outside the task workspaces so reset_workspace() never touches
# them. /tmp is the only writable FS on managed platforms.
TASKS_DIR = os.environ.get("AGENT_TASKS_DIR", "/tmp/sk8-tasks")

# Task ids are minted by new_task_id() below; anything else is rejected before
# it can become a filename or an object key (no path traversal via task_id).
_ID_RE = re.compile(r"[0-9a-f]{12}")


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


def _local_path(task_id: str) -> str:
    return os.path.join(TASKS_DIR, f"{task_id}.json")


def _gcs_key(task_id: str) -> str:
    return f"{file_io.AGENT_NAME}/tasks/{task_id}.json"


def _write(rec: dict) -> None:
    """Persist a record locally (atomic rename) and best-effort mirror to GCS.

    A GCS hiccup must not fail the task itself — the local copy still serves
    same-instance polls, which is strictly better than erroring the run.
    """
    os.makedirs(TASKS_DIR, exist_ok=True)
    path = _local_path(rec["task_id"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(tmp, path)
    if file_io.enabled():
        try:
            file_io.put_text(_gcs_key(rec["task_id"]), json.dumps(rec))
        except Exception:
            pass


def create(task_id: str, prompt: str) -> dict:
    """Record a task as running. Keeps only a prompt snippet for identification."""
    now = time.time()
    rec = {
        "task_id": task_id,
        "status": "running",
        "prompt": prompt[:200],
        "created_at": now,
        "updated_at": now,
        "result": None,
    }
    _write(rec)
    return rec


def finish(task_id: str, status: str, result: str) -> None:
    """Mark a task done/error and store its final text. Last act of every run."""
    now = time.time()
    rec = get(task_id) or {"task_id": task_id, "created_at": now}
    rec.update(status=status, result=result, updated_at=now, finished_at=now)
    _write(rec)


def get(task_id: str) -> dict | None:
    """Fetch a record: local disk first, then the GCS mirror (re-cached locally)."""
    if not _ID_RE.fullmatch(task_id):
        return None
    try:
        with open(_local_path(task_id), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        pass
    if file_io.enabled():
        text = file_io.get_text(_gcs_key(task_id))
        if text is not None:
            try:
                rec = json.loads(text)
            except ValueError:
                return None
            _write(rec)  # re-cache for subsequent polls on this instance
            return rec
    return None
