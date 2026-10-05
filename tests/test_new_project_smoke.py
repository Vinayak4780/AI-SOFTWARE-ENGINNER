"""Smoke test for creating a brand-new project in an empty, non-git folder --
the `aiswe new` path -- independent of the LLM agent loop.

Requires Docker Desktop running. Not wired into a CI runner (no CI yet) --
run manually with: .venv\\Scripts\\python.exe tests\\test_new_project_smoke.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiswe.agent.developer import _in_git_repo  # noqa: E402
from aiswe.sandbox import Sandbox, build_image  # noqa: E402


def main() -> None:
    build_image()

    tmp = Path(tempfile.mkdtemp(prefix="aiswe-new-"))
    try:
        assert not _in_git_repo(str(tmp)), "temp folder unexpectedly inside a git repo"
        print("[ok] empty folder detected as not a git repo")

        with Sandbox(tmp, network=False) as sandbox:
            sandbox.prepare_git("Smoke Tester", "smoke@example.com", init=True)
            assert (tmp / ".git").is_dir(), "git init didn't land on the host folder"
            print("[ok] git init (visible on host)")

            r = sandbox.call_helper(
                "write_file",
                path="src/calc/core.py",
                content="def add(a, b):\n    return a + b\n",
            )
            assert r["ok"], r
            r = sandbox.call_helper("write_file", path="src/calc/__init__.py", content="")
            assert r["ok"], r
            r = sandbox.call_helper(
                "write_file",
                path="tests/test_core.py",
                content=(
                    "import sys, pathlib\n"
                    "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))\n"
                    "from calc.core import add\n\n"
                    "def test_add():\n    assert add(2, 3) == 5\n"
                ),
            )
            assert r["ok"], r
            print("[ok] write_file created nested folders")

            result = sandbox.run_shell("pytest -q", timeout=120)
            assert result.exit_code == 0, result.stdout + result.stderr
            print("[ok] tests run and pass in the new project")

            r = sandbox.call_helper("git_diff")
            assert r["ok"], r
            assert "src/calc/core.py" in r["diff"], r
            print("[ok] reviewer diff works before the first commit")

            r = sandbox.call_helper("git_commit", message="Initial project")
            assert r["ok"], r
            print("[ok] first git_commit (no 'who are you' identity error)")

        log = subprocess.run(
            ["git", "log", "--format=%an <%ae> %s", "--", "."],
            cwd=tmp, capture_output=True, text=True, check=True,
        )
        assert "Smoke Tester <smoke@example.com> Initial project" in log.stdout, log.stdout
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=tmp, capture_output=True, text=True, check=True
        )
        assert branch.stdout.strip() == "main", branch.stdout
        print("[ok] commit landed on host, on branch main, with the given author")

        print("\nALL NEW-PROJECT SMOKE CHECKS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
