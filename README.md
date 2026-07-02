# sk8 🛹

[![PyPI](https://img.shields.io/pypi/v/sk8)](https://pypi.org/project/sk8/)

A minimal MCP server that exposes a **remote Claude Code agent** over HTTP.
Claude Code on your laptop can call its one tool, `run_task`, to delegate a
complete, self-contained task to a Claude Code instance running on this machine.

The tool runs `claude` headless here and returns the final text answer.
By default it is **synchronous and blocking**; for long tasks, pass
`detach=true` to get a task id back immediately and poll `get_task` for the
result (see "Detached tasks" below).

**`server_sdk.py`** — drives the same agent loop through the **Claude Agent
SDK** (`claude-agent-sdk`), giving a typed async message stream and structured
permission control. **This is what the container image runs**, because
`ClaudeAgentOptions` applies per-agent profile customization (tools, MCP,
system prompt) natively. 

Agents can be **customized per profile** — extra Python packages, bundled Claude
Code skills, and a tool/MCP/system-prompt spec baked into the image at build
time.

## GCP project setup (one-time)

The `sk8` CLI drives `gcloud` to provision agents on Cloud Run, so a GCP project
has to be prepared once before `sk8 create` will work. Run these once per
project (not per agent):

```bash
# 1. Install the gcloud SDK, then authenticate.
gcloud auth login

# 2. Select the project sk8 should deploy into (must have billing enabled).
gcloud config set project YOUR_PROJECT_ID

# 3. Enable the APIs sk8 uses (Cloud Run, Secret Manager, Artifact Registry, Cloud Build).
gcloud services enable \
  run.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com

# 4. Create the Docker repo sk8 pushes the agent image to.
#    The name "agents" and location must match sk8's defaults
#    (--repo agents, --region us-central1); override both flags if you change them.
gcloud artifacts repositories create agents \
  --repository-format=docker --location=us-central1

# 5. Store the shared Claude credential the agents run under, as the
#    "anthropic-api-key" secret (sk8 mounts it into every agent).
printf '%s' "$ANTHROPIC_API_KEY" | \
  gcloud secrets create anthropic-api-key --data-file=-
```

With that in place, `sk8 create <id> --build` builds the image (first time) and
deploys the agent. You can preview every `gcloud` command without running it via
`sk8 create <id> --dry-run`.

## 🛹 CLI

`sk8` is the command-line tool for managing agents — scriptable for both humans
and agents alike. It exposes the full agent lifecycle and emits JSON so a running
agent can spawn and register sub-agents. Install it as a console script to run
`sk8 <cmd>` from anywhere, or run it in place with `python sk8.py <cmd>`:

```bash
uv tool install .                  # install the `sk8` command from this repo
uv tool install --reinstall .      # reinstall after pulling/editing the code
uv tool uninstall sk8              # remove it
# (or use pipx/pip: `pipx install .` / `pipx reinstall sk8`)
```

Then drive the agent lifecycle:

```bash
sk8 create iris --build     # build image (first time) + provision
sk8 create iris             # subsequent agents reuse the image
sk8 create iris --profile ./profiles/data-analyst --build  # custom deps/skills/tools
sk8 create iris --json      # for agents: parse back {url, token, mcp_add_command}
sk8 create iris --dry-run   # preview the gcloud commands offline
sk8 list                    # list deployed agents in the region
sk8 delete iris --yes       # tear down the service + its token secret
sk8 suggest 5               # propose adjective-noun names
```

`create` prints the end-user's `claude mcp add` line (with `--json`, in the
`mcp_add_command` field). Re-running `create` with an existing id rotates its
token and redeploys.

## Verify

```bash
claude mcp list        # sk8 should show as connected
```

Then in a Claude Code session on your laptop:

> Use the sk8 run_task tool with prompt:
> "list the files in the current directory and summarize them"

The remote agent runs the task in its `cwd` and returns the final answer.

## Sessions (follow-up tasks)

Every `run_task` answer ends with a `[session_id: <uuid>]` line (detached
tasks report it via `get_task` once finished). Pass it back to continue the
same conversation instead of restating the brief:

> Use the sk8 run_task tool with session_id "<uuid>" and prompt:
> "now also add unit tests for that script"

A resumed task remembers the prior exchange and runs in the session's
original working directory (the `cwd` argument is ignored), so files from
earlier turns are still in place. Session transcripts are mirrored to GCS on
`--bucket` agents, so follow-ups work even after the instance was recycled;
without a bucket, sessions last only as long as the instance. An unknown or
expired id fails fast with `AGENT_ERROR: unknown session_id ...` — start
fresh with a full brief.

## Detached tasks (long-running work)

A synchronous `run_task` result lives and dies with its HTTP connection: if
the client's MCP timeout fires (Claude Code's default is well under sk8's
600s task budget) or the connection drops, the work completes on the server
and evaporates. For anything long, detach instead:

> Use the sk8 run_task tool with detach=true and prompt: "..."

The call returns `TASK_STARTED: <task_id>` immediately; the task runs in the
background in its own workspace. Poll:

> Use the sk8 get_task tool with task_id "<task_id>"

which returns `{status: running|done|error|lost|not_found, result, ...}` —
`result` carries the final answer (including any Artifacts block) once the
status is `done`. Poll every 30–60s; on a scale-to-zero deployment the polls
also keep the instance alive.

Task records are written to local disk and, when the agent has a GCS bucket
(`--bucket`), mirrored to `<agent>/tasks/<task_id>.json` — so a finished
result survives the instance being recycled between the run and the poll.
Without a bucket, detach still works but a recycled instance loses the
record (`get_task` then reports `not_found`).

Detached tasks are supported by the SDK backend (`server_sdk.py`, what the
container image runs). Deploys via `sk8 create` set `--no-cpu-throttling` on
the Cloud Run service so background work keeps its CPU after the HTTP
response returns.

## Cloud deployment (Cloud Run, App Runner, Fly, …)

The GCP services (Cloud Build, Artifact Registry, Secret Manager, IAM, Cloud
Run) and the agent lifecycle — image build → token mint → a `run_task` call
triggering Cloud Run:

![GCP service graph](images/remote-agent-gcp-architecture.svg)


## File transfer (optional, GCS-backed)

Pass files in and out of `run_task` without bloating the prompt or hitting Cloud
Run's 32 MB request cap. Provision an agent with a bucket:

```sh
sk8 create iris --bucket my-agents-bucket --build   # or: BUCKET=my-agents-bucket ./deploy.sh iris
```

This wires up GCS signed URLs and a `--ttl-days` lifecycle rule (default 7 days)
so inputs/outputs auto-expire. The agent then exposes two extra tools:

- `request_upload_url(filename, content_type)` → a signed **PUT** URL; upload an
  input straight to the bucket, then pass its object key to
  `run_task(inputs=[...])`.
- `fetch_result(object)` → re-mint a signed **GET** URL for an artifact.

`run_task` also accepts small files inline (`files=[{"name", "content_base64"}]`,
no bucket needed). Anything the agent writes under `cwd/outputs/` comes back in a
trailing `Artifacts:` block — signed download URLs when GCS-backed, else inline
base64. Without a bucket the feature stays dark and `run_task` is text-only. See
[`docs/file-transfer.md`](docs/file-transfer.md) for the full design.

## Limitations

- **No streaming or progress** — synchronous `run_task` blocks until the remote
  agent finishes (up to a 600s timeout); detached tasks report only
  running/done/error via `get_task`, not intermediate output.
- **No queue** — detached tasks run concurrently on the single instance and
  share its CPU/memory; there's no scheduling or backpressure beyond that.
- **Arbitrary code execution** — `claude` can do anything the host user can. 
  Treat reaching this endpoint as equivalent to a shell on the box.
- **Fresh context unless resumed** — each `run_task` without a `session_id` is
  a fresh headless `claude` invocation with no memory of previous tasks; put
  all needed context in the prompt, or resume a session for follow-ups.
