"""Smoke test for the Docker sandbox layer, independent of the LLM agent loop.

Requires Docker Desktop running. Not wired into a CI runner (no CI yet) --
run manually with: .venv\\Scripts\\python.exe tests\\test_sandbox_smoke.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiswe.sandbox import Sandbox, build_image  # noqa: E402


def main() -> None:
    build_image()

    tmp = Path(tempfile.mkdtemp(prefix="aiswe-smoke-"))
    try:
        (tmp / "hello.py").write_text("def greet(name):\n    return 'hi ' + name\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "smoke"], cwd=tmp, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp, check=True)

        with Sandbox(tmp, network=False) as sandbox:
            r = sandbox.call_helper("read_file", path="hello.py")
            assert r["ok"], r
            assert "def greet" in r["content"]
            print("[ok] read_file")

            r = sandbox.call_helper("list_dir", path=".")
            assert r["ok"], r
            assert "hello.py" in r["entries"]
            print("[ok] list_dir")

            r = sandbox.call_helper("search_code", query="greet", path=".")
            assert r["ok"], r
            assert any("hello.py" in m for m in r["matches"])
            print("[ok] search_code")

            r = sandbox.call_helper(
                "edit_file",
                path="hello.py",
                old_string="return 'hi ' + name",
                new_string="return 'hello ' + name",
            )
            assert r["ok"], r
            print("[ok] edit_file")

            r = sandbox.call_helper("read_file", path="hello.py")
            assert "hello " in r["content"], r
            print("[ok] edit persisted")

            exec_result = sandbox.run_shell("echo network_test && (curl -s -m 2 https://example.com > /dev/null 2>&1 && echo NET_UP || echo NET_DOWN)")
            print(f"[info] run_shell output: {exec_result.stdout.strip()!r}")
            assert "NET_DOWN" in exec_result.stdout, "expected network to be disabled by default"
            print("[ok] network isolation confirmed (no egress)")

            r = sandbox.call_helper("git_commit", message="smoke: edit hello.py")
            assert r["ok"], r
            print("[ok] git_commit")

        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=tmp, capture_output=True, text=True, check=True
        )
        assert "smoke: edit hello.py" in log.stdout
        print("[ok] commit landed on host repo (bind mount confirmed)")

        print("\nALL SMOKE CHECKS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
