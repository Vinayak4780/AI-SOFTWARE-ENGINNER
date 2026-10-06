"""Dependency check for packages a change adds (requirements*.txt,
pyproject.toml, package.json). Runs on the host -- it needs the network,
which the sandbox normally doesn't have -- and only sends package names and
versions, never code:

  * the package must exist on PyPI / npm. Models invent plausible package
    names, and attackers register those names ("slopsquatting"), so a
    missing package is a blocking finding, not a warning.
  * pinned versions are looked up in the OSV vulnerability database
    (https://osv.dev) -- a known-vulnerable version blocks the commit.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .policy import Finding, GateResult

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Dep:
    ecosystem: str  # "PyPI" | "npm"
    name: str
    version: str | None
    path: str
    new: bool = True  # False = an existing dependency whose version changed


_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:==\s*([A-Za-z0-9.+!-]+))?")


def _pep508(spec: str) -> tuple[str, str | None] | None:
    m = _REQ_LINE.match(spec)
    if not m:
        return None
    return m.group(1).lower().replace("_", "-"), m.group(2)


def parse_manifest(path: str, text: str) -> set[tuple[str, str, str | None]]:
    """(ecosystem, name, version-or-None) for every dependency in a manifest."""
    name = Path(path).name
    deps: set[tuple[str, str, str | None]] = set()
    if not text.strip():
        return deps
    if re.fullmatch(r"requirements.*\.(txt|in)", name):
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith(("-", "git+", "http:", "https:", "file:")):
                continue
            parsed = _pep508(line)
            if parsed:
                deps.add(("PyPI", *parsed))
    elif name == "pyproject.toml" and tomllib is not None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return deps
        project = data.get("project", {})
        specs = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            specs += group
        for spec in specs:
            parsed = _pep508(str(spec))
            if parsed:
                deps.add(("PyPI", *parsed))
    elif name == "package.json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return deps
        for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            for pkg, spec in (data.get(section) or {}).items():
                version = re.match(r"^[\^~=v]*(\d+\.\d+\.\d+[\w.+-]*)$", str(spec).strip())
                deps.add(("npm", pkg, version.group(1) if version else None))
    return deps


MANIFEST = re.compile(r"(^|/)(requirements[^/]*\.(txt|in)|pyproject\.toml|package\.json)$")


def added_dependencies(path: str, old_text: str, new_text: str) -> list[Dep]:
    old = parse_manifest(path, old_text)
    old_names = {(e, n) for e, n, _ in old}
    added = sorted(parse_manifest(path, new_text) - old, key=lambda d: (d[0], d[1], d[2] or ""))
    return [Dep(eco, name, version, path, new=(eco, name) not in old_names) for eco, name, version in added]


def _http_json(url: str, body: dict | None = None) -> dict | None:
    """Parsed JSON, or None on 404. Other errors raise."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"User-Agent": "aiswe", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 -- fixed public registries
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _exists(dep: Dep) -> bool:
    if dep.ecosystem == "PyPI":
        url = f"https://pypi.org/pypi/{urllib.parse.quote(dep.name)}/json"
    else:
        url = f"https://registry.npmjs.org/{urllib.parse.quote(dep.name, safe='@')}"
    return _http_json(url) is not None


def _vulns(deps: list[Dep]) -> list[list[str]]:
    queries = [{"package": {"name": d.name, "ecosystem": d.ecosystem}, "version": d.version} for d in deps]
    response = _http_json("https://api.osv.dev/v1/querybatch", {"queries": queries}) or {}
    return [[v["id"] for v in r.get("vulns", [])] for r in response.get("results", [])]


def check(deps: list[Dep], *, exists: Callable[[Dep], bool] = _exists,
          vulns: Callable[[list[Dep]], list[list[str]]] = _vulns) -> GateResult:
    result = GateResult()
    for dep in (d for d in deps if d.new):
        try:
            if not exists(dep):
                result.findings.append(Finding(
                    "deps", "high", dep.path, 0, "unknown-package",
                    f"{dep.name} does not exist on {dep.ecosystem} -- a typo or an invented name. "
                    "Attackers register such names; double-check the real package name.",
                ))
        except Exception as e:  # noqa: BLE001 -- network trouble is a scan error, not a finding
            result.errors.append(f"couldn't check {dep.name} on {dep.ecosystem}: {type(e).__name__}: {e}")
    pinned = [d for d in deps if d.version]
    if pinned:
        try:
            for dep, ids in zip(pinned, vulns(pinned)):
                if ids:
                    result.findings.append(Finding(
                        "deps", "high", dep.path, 0, "known-vulnerability",
                        f"{dep.name}=={dep.version} has known vulnerabilities ({', '.join(ids[:5])}) -- use a fixed version.",
                    ))
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"OSV vulnerability lookup failed: {type(e).__name__}: {e}")
    return result
