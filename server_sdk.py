"""sk8 MCP server — Claude Agent SDK backend (prototype).

Same core MCP surface as `server.py`, but `run_task` drives the agent
through the **Claude Agent SDK** (`query()`) instead of shelling out to
`claude -p` and parsing stdout. This backend additionally supports
**detached tasks**: `run_task(detach=true)` returns a task id immediately
and `get_task(task_id)` polls for the result, with task records persisted
via `task_store` (GCS-mirrored when file transfer is enabled) so a finished
result survives dropped connections and instance recycling.

What this buys over the subprocess version:
  * a typed, async message stream instead of a flat text blob — we can see
    each assistant turn (and could surface tool calls / progress);
  * structured permission control via `permission_mode` rather than relying on
    CLI flags;
  * a `ResultMessage` carrying the final text plus cost / duration / usage.

What it does NOT change: the SDK still spawns the `claude` binary under the
hood, so the box still needs Claude Code installed and authenticated, and the
"runs as root → needs IS_SANDBOX=1 with bypassPermissions" caveat from the
systemd unit still applies. This is a richer interface over the same engine,
not a different engine.

Install the extra dependency alongside fastmcp:
    uv pip install claude-agent-sdk      # or: pip install claude-agent-sdk
"""

import asyncio
import os
import sys
import time

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

import file_io
import profile_config
import task_store

TIMEOUT_SECONDS = 600
# get_task treats a "running" record older than this as lost: the 600s task
# timeout would have flipped it to error long before, so the only way to get
# here is the instance dying mid-run (Cloud Run scale-in / crash).
LOST_AFTER_SECONDS = TIMEOUT_SECONDS + 120

# Per-agent profile, baked into the image at build time (see profile_config.py).
# {} for the default (un-customized) image, so the splat below is a no-op then.
PROFILE = profile_config.load_profile()

# Managed platforms (Cloud Run, App Runner, Fly) inject the listen port via
# $PORT and only expose a writable filesystem under /tmp. Both default to the
# bare-metal values so nothing changes for the VM deployment.
PORT = int(os.environ.get("PORT", "8080"))
DEFAULT_CWD = os.environ.get("AGENT_DEFAULT_CWD", "/home/me/workspace")

# --- Auth: require a static bearer token, fail fast if it isn't configured ---
AGENT_TOKEN = os.environ.get("AGENT_TOKEN")
if not AGENT_TOKEN:
    sys.exit("AGENT_TOKEN env var is required (the shared bearer token)")

# StaticTokenVerifier checks `Authorization: Bearer <AGENT_TOKEN>` on every
# request and rejects anything else with 401.
auth = StaticTokenVerifier(tokens={AGENT_TOKEN: {"client_id": "sk8"}})

mcp = FastMCP("sk8", auth=auth)

# Strong references to in-flight detached runs — asyncio only keeps weak refs
# to tasks, so without this a detached run could be garbage-collected mid-task.
_DETACHED: set[asyncio.Task] = set()


# Prepended to every task prompt. The session is one-shot: when the agent ends
# its turn the SDK stream closes and whatever it just said becomes the stored
# result. An agent that backgrounds a command (run_in_background) and ends its
# turn "while waiting" is never re-invoked, so the real answer is lost — seen
# in the wild with a `sleep 90` task returning its interim status line.
HEADLESS_NOTE = (
    "You are running unattended in a one-shot session: the moment you end "
    "your turn, the session terminates and your last message is returned to "
    "the caller as the final result. There is no follow-up and no "
    "re-invocation. Run every command in the foreground and wait for it to "
    "finish — never use run_in_background, and never end your turn while any "
    "work is still pending.\n\n"
)


