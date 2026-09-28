"""Evaluation harness: runs the fixed task set (tasks.py) against fresh
scratch repos through the real `aiswe run` CLI path (not an in-process
shortcut -- this tests what a user actually runs), then scores each task by
whether its test suite passes afterward. See PLAN.md Roadmap v2.

Usage:
    .venv\\Scripts\\python.exe -m aiswe.evaluation.evaluator
    .venv\\Scripts\\python.exe -m aiswe.evaluation.evaluator --model groq/llama-3.1-8b-instant

Nothing before this file measured whether a prompt/model/routing change made
the agent better or worse -- run this before and after such a change to get
an actual number instead of a guess.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .tasks import TASKS, EvalTask

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable


@dataclass
class EvalResult:
    task_id: str
    passed: bool
    duration_s: float
    detail: str = ""


def _init_repo(task: EvalTask, repo_dir: Path) -> None:
    for relpath, content in task.files.items():
        p = repo_dir / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "eval@aiswe.local"],
        ["git", "config", "user.name", "aiswe-eval"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=repo_dir, check=True, capture_output=True, text=True)


def run_one(task: EvalTask, *, model: str | None = None) -> EvalResult:
    repo_dir = Path(tempfile.mkdtemp(prefix=f"aiswe-eval-{task.id}-"))
    start = time.time()
    try:
        _init_repo(task, repo_dir)

        cmd = [PYTHON, "-m", "aiswe.cli", "run", "--repo", str(repo_dir), "--task", task.task, "--yes"]
        if model:
            cmd += ["--model", model]
        run_result = subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=task.timeout_s
        )

        test_result = subprocess.run(
            [PYTHON, "-m", "pytest", "-q"], cwd=repo_dir, capture_output=True, text=True, timeout=60
        )
        passed = test_result.returncode == 0
        detail = "" if passed else (test_result.stdout + test_result.stderr)[-1500:] or run_result.stdout[-1500:]
        return EvalResult(task.id, passed, time.time() - start, detail)
    except subprocess.TimeoutExpired:
        return EvalResult(task.id, False, time.time() - start, detail="timed out")
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run aiswe's fixed evaluation task set")
    parser.add_argument("--model", default=None, help="force a specific litellm model id for every task")
    args = parser.parse_args()

    results = [run_one(t, model=args.model) for t in TASKS]

    passed = sum(r.passed for r in results)
    print(f"\n=== aiswe evaluation: {passed}/{len(results)} passed ===")
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.task_id} ({r.duration_s:.1f}s)")
        if not r.passed and r.detail:
            print(f"       {r.detail[:300]}")


if __name__ == "__main__":
    main()
