"""Repo memory: durable per-repo notes (architecture, conventions, build/test
commands, known issues) that shouldn't be re-discovered from scratch every
run. Stored as a plain markdown file inside the repo itself, so it travels
with the repo and the user can inspect/edit/commit it like any other file.

Short-term (current-task) memory is just the message history within one
run_task() call -- no separate module needed for that. Long-term memory
*across* repos isn't built (see PLAN.md Roadmap v2) -- lowest priority until
aiswe is used across many projects regularly.
"""

from __future__ import annotations

from pathlib import Path

NOTES_RELPATH = ".aiswe/repo-notes.md"


def read_notes(repo_path: str | Path) -> str:
    p = Path(repo_path) / NOTES_RELPATH
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="replace")


def write_notes(repo_path: str | Path, content: str) -> None:
    p = Path(repo_path) / NOTES_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