async def _run(prompt: str, cwd: str, inputs: list[str] | None = None,
               files: list[dict] | None = None) -> str:
    """Drive one task to completion via the Agent SDK, return the final text."""
    # The cwd may not exist yet — the default scratch dir ($AGENT_DEFAULT_CWD,
    # e.g. /tmp/workspace) lives on an in-memory FS that starts empty on every
    # managed-platform instance, so nothing creates it ahead of time.
    os.makedirs(cwd, exist_ok=True)
    # Clear any leftover inputs/outputs from a prior run on this (possibly warm)
    # instance so stale files aren't reused or re-returned. Unconditional:
    # inline transfer (no bucket) uses the same dirs, so don't gate on GCS.
    file_io.reset_workspace(cwd)
    # google-cloud-storage is blocking, so run it off the event loop.
    prompt = await asyncio.to_thread(_prepare_inputs, prompt, cwd, inputs, files)
    prompt = HEADLESS_NOTE + prompt
    opts = dict(
        cwd=cwd,
        # Run unattended: there is no human to approve tool calls mid-task, so
        # the agent must not block on a permission prompt. This is the SDK
        # equivalent of `--permission-mode bypassPermissions` in server.py and
        # carries the same blast radius — see the security notes in the README.
        permission_mode="bypassPermissions",
        # Behave like Claude Code (its full toolset + system prompt), so this is
        # a true drop-in for the `claude -p` backend rather than a bare model.
        system_prompt={"type": "preset", "preset": "claude_code"},
        # task_env() sets IS_SANDBOX (bypassPermissions refuses to run as root
        # otherwise) and puts the profile venv first on PATH so the agent's
        # python3 sees the profile's packages (not uv's project venv). Mirrors
        # server.py.
        env=profile_config.task_env(),
    )
    # Layer per-agent profile customization on top, native to the SDK:
    # allowed_tools, disallowed_tools, mcp_servers, and a system_prompt that
    # appends the profile persona onto the Claude Code preset above. Empty for
    # the default image, so this overrides nothing there.
    opts.update(profile_config.to_sdk_kwargs(PROFILE))
    options = ClaudeAgentOptions(**opts)

    final = None          # ResultMessage.result, if the SDK gives us one
    transcript: list[str] = []  # fallback: every assistant text block, joined

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    transcript.append(block.text)
        elif isinstance(message, ResultMessage):
            if message.is_error:
                return f"AGENT_ERROR: {message.subtype}"
            final = message.result

    text = final if final is not None else "\n".join(transcript)
    return text + await asyncio.to_thread(_collect_outputs, cwd)


def _prepare_inputs(prompt: str, cwd: str, inputs: list[str] | None,
                    files: list[dict] | None) -> str:
    """Materialize inputs into cwd/inputs/ and prepend a note about them.

    Two sources share one dir: `files` are small inline (base64) uploads needing
    no bucket; `inputs` are GCS object keys (requires file transfer enabled).
    Names are deduped across both so a shared basename never clobbers.
    """
    if not inputs and not files:
        return prompt
    used: set[str] = set()
    local: list[str] = []
    if files:
        local += file_io.write_inline_inputs(files, cwd, used)
    if inputs:
        if not file_io.enabled():
            raise file_io.FileIOError(
                "GCS inputs require file transfer (GCS_BUCKET unset); pass small "
                "files inline via `files` instead.")
        local += file_io.download_inputs(inputs, cwd, used)
    listing = "\n".join(f"- {p}" for p in local)
    note = (
        "Input files have been downloaded for this task:\n"
        f"{listing}\n"
        "Write any deliverables you want returned to the caller into the "
        "'outputs/' subdirectory of the working directory.\n\n"
    )
    return note + prompt


def _collect_outputs(cwd: str) -> str:
    """Render cwd/outputs/* as a trailing Artifacts block (empty if none).

    GCS-backed agents upload and return signed URLs; otherwise small files come
    back inline as base64 (over-cap files are listed, not returned).
    """
    if file_io.enabled():
        return file_io.format_artifacts(file_io.upload_outputs(cwd))
    return file_io.format_inline_artifacts(*file_io.inline_outputs(cwd))


async def _run_detached(task_id: str, prompt: str, cwd: str,
                        inputs: list[str] | None,
                        files: list[dict] | None) -> None:
    """Drive a detached task and persist its outcome as the last act.

    Every exit path lands in task_store.finish(), so a poll can always tell
    what happened — nothing escapes into the void of an unawaited coroutine.
    """
    try:
        result = await asyncio.wait_for(
            _run(prompt, cwd, inputs, files), timeout=TIMEOUT_SECONDS)
        status = "error" if result.startswith("AGENT_ERROR:") else "done"
    except asyncio.TimeoutError:
        status, result = "error", f"AGENT_ERROR: timed out after {TIMEOUT_SECONDS}s"
    except Exception as exc:
        status, result = "error", f"AGENT_ERROR: {exc}"
    await asyncio.to_thread(task_store.finish, task_id, status, result)


