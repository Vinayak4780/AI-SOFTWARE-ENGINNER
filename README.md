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
  cli.py              entry point (`aiswe run ...`)
  approval.py          human approval gate (diff preview + y/N), shared by everything
  model_router.py      picks/orders models from whichever provider keys are in .env
  agent/
    developer.py        the main tool-calling loop + planning phase
    reviewer.py          second-model review gate on every commit attempt
  tools/
    filesystem.py        read_file / list_dir / edit_file / write_file
    terminal.py           run_shell / run_tests
    git.py                 git_commit (+ get_diff, used internally by the reviewer)
    code_search.py         search_code (substring) / find_symbol (AST-based)
    github.py               git_push / create_pull_request / get_issue / comment_issue
    memory.py               update_repo_notes
  sandbox/
    docker.py              the per-task Docker sandbox runtime
  memory/
    repo_memory.py          reads/writes .aiswe/repo-notes.md
  evaluation/
    tasks.py                fixed evaluation task set
    evaluator.py             runs the task set through the real CLI, scores pass/fail
docker/
  Dockerfile            sandbox image (Python, git, gh CLI)
  helper.py              in-container JSON command helper (file/git/AST-symbol ops)
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
  container (`docker/Dockerfile`), with all capabilities dropped, no new privileges,
  a process-count limit, memory/CPU caps, and network disabled unless `--network`
  is passed. The container mounts your repo at `/workspace`.
- **Approval gate** (`approval.py`): every state-changing tool call is shown to
  you -- as a diff for edits -- before it runs, unless `--yes` is passed.
- **Evaluation harness** (`evaluation/`): `python -m aiswe.evaluation.evaluator`
  runs a fixed task set through the real CLI against fresh scratch repos and
  reports pass/fail per task -- use it to check whether a prompt/model/routing
  change actually helped before assuming it did.

## Why no Claude

The project started on the Claude Agent SDK (see PLAN.md's original architecture
notes) but was deliberately switched to free-only models: Claude has no free
API tier, so any real use would incur per-token cost. The tradeoff, confirmed
in testing: free models are noticeably less reliable (the OpenRouter free
Nvidia-hosted model returns intermittent `503 Service temporarily overloaded`)
and generally weaker at multi-step tool-calling than Claude. The retry +
model-fallback logic in `agent/developer.py` exists specifically to absorb that.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
```

Requires Docker Desktop running (used to build and run the per-task sandbox
container). The sandbox image is built automatically on first run.

Copy `.env.example` to `.env` and add at least one of `OPENROUTER_API_KEY`
(https://openrouter.ai/settings/keys) or `GROQ_API_KEY`
(https://console.groq.com/keys) -- both is better, so there's a fallback
provider if one is down.

## Usage

```powershell
.venv\Scripts\aiswe run --repo "C:\path\to\some\repo" --task "add type hints to utils.py and make sure tests still pass"
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
