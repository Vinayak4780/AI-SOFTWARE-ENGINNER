"""Sandbox package. docker.py is the only backend today; a future
Kata/Firecracker backend (see PLAN.md's sandboxing research) would live
alongside it here behind the same Sandbox/build_image interface."""

from .docker import ExecResult, Sandbox, SandboxError, build_image

__all__ = ["ExecResult", "Sandbox", "SandboxError", "build_image"]
