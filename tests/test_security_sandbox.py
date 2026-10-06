"""Live security checks against the real Docker sandbox (no model calls):
the hardening actually holds, and the scanners actually find planted issues.

Requires Docker Desktop running; builds the sandbox image on first run.
Run with: .venv\\Scripts\\python.exe tests\\test_security_sandbox.py  (or pytest)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiswe.sandbox import Sandbox, build_image  # noqa: E402
from aiswe.security import scanners  # noqa: E402
from aiswe.tools.git import get_diff  # noqa: E402

# Built at runtime so this file holds no secret-looking literal (gitleaks
# deliberately ignores AWS's documented EXAMPLE keys, so it must look real).
FAKE_SECRET = "Zk8f3Qw9Lm2Xv7" + "Rt1Nb4Hy6Jp0Cd5Gs8Ae3Ui7Ko"

VULNERABLE_PY = '''import sqlite3
import subprocess

aws_secret = "%s"


def find_user(conn: sqlite3.Connection, name: str):
    return conn.cursor().execute(f"SELECT * FROM users WHERE name = '{name}'").fetchall()


def ping(host: str) -> None:
    subprocess.run("ping -c 1 " + host, shell=True)
''' % FAKE_SECRET

VULNERABLE_JS = '''const child_process = require("child_process");
function run(cmd) { child_process.exec(cmd); }
function show(el, html) { el.innerHTML = html; }
'''


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_sandbox_security() -> None:
    build_image()
    repo = Path(tempfile.mkdtemp(prefix="aiswe-sec-"))
    os.environ["GH_TOKEN"] = "fake-token-for-test"
    try:
        (repo / "README.md").write_text("hi\n")
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@t.t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        config_before = (repo / ".git" / "config").read_text()

        with Sandbox(repo) as sb:
            sb.prepare_git("t", "t@t.t", init=False)

            # .git/config and hooks: read-only, even through a raw shell.
            assert sb.run_shell("echo '[core] fsmonitor = evil' >> .git/config").exit_code != 0
            assert sb.run_shell("echo 'evil' > .git/hooks/pre-commit").exit_code != 0
            assert not sb.call_helper("write_file", path=".git/hooks/post-checkout", content="x").get("ok")
            assert (repo / ".git" / "config").read_text() == config_before
            assert not (repo / ".git" / "hooks" / "pre-commit").exists()

            # Read-only image filesystem; /tmp and home writable.
            assert sb.run_shell("touch /usr/local/evil").exit_code != 0
            assert sb.run_shell("touch /tmp/ok && touch ~/ok").exit_code == 0

            # The token is not in the container, only in commands that ask for it.
            assert "fake-token-for-test" not in sb.run_shell("env").stdout
            assert "fake-token-for-test" in sb.run_shell("echo $GH_TOKEN", env=("GH_TOKEN",)).stdout

            # The scanners find the planted issues. (Not `semgrep --validate`: it
            # downloads a lint ruleset, and the sandbox has no network. A rule
            # that fails to load shows up as a scan error, asserted below.)
            (repo / "app.py").write_text(VULNERABLE_PY)
            (repo / "web.js").write_text(VULNERABLE_JS)
            diff = get_diff(sb)  # stages everything, like the commit gate does
            result = scanners.scan(sb, scanners.changed_files(diff), staged=True)
            print(result.summary())
            assert not result.errors, result.errors
            rules = {(f.tool, f.rule.split(":")[0]) for f in result.findings}
            assert ("semgrep", "py-sql-string-building") in rules
            assert ("semgrep", "py-subprocess-shell-true") in rules
            assert ("semgrep", "js-child-process-exec") in rules
            assert ("semgrep", "js-dom-xss") in rules
            assert ("bandit", "B602") in rules
            assert any(f.tool == "gitleaks" for f in result.findings), "gitleaks missed the secret"
            assert FAKE_SECRET not in result.summary()  # bandit quotes it; the summary must not

            # Commits still work with the read-only git config.
            assert sb.call_helper("git_commit", message="test").get("ok")

            # Whole-repo scan (audit mode) works too.
            assert not scanners.scan(sb).errors
    finally:
        os.environ.pop("GH_TOKEN", None)
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    test_sandbox_security()
    print("sandbox security test passed")
