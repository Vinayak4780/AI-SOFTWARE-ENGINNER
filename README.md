# AI Software Engineer (aiswe)

A sandboxed autonomous coding agent. Points at a local repo, takes a task
description, plans and edits code, runs tests, and commits -- all inside an
ephemeral, hardened Docker container with no network access by default.

**Free models only.** No Claude, no paid API path -- see "Why no Claude" below.

See [PLAN.md](PLAN.md) for the full architecture and roadmap. This is the
Phase 1 MVP: a single-tenant CLI tool.

## Project layout

```
src/aiswe/
  cli.py              entry point (`aiswe run ...`, `aiswe new ...`, `aiswe serve`)
  server.py            `aiswe serve`: JSON-lines chat protocol for editor front-ends
  approval.py          human approval gate (diff preview + y/N), shared by everything
  model_router.py      picks/orders models from whichever provider keys are in .env
  providers.py         supported platforms + custom endpoints: keys, live model lists, call routing
  agent/
    developer.py        AgentSession (the multi-turn tool-calling loop) + planning phase
    reviewer.py          second-model review gate on every commit attempt
    security.py          secure-coding rules, security commit gate, security review, audit mode
  security/
    redact.py            strips secrets from everything sent to a model provider
    policy.py            protected/secret/sensitive paths, findings, what blocks a commit
    scanners.py          gitleaks + bandit + semgrep, run in the sandbox
    deps.py              new dependencies: exist on PyPI/npm? known-vulnerable (OSV)?
  tools/
    filesystem.py        read_file / list_dir / edit_file / write_file
    terminal.py           run_shell / run_tests
    git.py                 git_commit (+ get_diff, used internally by the reviewer)
    code_search.py         search_code (substring) / find_symbol (AST-based)
    github.py               git_push / create_pull_request / get_issue / comment_issue
    memory.py               update_repo_notes
    security.py             security_scan (lets the model run the scanners)
  sandbox/
    docker.py              the per-task Docker sandbox runtime
    image/Dockerfile        sandbox image (Python, git, gh CLI, gitleaks, bandit, semgrep) -- ships inside the package
    image/semgrep-rules.yml  aiswe's own offline semgrep rules (injection / unsafe APIs)
    image/helper.py         in-container JSON command helper (file/git/AST-symbol ops)
  memory/
    repo_memory.py          reads/writes .aiswe/repo-notes.md
  evaluation/
    tasks.py                fixed evaluation task set
    evaluator.py             runs the task set through the real CLI, scores pass/fail
vscode-extension/      the VS Code chat panel (TypeScript) -- talks to `aiswe serve`
```

