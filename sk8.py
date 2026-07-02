#!/usr/bin/env python3
"""sk8 — create & manage remote MCP agents on GCP Cloud Run.

A Pythonic, scriptable equivalent of deploy.sh. Designed for **both humans and
agents**: a running agent can shell out to this to spawn sub-agents, and the
``create`` command emits machine-readable JSON (``--json``) so the caller can
parse back the URL + bearer token and register the new agent.

It wraps the ``gcloud`` CLI and Secret Manager; it has no third-party
dependencies (stdlib only), so it runs under plain ``python3`` or ``uv run``.

Prerequisites (see docs/cli.md for the full walkthrough):
  * gcloud SDK installed and authenticated (``gcloud auth login``)
  * a GCP project with billing, selected (``gcloud config set project ...``)
  * one-time project setup done: required APIs enabled, an Artifact Registry
    repo, and the shared ``anthropic-api-key`` secret created
  * the container image built at least once (``sk8 create <id> --build``)

Usage (installed as a console script via [project.scripts]; equivalently
``python sk8.py ...`` / ``python -m sk8 ...`` from the repo):
  sk8 create <agent-id> [--profile DIR] [--build] [--json] [--dry-run]
  sk8 suggest [N]
  sk8 list
  sk8 delete <agent-id> [--yes]

Run ``sk8 --help`` (or ``<command> --help``) for all flags.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

__version__ = "0.7.0"

# Side-view skateboard, shown as a banner on each invocation (stderr, so it
# never pollutes --json stdout; muted by --quiet like all other progress).
BANNER = r"""
         __   ____
   _____/ /__( __ )
  / ___/ //_/ __  |
 (__  ) ,< / /_/ /
/____/_/|_|\____/
  __________________
 (__________________)
   O              O
