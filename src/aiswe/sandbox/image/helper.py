#!/usr/bin/env python3
"""In-container helper: reads one JSON command from stdin, performs a file/git
action confined to /workspace, writes one JSON result to stdout.

Runs as the unprivileged sandbox user, inside the ephemeral per-task container.
All paths are resolved relative to /workspace and rejected if they'd escape it.
"""
import ast
import json
import subprocess
import sys
from pathlib import Path

WORKDIR = Path("/workspace").resolve()


def _resolve(path: str) -> Path:
    candidate = (WORKDIR / path).resolve()
    if candidate != WORKDIR and WORKDIR not in candidate.parents:
        raise ValueError(f"path escapes workspace: {path}")
    return candidate


def _resolve_writable(path: str) -> Path:
    candidate = _resolve(path)
    git_dir = WORKDIR / ".git"
    if candidate == git_dir or git_dir in candidate.parents:
        raise ValueError(f"refusing to modify git internals: {path}")
    return candidate


def read_file(path, **_):
    p = _resolve(path)
    return {"ok": True, "content": p.read_text(errors="replace")}


def list_dir(path=".", **_):
    p = _resolve(path)
    entries = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
    return {"ok": True, "entries": entries}


def search_code(query, path=".", max_results=200, **_):
    p = _resolve(path)
    matches = []
    for f in sorted(p.rglob("*")):
        if f.is_dir() or ".git" in f.parts:
            continue
        try:
            text = f.read_text(errors="ignore")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if query in line:
                matches.append(f"{f.relative_to(WORKDIR)}:{i}:{line.strip()}")
                if len(matches) >= max_results:
                    return {"ok": True, "matches": matches}
    return {"ok": True, "matches": matches}


def find_symbol(name, path=".", **_):
    """AST-based symbol search across .py files -- exact function/class
    definitions by name, not a substring grep. Python-only for now (see
    PLAN.md's code intelligence roadmap item for multi-language/tree-sitter);
    stdlib ast is used deliberately instead of adding a tree-sitter dependency
    to the sandbox image."""
    p = _resolve(path)
    matches = []
    for f in sorted(p.rglob("*.py")):
        if ".git" in f.parts:
            continue
        try:
            source = f.read_text(errors="ignore")
            tree = ast.parse(source, filename=str(f))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                matches.append(f"{f.relative_to(WORKDIR)}:{node.lineno}: {kind} {node.name}")
    return {"ok": True, "matches": matches}


def edit_file(path, old_string, new_string, **_):
    p = _resolve_writable(path)
    text = p.read_text()
    count = text.count(old_string)
    if count == 0:
        return {"ok": False, "error": "old_string not found in file"}
    if count > 1:
        return {
            "ok": False,
            "error": f"old_string is not unique ({count} occurrences); include more context",
        }
    p.write_text(text.replace(old_string, new_string, 1))
    return {"ok": True}


def write_file(path, content, **_):
    p = _resolve_writable(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return {"ok": True}


def git_commit(message, **_):
    add = subprocess.run(["git", "add", "-A"], cwd=WORKDIR, capture_output=True, text=True)
    if add.returncode != 0:
        return {"ok": False, "error": add.stderr}
    commit = subprocess.run(
        ["git", "commit", "-m", message], cwd=WORKDIR, capture_output=True, text=True
    )
    if commit.returncode != 0:
        return {"ok": False, "error": commit.stderr or commit.stdout}
    return {"ok": True, "output": commit.stdout}


def git_diff(**_):
    """Stages everything (including new files) and returns the full diff
    against HEAD, without committing -- used by the reviewer agent to see
    exactly what a proposed commit would contain."""
    add = subprocess.run(["git", "add", "-A"], cwd=WORKDIR, capture_output=True, text=True)
    if add.returncode != 0:
        return {"ok": False, "error": add.stderr}
    diff = subprocess.run(["git", "diff", "--cached"], cwd=WORKDIR, capture_output=True, text=True)
    if diff.returncode != 0:
        return {"ok": False, "error": diff.stderr}
    return {"ok": True, "diff": diff.stdout}


ACTIONS = {
    "read_file": read_file,
    "list_dir": list_dir,
    "search_code": search_code,
    "find_symbol": find_symbol,
    "edit_file": edit_file,
    "write_file": write_file,
    "git_commit": git_commit,
    "git_diff": git_diff,
}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad json input: {e}"}))
        return
    action = payload.get("action")
    fn = ACTIONS.get(action)
    if fn is None:
        print(json.dumps({"ok": False, "error": f"unknown action: {action}"}))
        return
    kwargs = {k: v for k, v in payload.items() if k != "action"}
    try:
        result = fn(**kwargs)
    except Exception as e:  # noqa: BLE001 -- reported back as tool output, not raised
        result = {"ok": False, "error": str(e)}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
