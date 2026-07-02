"""GCS-backed file transfer for the remote agent (shared by both backends).

MCP-over-HTTP is JSON-RPC, so file bytes would have to be base64'd (~33% bloat)
inside content blocks, and Cloud Run enforces a hard **32 MB inbound request
cap** — uploads bigger than that 413. So we treat **MCP as the control plane**
and move bytes **out-of-band through GCS**: the protocol carries only object
keys and short-lived *signed URLs*, and the client PUT/GETs bytes straight to
the bucket, bypassing the instance entirely.

Surface used by server.py / server_sdk.py:

  * ``enabled()``            — is file transfer configured on this instance?
  * ``request_upload_url()`` — mint a signed PUT URL for one input (a tool)
  * ``fetch_result()``       — mint a signed GET URL for one artifact (a tool)
  * ``download_inputs()``    — pull named inputs into ``cwd/inputs/`` before a run
  * ``upload_outputs()``     — push everything under ``cwd/outputs/`` to GCS after

Everything is **optional**: with ``GCS_BUCKET`` unset (local / VM deploy, the
default image) or ``google-cloud-storage`` not installed, ``enabled()`` is False,
the two tools are never registered, and ``run_task`` behaves exactly as before.

Signed URLs are minted **without a key file**: we refresh the runtime service
account's default credentials and sign via the IAM ``signBlob`` API. That needs
the SA to hold ``roles/iam.serviceAccountTokenCreator`` on *itself* plus object
read/write on the bucket (granted by the deploy tooling).
"""

from __future__ import annotations

import base64
import os
import shutil
import uuid
from datetime import timedelta

# Optional dependency: absent on a default/local install. Guard the import so
# both servers still start; enabled() stays False and nothing here is reached.
try:
    import google.auth
    from google.auth.transport.requests import Request
    from google.cloud import storage

    GCS_AVAILABLE = True
except ImportError:  # google-cloud-storage / google-auth not installed
    GCS_AVAILABLE = False

# One shared bucket, namespaced per agent (matches issue #6's prefix model).
BUCKET = os.environ.get("GCS_BUCKET")
AGENT_NAME = os.environ.get("AGENT_NAME", "default")  # object-key prefix
# Signed-URL lifetime: long enough to transfer a large file over a slow link,
# short enough to bound exposure of the unauthenticated URL.
URL_TTL_SECONDS = int(os.environ.get("SIGNED_URL_TTL", "3600"))
# Phase 2 (issue #14): small files ride *inline* (base64) through MCP itself, no
# bucket required. Hard cap kept well under Cloud Run's 32 MB inbound limit —
# base64 inflates ~33% and shares the request with the prompt + JSON-RPC frame.
# Above this, callers are pointed at the signed-URL path. Tune via INLINE_MAX_BYTES.
MAX_INLINE_BYTES = int(os.environ.get("INLINE_MAX_BYTES", str(8 * 1024 * 1024)))


class FileIOError(Exception):
    """A GCS transfer failed (missing object, denied signing, etc.)."""


def enabled() -> bool:
    """True only when a bucket is configured *and* the GCS libs are importable.

    Both servers gate tool registration and all transfer work on this, so a
    False here means the instance behaves byte-for-byte like today's default.
    """
    return GCS_AVAILABLE and bool(BUCKET)