"""

DEFAULT_REGION = "us-central1"
DEFAULT_REPO = "agents"
IMAGE_NAME = "sk8"
MAX_ID_LEN = 40

# Adjective-noun pools for `suggest` — GitHub-default-repo-style memorable slugs.
ADJECTIVES = [
    "amber", "azure", "brave", "bright", "calm", "clever", "cosmic", "crimson",
    "dapper", "eager", "fuzzy", "gentle", "golden", "hazel", "jolly", "lunar",
    "mellow", "nimble", "plucky", "quiet", "rusty", "silver", "sleek", "snowy",
    "solar", "stellar", "sunny", "swift", "teal", "vivid", "witty", "zesty",
]
NOUNS = [
    "otter", "heron", "lynx", "falcon", "willow", "cedar", "iris", "fern",
    "comet", "nebula", "quartz", "pebble", "maple", "robin", "sparrow", "badger",
    "ferret", "marmot", "koi", "newt", "wren", "finch", "tansy", "clover",
    "sage", "basil", "juniper", "aspen", "birch", "beacon",
]


# --- helpers -----------------------------------------------------------------

# Verbosity, set once from the global --quiet/--verbose flags in main().
_QUIET = False
_VERBOSE = False


def eprint(*args: object) -> None:
    """Progress/status to stderr (stdout stays clean for --json). Muted by --quiet."""
    if not _QUIET:
        print(*args, file=sys.stderr)


def vprint(*args: object) -> None:
    """Extra detail shown only under --verbose (e.g. each gcloud command)."""
    if _VERBOSE:
        print(*args, file=sys.stderr)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    # Errors always surface, even under --quiet, so write straight to stderr.
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def slugify(raw: str) -> str:
    """Lowercase; collapse any run of non-alphanumerics to a single hyphen; trim."""
    s = re.sub(r"[^a-z0-9]+", "-", raw.lower())
    return re.sub(r"-+", "-", s).strip("-")


def validate_agent_id(agent_id: str) -> None:
    """Enforce Cloud Run service-name rules (leading letter, [a-z0-9-], <=40)."""
    if not re.fullmatch(r"[a-z]([-a-z0-9]*[a-z0-9])?", agent_id):
        die(
            f"agent id '{agent_id}' is invalid — must start with a letter and "
            f"use only a-z, 0-9, hyphens. Try: sk8 suggest"
        )
    if len(agent_id) > MAX_ID_LEN:
        die(f"agent id '{agent_id}' is too long ({len(agent_id)} chars; max {MAX_ID_LEN}).")


def suggest_names(n: int = 6) -> list[str]:
    return [f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}" for _ in range(n)]


def resolve_profile(profile: str | None) -> tuple[str | None, str]:
    """Validate a --profile dir and derive its image-tag suffix.

    Returns (profile_rel, tag) where:
      * profile_rel is the path relative to the build context (cwd) to pass as
        the PROFILE build-arg, or None when no profile was given;
      * tag is the image tag — 'latest' for the default, else the slugified
        profile name, so the same profile reuses one image and distinct
        profiles get distinct images.
    The profile must live under the build context (cwd), since `gcloud builds
    submit . ` only uploads the current directory as the build context.
    """
    if not profile:
        return None, "latest"
    if not os.path.isdir(profile):
        die(f"profile dir '{profile}' not found.")
    rel = os.path.relpath(profile, os.getcwd())
    if rel.startswith(".."):
        die(f"profile dir '{profile}' must be inside the build context "
            f"(the current directory), so it's uploaded with `builds submit`.")
    name = slugify(os.path.basename(os.path.normpath(profile)))
    if not name:
        die(f"could not derive a profile name from '{profile}'.")
    return rel, name


def require_gcloud() -> None:
    if shutil.which("gcloud") is None:
        die("gcloud not found on PATH. Install the Google Cloud SDK first (see docs/cli.md).")


def gcloud(*args: str, capture: bool = False, check: bool = True,
           dry_run: bool = False, stdin: str | None = None) -> str:
    """Run a gcloud command. Returns stdout (stripped) when capture=True."""
    cmd = ["gcloud", *args]
    if dry_run:
        eprint("DRY-RUN  " + " ".join(cmd))
        return ""
    vprint("RUN  " + " ".join(cmd))
    try:
        # Never let subprocess raise CalledProcessError (a raw traceback);
        # check the return code ourselves and surface a clean die() message.
        result = subprocess.run(
            cmd, input=stdin, capture_output=capture, text=True, check=False,
        )
    except FileNotFoundError:
        die("gcloud not found on PATH. Install the Google Cloud SDK first (see docs/cli.md).")
    if check and result.returncode != 0:
        die(f"`{' '.join(cmd)}` failed:\n{(result.stderr or '').strip()}")
    return (result.stdout or "").strip() if capture else ""


def resolve_project(explicit: str | None) -> str:
    if explicit:
        return explicit
    proj = gcloud("config", "get-value", "project", capture=True)
    if not proj or proj == "(unset)":
        die("no GCP project set. Pass --project, or run `gcloud config set project <id>`.")
    return proj


# --- commands ----------------------------------------------------------------

def cmd_suggest(args: argparse.Namespace) -> int:
    for name in suggest_names(args.count):
        print(name)
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    agent_id = slugify(args.agent_id)
    validate_agent_id(agent_id)
    if agent_id != args.agent_id:
        eprint(f">> normalized agent id: '{args.agent_id}' -> '{agent_id}'")

    # --dry-run previews offline (no gcloud needed); real runs require it.
    if args.dry_run:
        project = args.project or "<project>"
    else:
        require_gcloud()
        project = resolve_project(args.project)
    region, repo = args.region, args.repo
    # The profile (if any) determines the image tag: same profile -> one image,
    # distinct profiles -> distinct images. Default profile -> :latest.
    profile_rel, tag = resolve_profile(args.profile)
    image = f"{region}-docker.pkg.dev/{project}/{repo}/{IMAGE_NAME}:{tag}"
    token_secret = f"{agent_id}-token"
    eprint(f">> project={project} region={region} service={agent_id} "
           f"profile={tag if profile_rel else 'default'}")

    if args.build:
        eprint(f">> building image {image}")
        if profile_rel:
            # --tag mode can't pass --build-arg; route profile builds through
            # cloudbuild.yaml, which forwards PROFILE to `docker build`.
            gcloud("builds", "submit", "--config", "cloudbuild.yaml",
                   f"--substitutions=_PROFILE={profile_rel},_IMAGE={image}", ".",
                   dry_run=args.dry_run)
        else:
            gcloud("builds", "submit", "--tag", image, ".", dry_run=args.dry_run)

    # Mint a fresh per-agent bearer token; store/rotate it in Secret Manager.
    token = secrets.token_hex(32)
    exists = subprocess.run(
        ["gcloud", "secrets", "describe", token_secret],
        capture_output=True, text=True,
    ).returncode == 0 if not args.dry_run else False
    if exists:
        gcloud("secrets", "versions", "add", token_secret, "--data-file=-",
               stdin=token, dry_run=args.dry_run)
    else:
        gcloud("secrets", "create", token_secret, "--data-file=-",
               stdin=token, dry_run=args.dry_run)

    # Grant the Cloud Run runtime service account (the Compute Engine default SA)
    # read access to the secrets it mounts. Without secretAccessor on each
    # secret, the first deploy fails with "Permission denied on secret".
    # Idempotent, so safe to run on every create.
    if args.dry_run:
        runtime_sa = "<project-number>-compute@developer.gserviceaccount.com"
    else:
        project_number = gcloud("projects", "describe", project,
                                "--format=value(projectNumber)", capture=True)
        runtime_sa = f"{project_number}-compute@developer.gserviceaccount.com"
    for secret in (token_secret, "anthropic-api-key"):
        # capture=True so the printed IAM policy doesn't pollute stdout (which
        # must stay clean JSON under --json).
        gcloud("secrets", "add-iam-policy-binding", secret,
               f"--member=serviceAccount:{runtime_sa}",
               "--role=roles/secretmanager.secretAccessor", "--quiet",
               capture=True, dry_run=args.dry_run)

    # Optional GCS file-transfer wiring (#14): with --bucket, create the bucket
    # (idempotent), grant the runtime SA object access + the
    # serviceAccountTokenCreator-on-itself needed to sign URLs with no key file,
    # and pass GCS_BUCKET/AGENT_NAME to the service. Without --bucket the feature
    # stays dark and run_task is text-only.
    env_vars = f"AGENT_NAME={agent_id}"
    if args.bucket:
        bucket_exists = (not args.dry_run) and subprocess.run(
            ["gcloud", "storage", "buckets", "describe", f"gs://{args.bucket}"],
            capture_output=True, text=True,
        ).returncode == 0
        if not bucket_exists:
            eprint(f">> creating bucket gs://{args.bucket}")
            gcloud("storage", "buckets", "create", f"gs://{args.bucket}",
                   f"--location={region}", "--quiet", dry_run=args.dry_run)
        gcloud("storage", "buckets", "add-iam-policy-binding", f"gs://{args.bucket}",
               f"--member=serviceAccount:{runtime_sa}",
               "--role=roles/storage.objectAdmin", "--quiet",
               capture=True, dry_run=args.dry_run)
        gcloud("iam", "service-accounts", "add-iam-policy-binding", runtime_sa,
               f"--member=serviceAccount:{runtime_sa}",
               "--role=roles/iam.serviceAccountTokenCreator", "--quiet",
               capture=True, dry_run=args.dry_run)
        # Lifecycle TTL (issue #6): delete objects older than --ttl-days so
        # inputs/outputs don't accumulate forever. Idempotent; 0 = leave as-is.
        if args.ttl_days > 0:
            rule = {"rule": [{"action": {"type": "Delete"},
                              "condition": {"age": args.ttl_days}}]}
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False
            ) as fh:
                json.dump(rule, fh)
                lifecycle_file = fh.name
            try:
                gcloud("storage", "buckets", "update", f"gs://{args.bucket}",
                       f"--lifecycle-file={lifecycle_file}", "--quiet",
                       capture=True, dry_run=args.dry_run)
            finally:
                os.unlink(lifecycle_file)
            eprint(f">> bucket lifecycle: objects deleted after {args.ttl_days} day(s)")
        env_vars += f",GCS_BUCKET={args.bucket}"
        eprint(f">> file transfer enabled: bucket={args.bucket} (object prefix {agent_id}/)")

    # Deploy a dedicated Cloud Run service for this agent.
    #   --concurrency=8 : NOT for parallel tasks — MCP's streamable-HTTP
    #   transport holds several connections open per session (a long-lived GET
    #   SSE channel + POSTs). concurrency=1 starves the extra streams (Cloud Run
    #   500s/429s) and the client can't connect. run_task stays effectively
    #   serial via max-instances=1. (See docs/cloud-deployment.md "Concurrency".)
    gcloud(
        "run", "deploy", agent_id,
        "--image", image,
        "--region", region,
        "--allow-unauthenticated",
        "--timeout=3600",          # tasks run up to 600s; 300s default would 504
        # Detached tasks (run_task detach=true) keep working after the HTTP
        # response returns; default request-based throttling would starve them
        # to near-zero CPU. Instance-based billing, but bounded by
        # min-instances=0/max-instances=1.
        "--no-cpu-throttling",
        f"--cpu={args.cpu}", f"--memory={args.memory}",
        "--min-instances=0", "--max-instances=1", "--concurrency=8",
        f"--set-secrets=AGENT_TOKEN={token_secret}:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest",
        f"--set-env-vars={env_vars}",
        dry_run=args.dry_run,
    )

    if args.dry_run:
        eprint("(dry run — no resources created)")
        return 0

    url = gcloud("run", "services", "describe", agent_id, "--region", region,
                 "--format=value(status.url)", capture=True)
    # The URL is a positional arg and must come right after the name, before
    # any flags — otherwise `claude mcp add` errors with "missing required
    # argument 'commandOrUrl'".
    mcp_add = (
        f'claude mcp add {agent_id} {url}/mcp --transport http '
        f'--header "Authorization: Bearer {token}"'
    )

    if args.json:
        json.dump(
            {"agent_id": agent_id, "project": project, "region": region,
             "profile": tag if profile_rel else "default",
             "bucket": args.bucket, "file_transfer": bool(args.bucket),
             "url": url, "token": token, "mcp_add_command": mcp_add},
            sys.stdout,
        )
        sys.stdout.write("\n")
    else:
        eprint("\n" + "=" * 64)
        eprint(f" Agent '{agent_id}' provisioned. Hand the user this one command:")
        eprint("=" * 64 + "\n")
        print(mcp_add)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    require_gcloud()
    fmt = "value(metadata.name,status.url)" if args.json else \
          "table(metadata.name, status.url, status.conditions[0].status)"
    out = gcloud("run", "services", "list", "--region", args.region,
                 f"--format={fmt}", capture=True)
    print(out)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    require_gcloud()
    agent_id = slugify(args.agent_id)
    if not args.yes:
        eprint(f"About to delete Cloud Run service '{agent_id}' and secret "
               f"'{agent_id}-token' in {args.region}.")
        if input("Type the agent id to confirm: ").strip() != agent_id:
            die("confirmation did not match; aborting.")
    gcloud("run", "services", "delete", agent_id, "--region", args.region, "--quiet")
    # Secret delete is best-effort (may not exist).
    gcloud("secrets", "delete", f"{agent_id}-token", "--quiet", check=False)
    eprint(f">> deleted agent '{agent_id}'")
    return 0


# --- argument parsing --------------------------------------------------------

EPILOG = """\
Examples:
  sk8 create iris --build                            build image (first time) + provision
  sk8 create iris                                    reuse the image; deploys in seconds
  sk8 create iris --profile ./profiles/data-analyst --build
                                                     bake custom deps/skills/tools into the image
  sk8 create iris --json                             machine-readable {url, token, ...} for agents
  sk8 create iris --dry-run                          preview the gcloud commands, run nothing
  sk8 list                                           list deployed agents in the region
  sk8 delete iris --yes                              tear down the service + token secret
  sk8 suggest 5                                      propose adjective-noun names

