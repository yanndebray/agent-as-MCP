"""Durable session transcripts for run_task follow-ups (issue #3).

Claude Code records each session as a JSONL transcript under
``~/.claude/projects/<munged-cwd>/<session-id>.jsonl`` and can resume it via
the Agent SDK's ``resume`` option — but only if the transcript is on local
disk and the run uses the same ``cwd`` (the project dir is derived from it).
Both are fragile on managed platforms: the filesystem is in-memory and blank
on every new instance, and the caller doesn't know the original cwd.

This module makes resume survivable:

  * ``persist()`` — after every run, record the session's cwd locally and,
    when file transfer is configured, mirror transcript + cwd to GCS under
    ``{AGENT_NAME}/sessions/``.
  * ``restore()`` — before a resumed run, ensure the transcript is back on
    local disk (from the GCS mirror if this is a fresh instance) and return
    the session's original cwd, so the run lands in the directory the
    conversation happened in.

Without a bucket the store still works for the lifetime of the instance
(warm resumes), matching task_store's degradation. All functions are
blocking — callers on the event loop wrap them in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import os
import re

import file_io

# Local session metadata (session_id -> cwd), outside the task workspaces so
# reset_workspace() never touches it. /tmp is the writable FS on managed
# platforms.
SESSIONS_DIR = os.environ.get("AGENT_SESSIONS_DIR", "/tmp/sk8-sessions")

# Claude Code session ids are UUIDs; reject anything else before it becomes a
# filename or object key (no path traversal via session_id).
_SID_RE = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def valid(session_id: str) -> bool:
    return bool(_SID_RE.fullmatch(session_id))


def project_dir(cwd: str) -> str:
    """The Claude Code project dir for a cwd (every non-alnum char -> '-')."""
    munged = re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(cwd))
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", munged)


def transcript_path(session_id: str, cwd: str) -> str:
    return os.path.join(project_dir(cwd), f"{session_id}.jsonl")


def _meta_path(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{session_id}.json")


def _gcs_transcript_key(session_id: str) -> str:
    return f"{file_io.AGENT_NAME}/sessions/{session_id}.jsonl"


def _gcs_meta_key(session_id: str) -> str:
    return f"{file_io.AGENT_NAME}/sessions/{session_id}.meta.json"


def persist(session_id: str, cwd: str) -> None:
    """Record a finished run's session: cwd meta locally, full mirror to GCS.

    Best-effort by design — a mirror hiccup must not fail the run whose
    answer is already in hand; it only narrows resume to this instance.
    """
    cwd = os.path.abspath(cwd)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with open(_meta_path(session_id), "w", encoding="utf-8") as fh:
        json.dump({"cwd": cwd}, fh)
    if not file_io.enabled():
        return
    path = transcript_path(session_id, cwd)
    if not os.path.isfile(path):
        return
    try:
        file_io.put_file(_gcs_transcript_key(session_id), path)
        file_io.put_text(_gcs_meta_key(session_id), json.dumps({"cwd": cwd}))
    except Exception:
        pass


def restore(session_id: str) -> str | None:
    """Make a session resumable on this instance; return its cwd, or None.

    Looks up the session's cwd (local meta first, then the GCS mirror) and,
    if the transcript isn't already on local disk, pulls it from GCS into the
    project dir the SDK will look in. None means the session is unknown here:
    bad id, expired mirror, or a bucket-less agent recycled since the run.
    """
    if not valid(session_id):
        return None
    meta = None
    try:
        with open(_meta_path(session_id), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        if file_io.enabled():
            text = file_io.get_text(_gcs_meta_key(session_id))
            if text is not None:
                try:
                    meta = json.loads(text)
                except ValueError:
                    meta = None
    if not meta or "cwd" not in meta:
        return None
    cwd = meta["cwd"]
    path = transcript_path(session_id, cwd)
    if not os.path.isfile(path):
        if not file_io.enabled():
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not file_io.get_file(_gcs_transcript_key(session_id), path):
            return None
    # Re-cache the meta locally so the next resume skips the GCS lookup.
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    with open(_meta_path(session_id), "w", encoding="utf-8") as fh:
        json.dump({"cwd": cwd}, fh)
    return cwd