def _safe_name(filename: str) -> str:
    """Reduce a client-supplied filename to a bare basename (no path traversal)."""
    name = os.path.basename(filename.replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        raise FileIOError(f"invalid filename: {filename!r}")
    return name


def _signer():
    """Return (storage.Client, service_account_email, access_token) for signing.

    Refreshing default credentials populates a fresh access token; passing it
    plus the SA email to ``generate_signed_url`` routes signing through IAM
    ``signBlob`` so no private key ever lives on the box.
    """
    creds, _ = google.auth.default()
    creds.refresh(Request())
    sa_email = getattr(creds, "service_account_email", None) or os.environ.get(
        "GCS_SIGNER_SA"
    )
    if not sa_email:
        raise FileIOError(
            "cannot determine signing service account; set GCS_SIGNER_SA or run "
            "under a service-account identity"
        )
    return storage.Client(), sa_email, creds.token


def _sign(blob_name: str, method: str, *, content_type: str | None = None) -> str:
    client, sa_email, token = _signer()
    blob = client.bucket(BUCKET).blob(blob_name)
    try:
        return blob.generate_signed_url(
            version="v4",
            method=method,
            expiration=timedelta(seconds=URL_TTL_SECONDS),
            content_type=content_type,  # binds the PUT's Content-Type; ignored for GET
            service_account_email=sa_email,
            access_token=token,
        )
    except Exception as exc:  # most commonly: SA lacks serviceAccountTokenCreator
        raise FileIOError(
            f"failed to sign URL for {blob_name!r} (does the runtime SA hold "
            f"roles/iam.serviceAccountTokenCreator on itself?): {exc}"
        ) from exc


def request_upload_url(
    filename: str, content_type: str = "application/octet-stream"
) -> dict:
    """Mint a signed PUT URL to upload one input straight to GCS.

    The client PUTs the bytes to ``upload_url`` (with the matching Content-Type),
    then passes the returned ``object`` key in ``run_task(inputs=[...])``.
    """
    key = f"{AGENT_NAME}/inputs/{uuid.uuid4().hex}/{_safe_name(filename)}"
    return {
        "object": key,
        "upload_url": _sign(key, "PUT", content_type=content_type),
        "content_type": content_type,
        "expires_in_seconds": URL_TTL_SECONDS,
    }


def fetch_result(object_key: str) -> dict:
    """Mint a fresh signed GET URL for an existing object (URLs expire)."""
    return {
        "object": object_key,
        "download_url": _sign(object_key, "GET"),
        "expires_in_seconds": URL_TTL_SECONDS,
    }


def put_text(key: str, text: str) -> None:
    """Write a small JSON/text object to the bucket (task records, not file bytes)."""
    storage.Client().bucket(BUCKET).blob(key).upload_from_string(
        text, content_type="application/json")


def get_text(key: str) -> str | None:
    """Read a small text object back, or None if absent/unreadable."""
    try:
        return storage.Client().bucket(BUCKET).blob(key).download_as_text()
    except Exception:
        return None


def put_file(key: str, path: str) -> None:
    """Upload one local file to the bucket (session transcripts and the like)."""
    storage.Client().bucket(BUCKET).blob(key).upload_from_filename(path)


def get_file(key: str, path: str) -> bool:
    """Download one object to a local path; False if absent/unreadable."""
    try:
        storage.Client().bucket(BUCKET).blob(key).download_to_filename(path)
        return True
    except Exception:
        # download_to_filename leaves a partial/empty file behind on failure.
        try:
            os.remove(path)
        except OSError:
            pass
        return False


def reset_workspace(cwd: str) -> None:
    """Remove stale ``inputs/`` and ``outputs/`` dirs before a run.

    Cloud Run reuses warm instances across sequential ``run_task`` calls, so a
    previous run's files can linger under ``cwd/inputs|outputs/`` — and the
    leftover outputs would be re-uploaded by the next run under a new run-id.
    Clearing both dirs up front scopes each run's transfer set to exactly the
    files that run touched. No-op when the dirs don't exist.
    """
    for sub in ("inputs", "outputs"):
        shutil.rmtree(os.path.join(cwd, sub), ignore_errors=True)


def _unique_name(name: str, used: set[str]) -> str:
    """Disambiguate ``name`` against ``used`` with a ``-N`` suffix; record it.

    Inputs from either source (GCS keys or inline files) can share a basename
    (e.g. two ``data.csv``); a flat ``inputs/`` dir would let the second clobber
    the first. The first keeps its name, later collisions get ``-1``, ``-2``, ….
    """
    if name in used:
        base, ext = os.path.splitext(name)
        n = 1
        while f"{base}-{n}{ext}" in used:
            n += 1
        name = f"{base}-{n}{ext}"
    used.add(name)
    return name


def write_inline_inputs(
    files: list[dict], cwd: str, used: set[str] | None = None
) -> list[str]:
    """Decode inline base64 ``files`` into ``cwd/inputs/``; return local paths.

    Each item is ``{"name": str, "content_base64": str}``. Needs no bucket — this
    is the Phase 2 small-file path. Each file is hard-capped at MAX_INLINE_BYTES
    (decoded); larger ones raise FileIOError pointing at the signed-URL path.
    ``used`` is a shared name set so inline + GCS inputs never collide.
    """
    if not files:
        return []
    used = set() if used is None else used
    dest_dir = os.path.join(cwd, "inputs")
    os.makedirs(dest_dir, exist_ok=True)
    local_paths: list[str] = []
    for item in files:
        name = _safe_name(str(item.get("name", "")))
        content_b64 = item.get("content_base64")
        if content_b64 is None:
            raise FileIOError(f"inline input {name!r} missing 'content_base64'")
        try:
            data = base64.b64decode(content_b64, validate=True)
        except Exception as exc:
            raise FileIOError(f"inline input {name!r} is not valid base64: {exc}")
        if len(data) > MAX_INLINE_BYTES:
            raise FileIOError(
                f"inline input {name!r} is {len(data)} bytes, over the "
                f"{MAX_INLINE_BYTES}-byte inline cap; upload it with "
                f"request_upload_url + run_task(inputs=[...]) instead.")
        dst = os.path.join(dest_dir, _unique_name(name, used))
        with open(dst, "wb") as fh:
            fh.write(data)
        local_paths.append(dst)
    return local_paths


def download_inputs(
    object_keys: list[str], cwd: str, used: set[str] | None = None
) -> list[str]:
    """Download each object into ``cwd/inputs/<basename>``; return local paths.

    Colliding basenames are disambiguated via ``_unique_name`` (shared ``used``
    set, so inline + GCS inputs don't clobber each other). Streams to disk (never
    into memory) to limit RAM pressure on Cloud Run's in-memory FS. Raises
    FileIOError if an object is missing or unreadable.
    """
    used = set() if used is None else used
    client = storage.Client()
    bucket = client.bucket(BUCKET)
    dest_dir = os.path.join(cwd, "inputs")
    os.makedirs(dest_dir, exist_ok=True)
    local_paths: list[str] = []
    for key in object_keys:
        name = _unique_name(_safe_name(key), used)
        blob = bucket.blob(key)
        dst = os.path.join(dest_dir, name)
        try:
            blob.download_to_filename(dst)
        except Exception as exc:
            raise FileIOError(f"could not download input {key!r}: {exc}") from exc
        local_paths.append(dst)
    return local_paths


def upload_outputs(cwd: str) -> list[tuple[str, str]]:
    """Upload every file under ``cwd/outputs/`` to GCS; return (relpath, GET URL).

    Output discovery is by **convention, not detection**: only files the agent
    wrote under ``outputs/`` are uploaded. Returns [] if that dir is absent or
    empty, so callers append nothing to the result in the common no-artifact case.
    """
    outputs_dir = os.path.join(cwd, "outputs")
    if not os.path.isdir(outputs_dir):
        return []
    client = storage.Client()
    bucket = client.bucket(BUCKET)
    run_id = uuid.uuid4().hex  # isolate this run's artifacts under the prefix
    results: list[tuple[str, str]] = []
    for root, _dirs, files in os.walk(outputs_dir):
        for name in files:
            local = os.path.join(root, name)
            rel = os.path.relpath(local, outputs_dir)
            key = f"{AGENT_NAME}/outputs/{run_id}/{rel}"
            try:
                bucket.blob(key).upload_from_filename(local)
            except Exception as exc:
                raise FileIOError(f"could not upload artifact {rel!r}: {exc}") from exc
            results.append((rel, _sign(key, "GET")))
    return results


def format_artifacts(artifacts: list[tuple[str, str]]) -> str:
    """Render uploaded artifacts as a trailing block appended to run_task output."""
    if not artifacts:
        return ""
    lines = "\n".join(f"- {rel} -> {url}" for rel, url in artifacts)
    return f"\n\nArtifacts:\n{lines}"


def inline_outputs(cwd: str) -> tuple[list[tuple[str, str]], list[tuple[str, int]]]:
    """Read ``cwd/outputs/*`` for the no-bucket path; base64 the small ones.

    Returns ``(items, skipped)`` where ``items`` is ``[(relpath, base64)]`` for
    files within MAX_INLINE_BYTES and ``skipped`` is ``[(relpath, size)]`` for
    files too large to inline (callers surface those as a "configure a bucket"
    hint rather than failing the whole run). Empty lists if ``outputs/`` is absent.
    """
    outputs_dir = os.path.join(cwd, "outputs")
    if not os.path.isdir(outputs_dir):
        return [], []
    items: list[tuple[str, str]] = []
    skipped: list[tuple[str, int]] = []
    for root, _dirs, files in os.walk(outputs_dir):
        for name in files:
            local = os.path.join(root, name)
            rel = os.path.relpath(local, outputs_dir)
            size = os.path.getsize(local)
            if size > MAX_INLINE_BYTES:
                skipped.append((rel, size))
                continue
            with open(local, "rb") as fh:
                items.append((rel, base64.b64encode(fh.read()).decode("ascii")))
    return items, skipped


def format_inline_artifacts(
    items: list[tuple[str, str]], skipped: list[tuple[str, int]]
) -> str:
    """Render inline (base64) artifacts + an over-cap notice as a trailing block."""
    blocks = ""
    if items:
        lines = "\n".join(f"- {rel} (base64): {b64}" for rel, b64 in items)
        blocks += f"\n\nArtifacts (inline base64):\n{lines}"
    if skipped:
        lines = "\n".join(f"- {rel} ({size} bytes)" for rel, size in skipped)
        blocks += (
            "\n\nArtifacts too large to inline (set GCS_BUCKET to return these "
            f"via signed URL):\n{lines}")
    return blocks
