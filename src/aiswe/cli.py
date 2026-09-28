"""CLI entrypoint.

Usage:
    aiswe run --repo PATH --task "description" [--network] [--yes] [--model NAME]

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
load_dotenv(PROJECT_ROOT / ".env")

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
        print("[billing] warning: no provider API keys found in .env -- this run will fail immediately.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiswe", description="AI software engineer -- sandboxed coding agent (free models only)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run the agent on a repo with a task description")
    run.add_argument("--repo", required=True, help="path to the local repo to work on")
    run.add_argument("--task", required=True, help="what to do, in plain English")
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

        _print_billing_banner()

        asyncio.run(
            run_task(
                str(repo_path),
                args.task,
                network=args.network,
                auto_approve=args.auto_approve,
                model=args.model,
            )
        )


if __name__ == "__main__":
    main()