Run `sk8 <command> --help` for per-command options.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sk8",
        description=f"{BANNER}\nCreate & manage remote MCP agents on GCP Cloud Run.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # Global verbosity, applied across all subcommands (set in main()).
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true",
                           help="echo each gcloud command before running it")
    verbosity.add_argument("-q", "--quiet", action="store_true",
                           help="suppress progress output (errors still shown)")
    sub = p.add_subparsers(dest="command", required=True)

    def add_gcp_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--project", help="GCP project id (default: gcloud's active project)")
        sp.add_argument("--region", default=DEFAULT_REGION, help=f"Cloud Run region (default: {DEFAULT_REGION})")

    c = sub.add_parser("create", help="provision one MCP agent")
    c.add_argument("agent_id", help="agent id / name, e.g. iris (slugified + validated)")
    add_gcp_opts(c)
    c.add_argument("--repo", default=DEFAULT_REPO, help=f"Artifact Registry repo (default: {DEFAULT_REPO})")
    c.add_argument("--cpu", default="2", help="vCPUs (default: 2)")
    c.add_argument("--memory", default="2Gi", help="memory (default: 2Gi)")
    c.add_argument("--profile", help="path to an agent profile dir (e.g. ./profiles/data-analyst): "
                   "extra Python deps, skills, and a tool/system-prompt spec baked into the image. "
                   "Distinct profiles get distinct image tags; same profile reuses one image.")
    c.add_argument("--bucket", help="GCS bucket to enable file in/out via signed URLs: "
                   "creates it if needed, grants the runtime SA the IAM it needs, and sets "
                   "GCS_BUCKET/AGENT_NAME on the service. Unset = text-only run_task.")
    c.add_argument("--ttl-days", type=int, default=7, metavar="N",
                   help="with --bucket, set a lifecycle rule deleting objects after N days "
                   "(default: 7; 0 = no TTL / keep forever).")
    c.add_argument("--build", action="store_true", help="build & push the image first")
    c.add_argument("--json", action="store_true", help="emit {agent_id,url,token,...} as JSON on stdout (for agents)")
    c.add_argument("--dry-run", action="store_true", help="print the gcloud commands without running them")
    c.set_defaults(func=cmd_create)

    s = sub.add_parser("suggest", help="propose memorable agent names")
    s.add_argument("count", nargs="?", type=int, default=6, help="how many (default: 6)")
    s.set_defaults(func=cmd_suggest)

    ls = sub.add_parser("list", help="list deployed Cloud Run services in the region")
    add_gcp_opts(ls)
    ls.add_argument("--json", action="store_true", help="machine-readable output")
    ls.set_defaults(func=cmd_list)

    d = sub.add_parser("delete", help="tear down an agent (service + token secret)")
    d.add_argument("agent_id", help="agent id to delete")
    add_gcp_opts(d)
    d.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    d.set_defaults(func=cmd_delete)

    return p


def main(argv: list[str] | None = None) -> int:
    global _QUIET, _VERBOSE
    args = build_parser().parse_args(argv)
    _QUIET, _VERBOSE = args.quiet, args.verbose
    eprint(BANNER)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
