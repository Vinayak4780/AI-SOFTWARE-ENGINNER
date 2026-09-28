"""Docker-based sandbox runtime.

Each task gets one ephemeral, hardened container: no capabilities, no new
privileges, a process-count limit, a memory/CPU cap, and no network access
unless explicitly requested. The container mounts a single host repo at
/workspace and is destroyed when the task ends.

The container runtime defaults to the standard "runc". Once gVisor is
installed (see PLAN.md, Phase 0/2), pass runtime="runsc" for hardened
syscall-level isolation with no code changes needed elsewhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IMAGE_NAME = "aiswe-sandbox:latest"
DOCKER_DIR = Path(__file__).resolve().parents[3] / "docker"
HELPER_PATH_IN_CONTAINER = "/usr/local/bin/aiswe_helper.py"
DEFAULT_RUNTIME = os.environ.get("AISWE_DOCKER_RUNTIME", "runc")


class SandboxError(RuntimeError):
    """Raised when the Docker sandbox can't be built, started, or reached."""


def build_image() -> None:
    """Build (or rebuild) the sandbox image. Cheap when layers are cached."""
    result = subprocess.run(
        ["docker", "build", "-t", IMAGE_NAME, str(DOCKER_DIR)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SandboxError(f"failed to build sandbox image:\n{result.stderr}")


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class Sandbox:
    """One ephemeral, hardened Docker container mounted on a single repo."""

    def __init__(
        self,
        repo_path: str | Path,
        *,
        network: bool = False,
        memory: str = "1g",
        cpus: str = "2",
        runtime: str = DEFAULT_RUNTIME,
    ) -> None:
        self.repo_path = str(Path(repo_path).resolve())
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.runtime = runtime
        self.container_name = f"aiswe-task-{uuid.uuid4().hex[:12]}"
        self._started = False

    def start(self) -> None:
        cmd = [
            "docker", "run", "-d", "--rm",
            "--name", self.container_name,
            "--runtime", self.runtime,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--network", "bridge" if self.network else "none",
            "-v", f"{self.repo_path}:/workspace",
            "-w", "/workspace",
        ]
        # Explicit allowlist only -- never blanket-forward the host environment
        # into the sandbox. GH_TOKEN/GITHUB_TOKEN are what `gh` (tools/github.py)
        # authenticates with inside the container.
        for env_var in ("GH_TOKEN", "GITHUB_TOKEN"):
            value = os.environ.get(env_var)
            if value:
                cmd += ["-e", f"{env_var}={value}"]
        cmd += [IMAGE_NAME, "sleep", "infinity"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise SandboxError(f"failed to start sandbox container:\n{result.stderr}")
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        subprocess.run(
            ["docker", "stop", "-t", "5", self.container_name],
            capture_output=True,
            text=True,
        )
        self._started = False

    def __enter__(self) -> "Sandbox":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def run_shell(self, command: str, timeout: int = 120) -> ExecResult:
        try:
            result = subprocess.run(
                ["docker", "exec", self.container_name, "sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
            return ExecResult(124, stdout, stderr + "\n[command timed out]")
        return ExecResult(result.returncode, result.stdout, result.stderr)

    def call_helper(self, action: str, timeout: int = 30, **kwargs: Any) -> dict[str, Any]:
        payload = json.dumps({"action": action, **kwargs})
        try:
            result = subprocess.run(
                ["docker", "exec", "-i", self.container_name, "python3", HELPER_PATH_IN_CONTAINER],
                input=payload,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "helper call timed out"}
        if not result.stdout.strip():
            return {"ok": False, "error": result.stderr.strip() or "helper produced no output"}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"bad helper output: {result.stdout!r} {result.stderr!r}"}