@mcp.tool
async def run_task(prompt: str, cwd: str = DEFAULT_CWD,
                   inputs: list[str] | None = None,
                   files: list[dict] | None = None,
                   detach: bool = False) -> str:
    """Delegate a complete, self-contained task to a remote agent on another machine.

    The remote agent is a full Claude Code instance driven via the Claude Agent
    SDK. It does NOT share your conversation, files, or context — so the `prompt`
    must carry everything it needs: the goal, relevant background, constraints,
    and the exact deliverable you expect. Write it as a standalone brief, not a
    follow-up.

    By default the call is synchronous and blocking: it runs the task to
    completion (up to 600s) and returns only the agent's final text answer. Use
    `cwd` to point the agent at the directory it should work in on the remote
    machine.

    For long tasks (or a client MCP timeout shorter than the task), pass
    `detach=true`: the call returns immediately with a line
    `TASK_STARTED: <task_id>` and the task runs in the background — poll
    `get_task(task_id)` for status and the final answer. Detached tasks get
    their own workspace unless you pass an explicit `cwd`. Poll roughly every
    30-60s; polling also keeps the (scale-to-zero) instance alive.

    File transfer — two ways to send files in, both landing in `cwd/inputs/`:
      * `files` (small files, no bucket needed): a list of
        `{"name": str, "content_base64": str}`. Each is capped at ~8 MB; larger
        ones are rejected with a pointer to the signed-URL path.
      * `inputs` (large files, GCS-backed agents only): a list of GCS object keys
        from `request_upload_url`, downloaded before the run.
    Anything the agent writes under `cwd/outputs/` comes back in a trailing
    "Artifacts:" block: signed download URLs when the agent is GCS-backed, else
    small files inline as base64 (over-cap files are listed, not returned). Use
    this instead of embedding file bytes in the prompt.

    Returns the agent's final answer (or `TASK_STARTED: <task_id>` when
    detached), or a string starting with "AGENT_ERROR:" on failure.
    """
    if detach:
        task_id = task_store.new_task_id()
        # Isolate each detached task's workspace: concurrent tasks sharing the
        # default cwd would clobber each other via reset_workspace(). An
        # explicit caller-chosen cwd is honored as-is.
        if cwd == DEFAULT_CWD:
            cwd = os.path.join(DEFAULT_CWD, "tasks", task_id)
        await asyncio.to_thread(task_store.create, task_id, prompt)
        task = asyncio.create_task(
            _run_detached(task_id, prompt, cwd, inputs, files))
        _DETACHED.add(task)
        task.add_done_callback(_DETACHED.discard)
        return (f"TASK_STARTED: {task_id}\n"
                f"Poll get_task(\"{task_id}\") for status and the result.")
    try:
        return await asyncio.wait_for(
            _run(prompt, cwd, inputs, files), timeout=TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return f"AGENT_ERROR: timed out after {TIMEOUT_SECONDS}s"
    except Exception as exc:  # e.g. cwd missing, claude not on PATH, SDK/GCS error
        return f"AGENT_ERROR: {exc}"


@mcp.tool
async def get_task(task_id: str) -> dict:
    """Check on a detached run_task: status and, once finished, the final answer.

    Returns {task_id, status, elapsed_seconds, prompt, result} where status is
    one of:
      * "running"   — still going; poll again in 30-60s (result is null).
      * "done"      — finished; `result` holds the agent's final answer,
                      including any trailing Artifacts block.
      * "error"     — finished badly; `result` holds an AGENT_ERROR line.
      * "lost"      — the record says running but far past the task timeout:
                      the instance almost certainly died mid-run. Re-submit.
      * "not_found" — unknown task id (or, on a bucket-less agent, the
                      instance was recycled and the record went with it).
    """
    rec = await asyncio.to_thread(task_store.get, task_id)
    if rec is None:
        return {"task_id": task_id, "status": "not_found",
                "result": None,
                "hint": "Unknown task id — the id may be wrong, or the "
                        "instance was recycled and this agent has no GCS "
                        "bucket to persist task records across instances."}
    # For finished tasks, elapsed is frozen at finished_at; a running task
    # reports time since creation. (finished_at is absent from records written
    # by older versions — fall back to poll time rather than KeyError.)
    end = rec.get("finished_at") or time.time()
    elapsed = round(end - rec["created_at"], 1)
    status = rec["status"]
    if status == "running" and elapsed > LOST_AFTER_SECONDS:
        status = "lost"
    return {"task_id": task_id, "status": status,
            "elapsed_seconds": elapsed, "prompt": rec.get("prompt"),
            "result": rec.get("result")}


# File-transfer tools only exist when the agent is GCS-backed (GCS_BUCKET set).
# On a default / local image they are never registered, so the surface is
# identical to before. GCS calls are blocking, so run them off the event loop.
if file_io.enabled():
    @mcp.tool
    async def request_upload_url(filename: str,
                                 content_type: str = "application/octet-stream") -> dict:
        """Mint a signed PUT URL to upload one input file directly to GCS.

        Uploading straight to the bucket bypasses MCP's JSON-RPC envelope and
        Cloud Run's 32 MB request cap. PUT the bytes to `upload_url` (with the
        matching Content-Type), then pass the returned `object` key to
        `run_task(inputs=[...])`.
        """
        return await asyncio.to_thread(
            file_io.request_upload_url, filename, content_type)

    @mcp.tool
    async def fetch_result(object: str) -> dict:
        """Mint a fresh signed GET URL for an artifact object (signed URLs expire).

        Use it to re-download an object listed in a previous run_task
        "Artifacts:" section straight from GCS.
        """
        return await asyncio.to_thread(file_io.fetch_result, object)


if __name__ == "__main__":
    # stateless_http: Cloud Run is multi-instance + scale-to-zero, so per-instance
    # MCP session state breaks (a session id minted on one instance 404s on the
    # next). Stateless mode avoids issuing one. See issue #14 Phase 0.
    mcp.run(transport="http", host="0.0.0.0", port=PORT, stateless_http=True)