`code_intelligence/` as a dedicated package and an `api/` service layer don't
exist -- `find_symbol` lives in `tools/code_search.py` since the AST approach
didn't need its own package, and there's no service/API layer yet (see
PLAN.md's Phase 3). Nothing is stubbed out ahead of being implemented.

## How it works

- **Orchestration** (`agent/developer.py`): a hand-written tool-calling loop
  using [LiteLLM](https://github.com/BerriAI/litellm), which speaks OpenRouter,
  Groq, and other providers through one interface.
- **Model routing** (`model_router.py`): picks a starting model based on
  a simple task-complexity heuristic (a bigger free reasoning model for
  harder/riskier tasks, a small fast one for simple tasks), and automatically
  falls back to the next free model in the chain if one fails or is overloaded.
- **Planning phase**: before implementation starts, a cheap call to the
  fast-tier model chain (independent of the main task's difficulty routing)
  sketches a short plan, giving a real planner/coder/reviewer model split.
- **Tool surface** (`tools/`): a small, constrained ACI (Agent-Computer Interface)
  -- `read_file`, `list_dir`, `search_code`, `find_symbol`, `edit_file`, `write_file`,
  `run_shell`, `run_tests`, `git_commit`, `update_repo_notes`, `git_push`,
  `create_pull_request`, `get_issue`, `comment_issue` -- modeled on SWE-agent's
  ACI pattern, instead of giving the model an unrestricted shell.
- **Reviewer agent** (`agent/reviewer.py`): every `git_commit` attempt is first
  reviewed by a *different* model than the one that wrote the change (a model
  reviewing its own output has blind spots about its own mistakes). If it
  requests changes, the developer loop gets the feedback back as the tool
  result and keeps working, up to 2 rounds per task, before falling through to
  the human approval gate regardless.
- **Repo memory** (`memory/repo_memory.py`): a `.aiswe/repo-notes.md` file the
  agent reads at the start of every task and can update via `update_repo_notes`
  -- durable facts about the repo (architecture, conventions, build/test
  commands) that shouldn't be re-discovered from scratch each run.
- **GitHub integration** (`tools/github.py`): push a branch, open a PR, read/
  comment on an issue, via the `gh` CLI. Needs `--network` (these reach
  github.com) and `GITHUB_TOKEN` set in `.env`. Not live-tested against a real
  repo/token -- see PLAN.md for the caveat.
- **Sandbox** (`sandbox/docker.py`): every task runs in its own ephemeral Docker
  container (`sandbox/image/Dockerfile`), with all capabilities dropped, no new privileges,
  a process-count limit, memory/CPU caps, and network disabled unless `--network`
  is passed. The container mounts your repo at `/workspace`.
- **Approval gate** (`approval.py`): every state-changing tool call is shown to
  you -- as a diff for edits -- before it runs, unless `--yes` is passed.
- **Evaluation harness** (`evaluation/`): `python -m aiswe.evaluation.evaluator`
  runs a fixed task set through the real CLI against fresh scratch repos and
  reports pass/fail per task -- use it to check whether a prompt/model/routing
  change actually helped before assuming it did.

## Security

Two goals: the agent itself can't hurt your machine or leak your secrets, and
the code it writes is checked for vulnerabilities before it's committed.

**Protecting you from the agent**
- Sandbox: ephemeral container, all capabilities dropped, no new privileges,
  non-root user, process/memory/CPU limits, **read-only root filesystem**
  (only `/tmp` and home are writable tmpfs), no network unless `--network`,
  base image pinned by digest, gitleaks pinned by SHA-256.
- **`.git/config` and `.git/hooks` are mounted read-only.** They can make your
  *host* run code (`core.fsmonitor` fires on any `git status`, which editors
  run constantly; hooks fire on your next commit), so the model can't write
  them -- not even via `run_shell`. New repos are `git init`-ed on the host
  first so this protection applies from the first run.
- Edits to files your machine or CI executes (`.vscode/`, `.github/workflows/`,
  `Makefile`, `package.json`, `setup.py`, ...) are flagged on the approval card.
- **Secrets never reach the model provider:** every tool result, diff and
  repo-notes file is redacted (API keys, tokens, private keys, passwords in
  URLs/assignments). Reading a secrets file (`.env`, `*.pem`, `id_rsa`, ...)
  always asks first, even with `--yes`; writing a `[REDACTED:...]` placeholder
  back into a file is refused (it would destroy the real secret).
- `GITHUB_TOKEN` is passed only to the fixed `gh`/`git push` commands, never to
  the container, so model-run shell commands can't read it.
- Prompt injection: file contents, GitHub issues and repo notes are passed as
  untrusted data, and the model is told not to follow instructions in them.
- Approval previews are never truncated. With `--yes --network`, shell and
  GitHub actions still ask (set `AISWE_ALLOW_UNATTENDED_NETWORK=1` to skip).
- Custom endpoints over plain `http://` to a non-local host get a warning (the
  key and your code would travel unencrypted).

**Securing the code it writes**
- Secure-coding rules are part of every task's system prompt.
- **Security gate on every commit:** gitleaks (secrets), bandit (Python) and
  aiswe's semgrep rules (SQL/command injection, eval, unsafe deserialization,
  disabled TLS, XSS... across Python, JS/TS, Go, Java, C) scan the changed
  files; new dependencies are checked to **exist** on PyPI/npm (models invent
  package names that attackers then register) and against the OSV
  vulnerability database. Changes touching auth, crypto, SQL, subprocesses,
  file paths, uploads, CI config etc. also get an AI **security review**.
  HIGH findings go back to the model to fix (2 rounds), then to you on the
  approval card. The gate **fails closed**: if a check can't run, an
  unattended (`--yes`) commit is refused instead of waved through.
- **Audit mode** -- `aiswe audit` (report only), `aiswe audit --fix` (patch,
  add security tests, run them, commit), `aiswe audit "the login API"` to
  focus; in VS Code, the shield button or the *Security mode* checkbox.
- `security_scan` is also a tool the model can use mid-task.

Limits: scanners give leads, not proof, and work best for Python today
(other languages get semgrep and gitleaks). The AI review catches logic flaws
(missing auth checks, rate limiting) that scanners can't, but it's a model --
it can miss things. Treat this as a strong safety net, not a guarantee.

## Why no Claude

The project started on the Claude Agent SDK (see PLAN.md's original architecture
notes) but was deliberately switched to free-only models: Claude has no free
API tier, so any real use would incur per-token cost. The tradeoff, confirmed
in testing: free models are noticeably less reliable (the OpenRouter free
Nvidia-hosted model returns intermittent `503 Service temporarily overloaded`)
and generally weaker at multi-step tool-calling than Claude. The retry +
model-fallback logic in `agent/developer.py` exists specifically to absorb that.

## Install (use it in any folder)

Install once, globally, so the `aiswe` command works everywhere:

```powershell
pip install git+https://github.com/Vinayak4780/AI-SOFTWARE-ENGINNER.git
# or, from a local checkout:  pip install .
# for developing aiswe itself: python -m venv .venv; .venv\Scripts\pip install -e .
```

Requires Docker Desktop running (used to build and run the per-task sandbox
container). The sandbox image is built automatically on first run.

Supported platforms: OpenRouter, Groq, Claude (Anthropic), OpenAI, Google
Gemini, Qwen (Alibaba DashScope), ModelScope, NVIDIA NIM, DeepSeek, Mistral,
xAI, Cerebras, Together, Fireworks, DeepInfra -- plus any **custom
OpenAI-compatible endpoint** (Ollama, LM Studio, vLLM, ...). Add a key for any
of them and `aiswe models` lists every model that platform offers (fetched
live); pass one with `--model <id>`, or leave it out for **Auto** routing,
which tries the hand-verified free models first and then the best coding
models from each platform you've configured, with automatic fallback.

Put your API keys in a global config file, `~/.aiswe/.env`
(`C:\Users\<you>\.aiswe\.env` on Windows), using `.env.example` as the
template. Add a key for at least one platform -- `OPENROUTER_API_KEY`
(https://openrouter.ai/settings/keys) and `GROQ_API_KEY`
(https://console.groq.com/keys) both have free models; more than one is
better, so there's a fallback provider if one is down. (In VS Code you can
enter keys in the chat panel instead -- see below.) Keys are looked up in `./.env` (the folder you run
in), then `~/.aiswe/.env`, then a source checkout's own `.env`; the first
value found wins.

## Usage

`cd` into the project you want it to work on, then:

```powershell
aiswe run "add type hints to utils.py and make sure tests still pass"
aiswe run                                   # prompts for the task
aiswe run "..." --repo "C:\path\to\repo"    # work on a different folder
```

The agent reads, edits, and creates files, runs commands and tests, and
commits -- all inside the sandbox, with the folder mounted at `/workspace`.
Each change is shown to you for approval first unless you pass `--yes`.

### Starting a new project

```powershell
aiswe new my-app "a todo list CLI in Python, with tests"
aiswe new my-app                            # prompts for what to build
```

`aiswe new` creates the folder (it must be missing or empty), runs `git init`
(branch `main`), and has the agent build the project from scratch: source
files, README, `.gitignore`, tests, then a first commit. `aiswe run` in a
folder that isn't a git repo also runs `git init` first, so commits work.
Commits are authored with your host git `user.name`/`user.email`.

Python and C/C++ projects can be run and tested inside the sandbox. Other
languages (Node, Java, Go, Rust, ...) get written but not executed, since
those toolchains aren't in the sandbox image yet.

As a library:

```python
import asyncio
from aiswe import run_task
asyncio.run(run_task("path/to/repo", "fix the failing test", auto_approve=False))
```

For a multi-turn conversation with your own UI, use `AgentSession` (in
`agent/developer.py`): pass an `emit(event_dict)` callback for progress and an
async `approver(tool_name, args) -> bool` for approvals, then
`await session.send(message)` once per message and `session.close()` at the end.

### VS Code chat panel

`vscode-extension/` is a Continue-style chat panel: ask questions or describe
changes, see each proposed edit in VS Code's diff view, Approve/Reject in the
chat. The file you have open (and your selection) is sent with each message.
Pick a model per message from the dropdown (Auto, or any model of any
platform you've added a key for); the gear button opens the keys screen,
where keys and custom endpoints are saved encrypted in VS Code's secret storage.

```powershell
cd vscode-extension
npm install; npm run compile; npm run package     # -> aiswe-0.1.0.vsix
code --install-extension aiswe-0.1.0.vsix
```

It runs `python -m aiswe serve` in your workspace folder, so aiswe must be
pip-installed into the Python the `aiswe.pythonPath` setting points at.
`aiswe serve` speaks JSON lines over stdin/stdout -- the protocol is
documented at the top of `server.py`, so other editors can reuse it.

Security audit:

```powershell
aiswe audit                       # ranked report, no edits
aiswe audit "the login API"       # focus on one area
aiswe audit --fix                 # also patch, add security tests, run them, commit
```

Flags:
- `--network` -- allow network access inside the sandbox (off by default).
- `--yes` -- auto-approve actions instead of prompting for each one.
- `--model NAME` -- force a specific litellm model id, bypassing automatic
  routing/fallback (e.g. `--model groq/llama-3.1-8b-instant`).

## Current limits (see PLAN.md for what's next)

- Single-tenant only; no service/API layer yet (Phase 3).
- Sandbox runtime is standard `runc`; gVisor (`runsc`) hardening is Phase 0/2
  work that requires a real Linux WSL distro (Docker Desktop's own WSL VM
  doesn't support installing a custom container runtime).
- No repo-map/context-ranking yet for large repos (Phase 2).
- Free models are meaningfully weaker and flakier than Claude at multi-step
  coding tasks -- expect more retries, occasional wrong edits, and outright
  task failures if a free provider is overloaded for longer than the retry
  window.
