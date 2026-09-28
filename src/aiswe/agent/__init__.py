"""Agent package: developer.py drives the main tool-calling loop,
reviewer.py gates every commit attempt before it reaches the human.

planner.py and debugger.py (a separate planning pass before implementation,
and a dedicated debug-loop distinct from the main loop) are not built yet --
today's developer.py does both inline. See PLAN.md's Roadmap v2."""

from .developer import run_task

__all__ = ["run_task"]
