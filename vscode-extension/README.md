# aiswe for VS Code

A chat panel for **aiswe**, a sandboxed AI software engineer. Ask questions
about your code or describe a change; the agent reads files, edits, runs
tests and commits -- inside a locked-down Docker container -- and asks you
before every edit, command or commit, with the change shown in VS Code's diff
view.

## Requirements

1. **Python 3.10+ with aiswe installed:**
   `pip install git+https://github.com/Vinayak4780/AI-SOFTWARE-ENGINNER.git`
2. **Docker Desktop running** (the sandbox image builds on the first message).
3. **An API key** for at least one platform -- enter it in the panel (gear
   button), or put it in `~/.aiswe/.env` / your project's `.env`.

## Use

- Click the **aiswe** icon in the activity bar to open the chat.
- **Models & keys** (gear / key icon): paste a key for OpenRouter, Groq,
  Claude, OpenAI, Gemini, Qwen (DashScope), ModelScope, NVIDIA, DeepSeek,
  Mistral, xAI, Cerebras, Together, Fireworks or DeepInfra, or add a **custom
  OpenAI-compatible endpoint** (Ollama, LM Studio, vLLM...). Keys are stored
  encrypted in VS Code's secret storage.
- **Model picker**: *Auto* (recommended -- picks a good model and falls back
  if one is down) or any model from the platforms you've added. Free models
  are marked. Refresh (&#x21bb;) re-fetches the lists.
- The file you have open (and your selection, if any) is sent with your
  message -- untick "Include ..." to leave it out.
- **Security**: the shield button runs a full security audit (ranked report);
  tick *Security mode* to have any message handled as a security review --
  e.g. "secure the login API". Every commit passes a security gate (secret,
  injection and dependency checks, plus an AI security review for sensitive
  changes); its findings appear in yellow on the approval card.
- Approval cards show each proposed action. Edits open in a diff view
  automatically; **Approve** / **Reject** in the chat.
- **Stop** cancels the running message; **New Chat** (+) forgets the conversation.
- **Show Log** shows the agent process's output, for troubleshooting.

The agent works on the first folder in your workspace.

## Settings

| Setting | Default | |
|---|---|---|
| `aiswe.pythonPath` | `python` | Interpreter with aiswe installed (e.g. a venv's `python.exe`) |
| `aiswe.autoApprove` | `false` | Skip approval prompts |
| `aiswe.network` | `false` | Allow network in the sandbox (for `git push` / PRs) |

## Building from source

```
npm install
npm run compile
npm run package      # -> aiswe-0.1.0.vsix
code --install-extension aiswe-0.1.0.vsix
```

Press F5 in this folder (with the "Run Extension" launch config) to try it in
a development VS Code window.
