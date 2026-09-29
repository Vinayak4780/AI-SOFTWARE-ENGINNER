"""CLI entrypoint.

Usage (run from inside the folder you want the agent to work on):
    aiswe run "description" [--repo PATH] [--network] [--yes] [--model NAME]
    aiswe run               # prompts for the task

API keys are read from, in order (first value wins): ./.env in the current
folder, ~/.aiswe/.env (a global config, so an installed aiswe works in any
folder), then the source checkout's own .env.

Which model runs is auto-detected from whatever API key(s) you've put in
.env (see model_router.py) -- add a key, it becomes usable, no code changes.
Free models are preferred by default; see model_router.py for the exact rule.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

# Must run before importing .agent: sandbox/docker.py reads AISWE_DOCKER_RUNTIME
# from the environment at import time, so .env has to be loaded first.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
GLOBAL_CONFIG = Path.home() / ".aiswe" / ".env"
ENV_FILES = (Path.cwd() / ".env", GLOBAL_CONFIG, PROJECT_ROOT / ".env")
for _env_file in ENV_FILES:
    # override=False: earlier files (and real environment variables) win.
    load_dotenv(_env_file, override=False)

from .agent import run_task  # noqa: E402
from .model_router import available_providers  # noqa: E402

# Windows consoles default to a legacy codepage (e.g. cp1252) that can't
# encode characters models routinely emit (arrows, em dashes, checkmarks).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _print_billing_banner() -> None:
    """List which providers are actually usable right now, since a run will
    fail partway through if the model chain picks a provider with no key set."""
    providers = sorted(available_providers())
    if providers:
        print(f"[billing] configured providers: {', '.join(providers)} (see model_router.py for which are free)")
    else:
        print(
            f"[billing] warning: no provider API keys found (looked in ./.env and {GLOBAL_CONFIG}) "
            "-- this run will fail immediately."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiswe", description="AI software engineer -- sandboxed coding agent (free models only)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run the agent on a repo with a task description")
    run.add_argument("task_text", nargs="?", metavar="TASK", help="what to do, in plain English")
    run.add_argument("--task", default=None, help="same as TASK, as a flag")
    run.add_argument("--repo", default=".", help="path to the repo to work on (default: current folder)")
    run.add_argument(
        "--network",
        action="store_true",
        help="allow network access inside the sandbox (default: off)",
    )
    run.add_argument(
        "--yes",
        action="store_true",
        dest="auto_approve",
        help="auto-approve edits/commands/commits instead of prompting (use with care)",
    )
    run.add_argument(
        "--model",
        default=None,
        help=(
            "force a specific litellm model id (e.g. 'groq/llama-3.1-8b-instant'), "
            "bypassing automatic task-based routing and fallback"
        ),
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run":
        repo_path = Path(args.repo).resolve()
        if not repo_path.is_dir():
            print(f"error: no such directory: {repo_path}", file=sys.stderr)
            sys.exit(1)

        task = args.task or args.task_text
        if not task:
            try:
                task = input(f"Task for {repo_path}: ").strip()
            except EOFError:
                task = ""
        if not task:
            print("error: no task given", file=sys.stderr)
            sys.exit(1)

        _print_billing_banner()

        asyncio.run(
            run_task(
                str(repo_path),
                task,
                network=args.network,
                auto_approve=args.auto_approve,
                model=args.model,
            )
        )


if __name__ == "__main__":
    main()
