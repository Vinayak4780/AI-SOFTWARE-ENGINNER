"""AI Software Engineer -- usable as a CLI (`aiswe run`) or as a library:

    import asyncio
    from aiswe import run_task
    asyncio.run(run_task("path/to/repo", "fix the failing test", auto_approve=False))

As a library, provider keys come from the process environment (the CLI's
.env loading lives in cli.py) -- call dotenv.load_dotenv() yourself if needed.
"""

__version__ = "0.1.0"
__all__ = ["run_task", "Sandbox"]


def __getattr__(name: str):
    # Lazy, so `import aiswe` stays cheap and doesn't pull in litellm.
    if name == "run_task":
        from .agent import run_task
        return run_task
    if name == "Sandbox":
        from .sandbox import Sandbox
        return Sandbox
    raise AttributeError(f"module 'aiswe' has no attribute {name!r}")
