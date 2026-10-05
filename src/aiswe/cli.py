"""CLI entrypoint.

Usage (run from inside the folder you want the agent to work on):
    aiswe run "description" [--repo PATH] [--network] [--yes] [--model NAME]
    aiswe run               # prompts for the task
    aiswe new FOLDER "description" [--network] [--yes] [--model NAME]
                            # create a brand-new project from scratch in FOLDER
    aiswe models              # list configured providers and their models
    aiswe serve [--repo PATH] [--network] [--yes] [--model NAME]
                            # JSON-lines chat server for editor front-ends (see server.py)

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
from .providers import all_providers, list_all_models  # noqa: E402
from .server import serve  # noqa: E402

# Windows consoles default to a legacy codepage (e.g. cp1252) that can't
# encode characters models routinely emit (arrows, em dashes, checkmarks).
for _stream in (sys.stdin, sys.stdout, sys.stderr):
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


def _print_models() -> None:
    listed = list_all_models()
    if not listed:
        print(f"No providers configured. Add a key to ./.env or {GLOBAL_CONFIG} -- supported:")
        for p in all_providers():
            print(f"  {p.label:28} {p.key_envs[0]:22} {p.key_url}")
        return
    for pm in listed:
        print(f"\n{pm.provider.label} ({len(pm.models)} models){'  -- ' + pm.error if pm.error else ''}")
        for m in pm.models:
            print(f"  {m.id}{'  [free]' if m.free else ''}")
    print("\nUse one with --model <id>, or leave it out for automatic routing.")


def _add_agent_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--network",
        action="store_true",
        help="allow network access inside the sandbox (default: off)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        dest="auto_approve",
        help="auto-approve edits/commands/commits instead of prompting (use with care)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "force a specific litellm model id (e.g. 'groq/llama-3.1-8b-instant'), "
            "bypassing automatic task-based routing and fallback"
        ),
    )


def _get_task(args: argparse.Namespace, prompt: str) -> str:
    task = args.task or args.task_text
    if not task:
        try:
            task = input(prompt).strip()
        except EOFError:
            task = ""
    if not task:
        print("error: no task given", file=sys.stderr)
        sys.exit(1)
    return task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aiswe", description="AI software engineer -- sandboxed coding agent (free models only)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run the agent on a repo with a task description")
    run.add_argument("task_text", nargs="?", metavar="TASK", help="what to do, in plain English")
    run.add_argument("--task", default=None, help="same as TASK, as a flag")
    run.add_argument("--repo", default=".", help="path to the repo to work on (default: current folder)")
    _add_agent_options(run)

    new = subparsers.add_parser("new", help="create a brand-new project from scratch in a new (or empty) folder")
    new.add_argument("folder", help="folder to create the project in (created if missing; must be empty)")
    new.add_argument("task_text", nargs="?", metavar="TASK", help="what to build, e.g. 'a todo CLI in Python with tests'")
    new.add_argument("--task", default=None, help="same as TASK, as a flag")
    _add_agent_options(new)

    subparsers.add_parser("models", help="list the providers you have keys for and every model they offer")

    srv = subparsers.add_parser("serve", help="chat server over stdin/stdout (JSON lines), used by the VS Code extension")
    srv.add_argument("--repo", default=".", help="path to the repo to work on (default: current folder)")
    _add_agent_options(srv)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "models":
        _print_models()
        return

    if args.command == "serve":
        repo_path = Path(args.repo).resolve()
        if not repo_path.is_dir():
            print(f"error: no such directory: {repo_path}", file=sys.stderr)
            sys.exit(1)
        asyncio.run(serve(str(repo_path), network=args.network, auto_approve=args.auto_approve, model=args.model))
        return

    if args.command == "run":
        repo_path = Path(args.repo).resolve()
        if not repo_path.is_dir():
            print(
                f"error: no such directory: {repo_path}\n"
                f"(to start a new project there, use: aiswe new {args.repo} \"what to build\")",
                file=sys.stderr,
            )
            sys.exit(1)
        task = _get_task(args, f"Task for {repo_path}: ")
        new_project = False

    elif args.command == "new":
        repo_path = Path(args.folder).resolve()
        if repo_path.exists() and (not repo_path.is_dir() or any(repo_path.iterdir())):
            print(
                f"error: {repo_path} already exists and isn't an empty folder\n"
                f"(to work on an existing project, use: aiswe run --repo {args.folder} \"task\")",
                file=sys.stderr,
            )
            sys.exit(1)
        task = _get_task(args, f"What should the new project in {repo_path} be? ")
        repo_path.mkdir(parents=True, exist_ok=True)
        print(f"[new] creating project in {repo_path}")
        new_project = True

    _print_billing_banner()

    asyncio.run(
        run_task(
            str(repo_path),
            task,
            network=args.network,
            auto_approve=args.auto_approve,
            model=args.model,
            new_project=new_project,
        )
    )


if __name__ == "__main__":
    main()
