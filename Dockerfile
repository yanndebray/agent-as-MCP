# Container image for the sk8 MCP server.
# Works on Cloud Run, AWS App Runner, Fly.io, Fargate, or any container host.
# Reads three env vars at runtime:
#   PORT               - listen port (managed platforms inject this; default 8080)
#   AGENT_TOKEN        - shared bearer token gating /mcp (required)
#   ANTHROPIC_API_KEY  - the box's Claude Code credential / billing identity (required)
#
# Build arg:
#   PROFILE  - path (relative to build context) to the agent profile baked into
#              the image: extra Python deps, skills, and a tool/system-prompt
#              spec. Defaults to the empty `profiles/default` so a plain build
#              behaves exactly as an un-customized agent. A profile build passes
#              e.g. --build-arg PROFILE=profiles/data-analyst (see cloudbuild.yaml).
FROM node:22-slim

# Claude Code CLI (the `claude` binary the agent runs under, via the SDK)
RUN npm install -g @anthropic-ai/claude-code

# System deps + uv (Python package manager). python3/venv give the *agent's
# tasks* a real interpreter for profile packages (pandas, python-pptx, ...),
# separate from uv's managed interpreter that runs the server itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# sk8.py is shipped in the wheel (see pyproject [tool.hatch...]), so it must
# be present for `uv sync` to build the project; it's not used at runtime.
# README.md is required too: pyproject's `readme = "README.md"` makes hatchling
# read it during the build.
COPY pyproject.toml uv.lock README.md server.py server_sdk.py profile_config.py file_io.py task_store.py session_store.py sk8.py ./
# --extra gcs pulls in google-cloud-storage so the deployed agent can mint signed
# URLs and shuttle files to/from GCS (issue #14). Harmless when GCS_BUCKET is
# unset — file_io.enabled() is then False and the file-I/O tools don't register.
RUN uv sync --frozen --extra gcs

# --- Per-agent profile -------------------------------------------------------
ARG PROFILE=profiles/default
# Copy the whole profile bundle to a fixed path the server reads at runtime.
COPY ${PROFILE}/ /profile/
ENV AGENT_PROFILE_DIR=/profile

# Install the profile's Python packages into a venv on PATH, so the agent's
# `python`/`python3` task subprocesses resolve them. A comments-only / empty
# requirements.txt (the default profile) is a no-op.
RUN python3 -m venv /opt/agent-venv \
 && if [ -f /profile/requirements.txt ]; then \
      /opt/agent-venv/bin/pip install --no-cache-dir -r /profile/requirements.txt; \
    fi
ENV PATH="/opt/agent-venv/bin:$PATH"
# The server runs via `uv run`, which activates its own project venv and puts
# that bin ahead on PATH — so the ENV PATH above isn't enough on its own. The
# servers read AGENT_TASK_VENV and re-prepend this venv for the agent's task
# subprocess, so the agent's python3 resolves the profile's packages.
ENV AGENT_TASK_VENV=/opt/agent-venv

# Bundle the profile's Claude Code skills where the agent auto-discovers them
# (root's HOME is /root; Claude Code reads ~/.claude/skills).
RUN mkdir -p /root/.claude/skills \
 && if [ -d /profile/skills ]; then cp -a /profile/skills/. /root/.claude/skills/; fi
# -----------------------------------------------------------------------------

# bypassPermissions refuses to run as root unless the host is marked sandboxed.
ENV IS_SANDBOX=1
# Default scratch dir for the agent. The container FS (incl. ~/.claude state) is
# writable, but in-memory on Cloud Run — writes use instance RAM and don't
# persist. Mount a sized in-memory or GCS volume here to cap/persist (see
# docs/cloud-deployment.md §3 "Write access").
ENV AGENT_DEFAULT_CWD=/tmp/workspace
ENV PORT=8080
EXPOSE 8080

# Claude Agent SDK backend: ClaudeAgentOptions exposes the profile's tool/MCP/
# system-prompt customization natively (see server_sdk.py / profile_config.py).
CMD ["uv", "run", "server_sdk.py"]
