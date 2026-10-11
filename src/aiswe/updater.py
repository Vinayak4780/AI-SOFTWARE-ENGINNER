"""`aiswe update`: pull the latest aiswe from GitHub and reinstall it, plus
the VS Code extension -- one command instead of pip + npm + code steps.

Two kinds of install:
  - a source checkout (`pip install -e .` in a git clone): `git pull` it --
    the editable install picks the new code up as is.
  - a normal pip install: clone the repo to a temp folder and pip-install
    from there (the version number rarely changes, so a plain
    `pip install --upgrade git+...` would skip the update).
Either way the extension is then built from that source and installed with
`code --install-extension`, if npm and the `code` command are available.

AISWE_UPDATE_SOURCE overrides where updates come from (a fork, or a local path).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_SOURCE = "https://github.com/Vinayak4780/AI-SOFTWARE-ENGINNER.git"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class UpdateError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path | None = None, quiet: bool = False) -> str:
    print(f"> {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=quiet,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if quiet else ""
        raise UpdateError(f"command failed (exit {result.returncode}): {' '.join(cmd)}" + (f"\n{detail}" if detail else ""))
    return (result.stdout or "").strip() if quiet else ""


def _tool(name: str) -> str | None:
    """Full path (npm/code are .cmd scripts on Windows, which need the real path)."""
    return shutil.which(name)


def _source_checkout() -> Path | None:
    """The git clone aiswe is running from, for an editable install."""
    if (PROJECT_ROOT / ".git").exists() and (PROJECT_ROOT / "pyproject.toml").exists():
        return PROJECT_ROOT
    return None


def _git() -> str:
    git = _tool("git")
    if not git:
        raise UpdateError("git isn't installed (or not on PATH) -- install it from https://git-scm.com and try again.")
    return git


def _update_checkout(checkout: Path) -> bool:
    """git pull the checkout; True if it changed."""
    git = _git()
    print(f"[update] aiswe is installed from the source folder {checkout} -- pulling the latest changes")
    before = _run([git, "rev-parse", "HEAD"], cwd=checkout, quiet=True)
    _run([git, "pull", "--ff-only"], cwd=checkout)
    after = _run([git, "rev-parse", "HEAD"], cwd=checkout, quiet=True)
    if before == after:
        print("[update] aiswe is already up to date")
        return False
    log = _run([git, "log", "--oneline", f"{before}..{after}"], cwd=checkout, quiet=True)
    print(f"[update] new changes:\n{log}")
    # A pull can add dependencies; reinstalling the editable package fetches them.
    _run([sys.executable, "-m", "pip", "install", "--quiet", "-e", str(checkout)])
    return True


def _update_pip_install(source: str, workdir: Path) -> Path:
    """Clone `source` and reinstall aiswe from it; returns the clone."""
    git = _git()
    clone = workdir / "aiswe-src"
    print(f"[update] downloading the latest aiswe from {source}")
    _run([git, "clone", "--depth", "1", "--quiet", source, str(clone)])
    print(f"[update] latest version: {_run([git, 'log', '-1', '--format=%h %s'], cwd=clone, quiet=True)}")
    pip = [sys.executable, "-m", "pip", "install", "--quiet"]
    # Same version number as before, so force the reinstall of aiswe itself
    # (only -- not every dependency), then let pip add any new dependencies.
    _run(pip + ["--force-reinstall", "--no-deps", str(clone)])
    _run(pip + [str(clone)])
    return clone


def _update_extension(source_dir: Path, workdir: Path) -> None:
    ext_dir = source_dir / "vscode-extension"
    if not (ext_dir / "package.json").exists():
        print("[update] no vscode-extension folder in this version -- skipping the VS Code extension")
        return
    npm, code = _tool("npm"), _tool("code")
    if not npm or not code:
        missing = " and ".join(n for n, p in (("npm (Node.js)", npm), ("the `code` command", code)) if not p)
        print(f"[update] skipping the VS Code extension: {missing} not found. Install Node.js from "
              "https://nodejs.org and, in VS Code, run 'Shell Command: Install code command in PATH' if needed.")
        return
    print("[update] building the VS Code extension")
    _run([npm, "ci" if (ext_dir / "package-lock.json").exists() else "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=ext_dir)
    _run([npm, "run", "compile"], cwd=ext_dir)
    vsix = workdir / "aiswe.vsix"
    _run([npm, "run", "package", "--", "--out", str(vsix)], cwd=ext_dir)
    _run([code, "--install-extension", str(vsix), "--force"])


def update(skip_extension: bool = False, force: bool = False) -> None:
    source = os.environ.get("AISWE_UPDATE_SOURCE", "").strip() or DEFAULT_SOURCE
    checkout = _source_checkout()
    with tempfile.TemporaryDirectory(prefix="aiswe-update-") as tmp:
        workdir = Path(tmp)
        if checkout:
            changed = _update_checkout(checkout)
            source_dir = checkout
        else:
            source_dir = _update_pip_install(source, workdir)
            changed = True
        if skip_extension:
            pass
        elif changed or force:
            _update_extension(source_dir, workdir)
        else:
            print("[update] nothing new, so the VS Code extension wasn't rebuilt (use --force to rebuild it anyway)")
    print("\n[update] done. In VS Code press Ctrl+Shift+P -> 'Developer: Reload Window' to use the new version.")


def main(skip_extension: bool = False, force: bool = False) -> None:
    try:
        update(skip_extension=skip_extension, force=force)
    except UpdateError as e:
        print(f"\n[update] failed: {e}", file=sys.stderr)
        sys.exit(1)
