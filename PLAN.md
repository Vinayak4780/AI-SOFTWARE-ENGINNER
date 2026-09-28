# AI Software Engineer — Project Plan

Goal: build an autonomous coding agent ("AI software engineer") that works first as a personal local tool and is architected from day one to scale into a hosted, multi-tenant product. Built in Python, studying/adapting existing open-source agent architectures rather than starting from zero. Sandboxing must be free and self-hostable while supporting multiple tenants. Model calls must be free (no per-token cost) unless a paid provider is added and named explicitly.

## Current status (2026-09-25)

Phase 1 MVP is built and working, verified with real end-to-end runs (not just code review) — see `README.md` for setup/usage and the project layout. What's actually in place, since it has drifted from the original architecture bets below:

- **Orchestration**: a hand-rolled tool-calling loop (`agent/developer.py`) using LiteLLM, **not** the Claude Agent SDK (dropped — no free tier, see "Why no Claude" in README.md) and **not yet** LangGraph/deepagents (see "Open decision" below).
- **Model routing** (`model_router.py`): auto-detects which provider API keys are present in `.env` (OpenRouter, Groq, Gemini, Cerebras, Anthropic, OpenAI, Mistral, Together, Fireworks, DeepInfra, xAI, Cohere all recognized), builds a fallback chain from a hand-maintained catalog of known-good model ids, prefers free models, falls back to a paid one only as a last resort or if named explicitly via `AISWE_MODEL`.
- **Tool surface** (`tools/`, one module per concern): `read_file`, `list_dir`, `search_code`, `find_symbol`, `edit_file`, `write_file`, `run_shell`, `run_tests`, `git_commit`, `update_repo_notes`, `git_push`, `create_pull_request`, `get_issue`, `comment_issue` — 14 tools total. No linter/typecheck tools yet.
- **Reviewer agent** (`agent/reviewer.py`) — every `git_commit` attempt is reviewed by a different model than the one that wrote the change before it reaches the human approval gate; up to 2 feedback rounds per task, verified live (correctly returned APPROVE on a real diff, using a different model than the author).
- **Planning phase** (`_make_plan` in `agent/developer.py`): a cheap call to the fast-tier model chain (`model_router.build_planner_chain()`, independent of the main task's difficulty routing) sketches a short plan before the implementation model starts, giving a real "different model for a different job" split (planner vs. coder vs. reviewer) without a framework migration.
- **Code intelligence** (`find_symbol`, in `docker/helper.py` + `tools/code_search.py`): AST-based (Python stdlib `ast`, not tree-sitter) exact function/class-definition lookup, verified live to correctly locate a symbol in a non-top-level file (`auth/service.py`) that a literal substring grep would have had to guess at. Python-only; multi-language tree-sitter support is still a future extension, not done.
- **Repo memory** (`memory/repo_memory.py`): a `.aiswe/repo-notes.md` file inside the repo, read at the start of every task and updatable by the agent via `update_repo_notes`. Verified live: notes written in one run were loaded and referenced in a subsequent run.
- **GitHub integration** (`tools/github.py`): `git_push`, `create_pull_request`, `get_issue`, `comment_issue` via the `gh` CLI (added to `docker/Dockerfile` via the official apt-repo install method) and a `GITHUB_TOKEN`/`GH_TOKEN` passed into the sandbox via an explicit env-var allowlist (`sandbox/docker.py` never blanket-forwards the host environment). **Not live-tested** against a real GitHub repo/token — no token was available in the environment this was built in. `gh --version` was confirmed working inside the built image; the tool code itself is implemented but less proven than everything else here until run for real once.
- **Evaluation harness** (`evaluation/`): a fixed 3-task set (`tasks.py`) run through the real CLI path against fresh scratch repos, scored by whether tests pass afterward (`evaluator.py`, `python -m aiswe.evaluation.evaluator`). Start small and grow — nothing before this measured whether a prompt/model/routing change helped or hurt.
- **Sandbox** (`sandbox/docker.py`): Docker with the standard `runc` runtime, hardened (capabilities dropped, no new privileges, pids/memory/CPU limits, network off by default). **gVisor is not installed yet** — Phase 0's WSL2/gVisor spike hasn't happened; this is the biggest gap between the plan below and reality.
- **Approval gate** (`approval.py`): diff preview + y/N prompt before any state-changing tool call (now also covering `git_push`, `create_pull_request`, `comment_issue`, `update_repo_notes`), shared by all providers.
- **Reliability**: automatic retry + model-fallback on transient provider errors (observed live: OpenRouter's free Nvidia-hosted model returning intermittent `503`s, other free models hitting shared-pool `429`s) and defensive handling of malformed tool calls from weaker models (observed live: a 2.6B model omitting a required argument, and separately hallucinating a nonexistent `finish` tool) — all confirmed via real failing runs, not hypothetical.
- **Package structure**: split from a flat `src/aiswe/*.py` into `agent/`, `tools/`, `sandbox/`, `memory/`, `evaluation/` subpackages matching each concern (informed by a proposed layout). `api/` and `code_intelligence/` (as a dedicated package, vs. `find_symbol` living in `tools/code_search.py`) still don't exist — no service layer built yet, and the AST approach didn't need its own package.
- **Not done**: the LangGraph/deepagents state-machine migration (see "Open decision" below, deliberately deferred), multi-language code intelligence (Python-only today), and role-based routing beyond planner/coder/reviewer (e.g. a distinct debugger role).

## Architecture decisions (and why)

**Orchestration base: a hand-rolled LiteLLM tool-calling loop, not a packaged agent framework (yet).**
Started on the Claude Agent SDK, which provided the turn/tool-use loop, a permission system, and a sandboxed Bash tool for free — but it's Claude-only, and Claude has no free tier. Rebuilt on LiteLLM instead, which is provider-agnostic (one interface for OpenRouter, Groq, Anthropic, etc.) but doesn't provide an agent loop itself, so the loop, tool-calling, and retry/fallback logic are hand-written in `agent/developer.py`. Design patterns are still borrowed from OpenHands/SWE-agent/Aider — not their runtime code.

**Tool surface: SWE-agent's ACI (Agent-Computer Interface) pattern, not raw bash-everything.**
Instead of one big "run shell command" tool, define a small, constrained set the model calls: `read_file`, `edit_file` (search/replace or unified diff), `search_code`, `run_tests`, `run_shell` (locked down), `git_commit`. SWE-agent's research shows this materially cuts model error rates versus free-form shell access. Single highest-leverage idea from the research.

**Context management: Aider's repo-map technique.**
Tree-sitter-parse the repo into a signature-level map, rank relevance with a PageRank-style graph over file dependencies, and only pull the top-N relevant symbols into context. Pair with OpenHands-style history condensation for long-running tasks.

**Execution architecture: one ephemeral container per task ("Runtime-as-a-server"), per OpenHands.**
Each task spins up a fresh sandboxed container, mounts a repo clone, executes there, then is destroyed. Same shape whether running one task for yourself or hundreds of tasks for many tenants — build this abstraction once.

**Sandboxing: Docker + gVisor (`runsc`) as the core runtime, from day one.**
Since both "free" and "multi-tenant-capable" are required simultaneously (not free-now-rearchitect-later), gVisor is the right starting point rather than plain Docker:
- Free, open-source, no KVM required → works today under WSL2 on Windows.
- Process-level isolation hardened enough for untrusted agent-generated code (Google runs its own agent sandboxes on it).
- The same Docker+gVisor setup moves to a Linux VPS unchanged when going multi-tenant — no rewrite.
- Layer standard hardening on top regardless: rootless Docker, seccomp allowlist, dropped capabilities, read-only rootfs, cgroup CPU/mem/pid quotas, default-deny network egress (allowlist package registries/git remotes only).

Firecracker/Kata are the right *later* step (true VM-level isolation — what E2B/AWS Lambda/fly.io use) but both need `/dev/kvm`, which WSL2 doesn't expose. They become viable once on a real Linux host/VPS. Don't block on them now — gVisor gives genuinely safe multi-tenant isolation without that dependency.

## Phased roadmap

**Phase 0 — Environment spike (few days)**
Set up WSL2 + Docker + `runsc` (gVisor) locally; confirm `docker run --runtime=runsc` works. Stand up a minimal Python project with the Claude Agent SDK; clone OpenHands and SWE-agent locally purely to read their runtime/ACI code (both MIT-licensed, safe to study and adapt).

**Phase 1 — Personal MVP (single tenant, local) — DONE, see "Current status"**
CLI tool: point it at a local repo + task description → agent plans → uses the ACI tool set inside a sandboxed container → runs tests → proposes a diff → human approves → it commits. Full loop end-to-end, single-user. Built with `runc`, not gVisor yet (Phase 0 was skipped in practice — came back to it below).

**Phase 2 — Harden + improve agent quality (partially done)**
Diff-approval gate: done. git-commit-per-edit checkpointing: done (every accepted edit can already be committed immediately). Still open: the repo-map context manager (needed once repos are bigger than fit in one context window), tightening the sandbox (seccomp allowlist, network egress allowlist, resource caps beyond the current memory/CPU/pids limits), gVisor itself, and a SWE-bench-lite-style eval set (see Roadmap v2's evaluation harness below — same idea, more detail).

**Phase 3 — Turn it into a service**
Wrap the agent behind a FastAPI service with a job queue (asyncio queue or Redis/RQ); one sandbox container spun up per job; persist task/conversation state (start with SQLite, move to Postgres). Add basic auth. Personal tool and product start sharing one codebase.

**Phase 4 — Multi-tenant hardening**
Per-tenant Docker networks (no shared volumes/networks between tenants), per-tenant resource quotas, full audit logging of every tool call. Decide whether to move to Kubernetes — if so, Kata Containers is the natural next isolation upgrade over gVisor; otherwise a custom Firecracker orchestration layer once on a KVM-capable Linux host.

**Phase 5 — Iterate on capability**
Add MCP support (à la Goose) if/when the agent needs to reach third-party tools/integrations rather than just filesystem+shell.

## Open decision: framework migration (deepagents / LangGraph)

A proposal (2026-09-25) suggested rebuilding the orchestration layer on LangGraph/deepagents instead of the current hand-rolled LiteLLM loop, citing sub-agent delegation, a cleaner state-machine model, and prior user experience with deepagents. Not done yet, deliberately — this would be the **third** rewrite of the orchestration layer this project has gone through (Claude Agent SDK → hand-rolled OpenAI client → generic LiteLLM loop), and the current one is tested and working. Real tradeoffs to weigh before doing it:
- **For**: built-in state-machine/graph structure (matches the "Explore → Plan → Implement → Test → Review" flow below far more naturally than our flat turn loop); built-in sub-agent spawning (useful for the reviewer-agent idea below); less hand-written retry/loop plumbing.
- **Against**: deepagents/LangGraph expect a LangChain `BaseChatModel`, not a raw LiteLLM call — needs a wrapper (e.g. `langchain-litellm`'s `ChatLiteLLM`) to keep the free-model auto-routing working, adding a dependency layer; our sandboxed tools and approval gate would need re-wiring into LangGraph's tool-call interception rather than our own `can_use_tool`-style check; real migration effort with no guaranteed quality win on today's small tasks.
- **Recommendation**: don't migrate speculatively. Revisit specifically when the reviewer-agent or multi-file-planning work below hits a wall that a flat loop genuinely can't express — that's the concrete trigger, not "frameworks are generally nicer."

## Roadmap v2 (from a 2026-09-25 proposal review) — status

Everything below was implemented and (except where noted) verified with real runs on 2026-09-25, in the priority order originally laid out here:

- **~~Reviewer agent~~ — done.** See "Current status" above.
- **~~Evaluation harness~~ — done.** `evaluation/tasks.py` + `evaluation/evaluator.py`, 3 tasks (grow this list over time — the mechanism, not the task count, was the point of building it now). Run: `python -m aiswe.evaluation.evaluator`.
- **~~Code intelligence upgrade~~ — done, scoped down.** Built as AST-based `find_symbol` (stdlib `ast`, Python-only) rather than tree-sitter/multi-language — verified live finding a symbol in a non-top-level file. True tree-sitter multi-language support and hybrid semantic+symbol search (the original proposal's fuller vision) remain a future extension if/when non-Python repos or much larger codebases are actually in use.
- **~~GitHub issue → branch → PR automation~~ — implemented, not live-tested.** `tools/github.py` (`git_push`, `create_pull_request`, `get_issue`, `comment_issue`) via the `gh` CLI, installed in `docker/Dockerfile`, authenticated via `GITHUB_TOKEN`/`GH_TOKEN` passed through an explicit env allowlist in `sandbox/docker.py`. `gh --version` confirmed working in the built image; the tools themselves need a real GitHub repo + token to verify, which wasn't available while building this — treat as less proven than the rest until run for real once.
- **~~Memory tiers~~ — repo memory done, the other two were already fine as-is.** Short-term (current-task) memory needed nothing new — it's the message history within one `run_task` call. Repo memory: `memory/repo_memory.py` + `.aiswe/repo-notes.md`, read at task start and updatable via the `update_repo_notes` tool, verified live across two separate runs (written in one, loaded in the next). Long-term cross-repo memory remains not built — still correctly the lowest priority per the original note, since it only matters once aiswe is used across many projects regularly.
- **~~Role-based model split~~ — planner/coder/reviewer done.** `model_router.build_planner_chain()` always pulls the fast tier regardless of the main task's difficulty routing, and `agent/developer._make_plan()` uses it for a cheap planning pass before the (possibly stronger/slower) implementation model starts — a genuinely different axis from the existing difficulty-based chain, as this item originally called for. A distinct "debugger" role (a fourth axis, for when tests fail) is not built; today's developer loop just keeps iterating in place.

Not done, and not attempted this round: the LangGraph/deepagents state-machine migration itself (see "Open decision" above — every *capability* that proposal wanted, reviewer/planner/evaluation/GitHub/memory, is now built on top of the existing flat loop instead, without needing the framework swap).

## Research reference — open-source agents studied

### OpenHands (formerly OpenDevin) — github.com/All-Hands-AI/OpenHands
- ReAct-style Action→Observation event stream (CodeAct paradigm); controller manages the loop via LiteLLM.
- Small set of general actions (`CmdRunAction`, `IPythonRunCellAction`, file edit, browse) rather than many narrow tools — model writes code/bash directly.
- Event-stream history + a "Memory" module that condenses/filters history; "microagents" for repo-specific prompt snippets.
- Sandbox: one Docker container per task ("Runtime" server-in-a-box); newer versions add a security gate between actions and execution.
- MIT license, ~74k stars, VC-backed, actively released (v1.7 as of mid-2026). Most actively maintained of the group.
- Reusable: Action/Observation event-stream abstraction, Runtime-as-container-server pattern, microagents.

### SWE-agent — github.com/SWE-agent/SWE-agent (Princeton NLP)
- Simple ReAct loop; real contribution is the ACI: curated, LM-friendly commands (`open`, `goto`, `edit`, `search_file`, `submit`) instead of raw shell access.
- Structured pseudo-terminal commands with strict output formatting/linting feedback baked into the interface.
- Minimal context management — relies on ACI's concise outputs; not built for very long multi-day tasks.
- Sandbox: Docker container per SWE-bench instance, mainly for benchmark reproducibility.
- MIT license, ~19.7k stars, NeurIPS 2024 paper, still maintained but slower cadence than OpenHands.
- Reusable: the ACI concept — constrained, LM-optimized tool surface with built-in error feedback — is the most valuable idea, copied by nearly every later agent.

### Aider — github.com/Aider-AI/aider (Apache 2.0)
- Not autonomous by default — conversational pair-programmer loop; "Architect mode" pairs a planning model with a cheaper editor model.
- Core innovation: the repo map — tree-sitter parses the whole repo into a signature-level map, ranked/pruned with a graph algorithm over file dependency edges.
- Repo map + git-diff-based edit formats + automatic atomic git commits per change (cheap checkpoint/rollback).
- No built-in sandbox; assumes trusted local execution.
- Apache 2.0; maintenance visibly slowed in 2026 (last substantive release Feb 2026, reports of no commits since ~May 2026).
- Reusable: repo-map/context-ranking technique and git-commit-per-edit checkpointing. Not reusable as an autonomous-loop base since it isn't one.

### Other notable entrants
- **Goose** (Block → Linux Foundation's Agentic AI Foundation, `aaif-goose/goose`, Apache 2.0, ~50k+ stars): Rust workspace, MCP-native from the start (70+ extensions, 30+ providers). Good reference for an MCP-first tool architecture. No strong built-in sandboxing.
- **Cline** (VS Code extension, ~67k stars): model-agnostic, plan/act mode split, human-in-the-loop diff approval — good UX reference.
- **Plandex** (AGPL-3.0): built for large multi-file refactors, accumulates a full changeset in a sandbox for review before applying. Worth studying the "batch changeset + review gate" pattern; AGPL needs checking against hosting plans if code is reused directly.
- **AutoCodeRover**: effectively commercialized/absorbed (became a SonarQube remediation product in 2026) — no longer a live independent project.
- **Devika**: ~15k stars, little sign of being a leading reference architecture next to OpenHands/SWE-agent.

## Research reference — sandboxing options compared

### Docker + gVisor (runsc)
Process-level isolation, hardened (Sentry reimplements much of the Linux syscall surface in userspace). Linux only, but works in WSL2 (no KVM needed). Multi-tenancy is DIY on top of standard Docker/K8s multi-tenancy; swap runtime via `--runtime=runsc`. Low-medium setup complexity, ~2.2–2.8x slower on syscall-heavy workloads (fine for compile/test). Used by Google's own infra and emerging "agent sandbox" projects. **Most practical free option for Windows-via-WSL2 → later Linux VPS.**

### Firecracker microVMs
VM-level isolation (KVM-backed), strongest of the free options, separate kernel per sandbox, ~125ms boot. Requires `/dev/kvm` — not available under WSL2 or macOS, only bare-metal Linux or nested-virt cloud instances. Excellent native multi-tenancy (one microVM per tenant/task) — this is the AWS Lambda/E2B/Vercel Sandbox model. High setup complexity (build orchestration yourself). Good later-stage target once off Windows.

### Kata Containers
VM-level isolation like Firecracker but pod/OCI-native — every pod gets its own kernel inside a KVM microVM, drop-in on Kubernetes. Same KVM/Linux requirement. Best fit for multi-tenant Kubernetes specifically. Medium-high setup complexity — heavier than gVisor, lighter than bespoke Firecracker orchestration. 2025-2026 pattern: Kata owns multi-tenant K8s, Firecracker owns serverless/agent-sandbox platforms, gVisor is Google's agent substrate.

### nsjail / bubblewrap / firejail
Process-level only (namespaces + seccomp + cgroups + capability dropping) — no separate kernel, so kernel CVEs remain an attack surface. Works in WSL2. Fully DIY multi-tenancy. Low overhead, fast startup. Good as an *additional inner layer* inside a gVisor container for fast local iteration; not sufficient alone for hosted multi-tenant traffic.

### Docker rootless + seccomp/AppArmor + cgroups
Process-level; rootless reduces blast radius but is not by itself safe for adversarial code. DIY multi-tenancy via per-tenant containers + cgroup limits + locked-down seccomp allowlist + AppArmor/SELinux + read-only rootfs + dropped capabilities. Good v1/personal-tool baseline (no KVM needed), but layer gVisor (or later Kata/Firecracker) on top for a genuine hosted multi-tenant product.

### E2B open-source/self-hosted
SDK/dashboard genuinely open-source; Terraform can deploy the full stack to your own AWS/GCP account for free. Catch: isolation is Firecracker-based under the hood, so self-hosting for real multi-tenant isolation still inherits the KVM/bare-metal requirement — not a way around it. On a Windows laptop, only a degraded/non-Firecracker dev mode is possible. Worth revisiting once on a Linux VPS/bare-metal box.

### Recommended path
1. **Now (Windows + WSL2, personal tool)**: Docker + gVisor (`runsc`) as the core runtime, with seccomp allowlist + cgroups; optionally nsjail/bubblewrap as an inner layer.
2. **Multi-tenant hardening**: per-tenant Docker networks, resource quotas, audit logging — still gVisor, still WSL2/Linux-compatible.
3. **Move to Kata Containers (if Kubernetes) or Firecracker (if custom orchestration)** once hosted on a real Linux VPS/bare-metal/cloud-metal instance and true VM-level isolation is required — this is also when self-hosted E2B becomes viable.

### Claude Agent SDK — reduces orchestration work but isn't a sandbox
Provides the full agent loop (turns, tool-use loop, streaming), a built-in permission system (allow/ask/deny, evaluated in that precedence order, path-scoped `Read()`/`Edit()` rules), and a first-class Bash tool. Ships a sandboxed Bash tool (macOS, Linux, WSL2) using OS-level enforcement to restrict which files/network domains a command and its children can touch. Important distinction: the sandbox restricts filesystem/network reach; permission modes control whether an action is attempted at all — they are not interchangeable, and this is not equivalent to full untrusted-code isolation (no separate kernel/VM boundary). Use the SDK as the orchestration layer, but still run it inside one of the container/VM sandboxes above when executing agent-generated code from untrusted/multi-tenant sources.
