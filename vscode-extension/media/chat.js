// Chat panel UI. Talks to extension.ts via postMessage; extension.ts relays
// to/from the Python `aiswe serve` process.
(function () {
  const vscode = acquireVsCodeApi();
  const messages = document.getElementById("messages");
  const empty = document.getElementById("empty");
  const form = document.getElementById("composer");
  const input = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const stopBtn = document.getElementById("stop");
  const status = document.getElementById("status");
  const includeContext = document.getElementById("include-context");
  const contextLabel = document.getElementById("context-label");
  const contextRow = document.getElementById("context-row");
  const modelSelect = document.getElementById("model");
  const securityMode = document.getElementById("security-mode");
  const settingsPane = document.getElementById("settings");

  let providers = []; // from the server's "models" event
  let selectedModel = "auto";
  let savedKeys = []; // env var names saved in VS Code (never the values)
  let customEndpoints = []; // [{name, baseUrl, hasKey}]
  let backendError = ""; // why the server couldn't list models, if it couldn't

  const toolRows = new Map(); // tool_call id -> <details>
  const openApprovals = new Map(); // approval id -> card element
  let busy = false;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function append(node) {
    empty.hidden = true;
    const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
    messages.appendChild(node);
    if (nearBottom) messages.scrollTop = messages.scrollHeight;
    return node;
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // Small markdown subset: fenced code, inline code, bold, headings, lists.
  function renderMarkdown(text) {
    const parts = text.split(/```/);
    return parts
      .map((part, i) => {
        if (i % 2 === 1) {
          const body = part.replace(/^[\w+-]*\n/, "");
          return `<pre><code>${escapeHtml(body.replace(/\n$/, ""))}</code></pre>`;
        }
        return escapeHtml(part)
          .replace(/`([^`\n]+)`/g, "<code>$1</code>")
          .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
          .replace(/^#{1,6} (.*)$/gm, "<strong>$1</strong>")
          .replace(/^\s*[-*] /gm, "• ")
          .replace(/\n/g, "<br>");
      })
      .join("");
  }

  function renderDiffText(text) {
    const pre = el("pre", "diff");
    for (const line of text.split("\n")) {
      const span = el("span", line.startsWith("+") && !line.startsWith("+++") ? "add"
        : line.startsWith("-") && !line.startsWith("---") ? "del" : "", line + "\n");
      pre.appendChild(span);
    }
    return pre;
  }

  function toolSummary(name, args) {
    const target = args.path || args.command || args.query || args.name || args.message || "";
    return target ? `${name}  ${String(target).split("\n")[0].slice(0, 80)}` : name;
  }

  function addUser(text, label) {
    const node = el("div", "msg user");
    if (label) node.appendChild(el("div", "chip", label));
    node.appendChild(el("div", "body", text));
    append(node);
  }

  function addApproval(event) {
    const card = el("div", "approval");
    card.appendChild(el("div", "approval-title", `Allow ${event.tool}?`));
    if (event.warnings && event.warnings.length) {
      card.classList.add("warned");
      const list = el("ul", "approval-warnings");
      for (const w of event.warnings) list.appendChild(el("li", "", w));
      card.appendChild(list);
    }
    card.appendChild(renderDiffText(event.description));
    const row = el("div", "approval-actions");
    const approve = el("button", "", "Approve");
    const reject = el("button", "secondary", "Reject");
    row.append(approve, reject);
    if (event.hasDiff) {
      const view = el("button", "link", "View diff");
      view.type = "button";
      view.onclick = () => vscode.postMessage({ kind: "viewDiff", id: event.id });
      row.appendChild(view);
    }
    const answer = (approved) => {
      vscode.postMessage({ kind: "approve", id: event.id, approved });
      resolveApproval(event.id, approved ? "Approved" : "Rejected");
    };
    approve.onclick = () => answer(true);
    reject.onclick = () => answer(false);
    card.appendChild(row);
    openApprovals.set(event.id, card);
    append(card);
    approve.focus();
  }

  function resolveApproval(id, outcome) {
    const card = openApprovals.get(id);
    if (!card) return;
    openApprovals.delete(id);
    card.classList.add("resolved");
    card.querySelector(".approval-actions").replaceWith(el("div", "approval-outcome", outcome));
  }

  function handleEvent(event) {
    switch (event.type) {
      case "assistant": {
        const node = el("div", "msg assistant");
        node.innerHTML = renderMarkdown(event.text);
        append(node);
        break;
      }
      case "tool_call": {
        const row = el("details", "tool");
        row.appendChild(el("summary", "", toolSummary(event.name, event.args)));
        toolRows.set(event.id, row);
        append(row);
        break;
      }
      case "tool_result": {
        const row = toolRows.get(event.id);
        if (!row) break;
        const result = String(event.result);
        if (/^(ERROR|DENIED|CANCELLED|REVIEWER)/.test(result)) row.classList.add("flagged");
        row.appendChild(el("pre", "", result.length > 6000 ? result.slice(0, 6000) + "\n…[truncated]" : result));
        break;
      }
      case "approval_request":
        addApproval(event);
        break;
      case "log":
        append(el("div", "log", event.text.trim()));
        break;
      case "error":
        append(el("div", "error", event.text));
        if (!providers.length && !backendError) { // keep the first, most specific reason
          backendError = event.text;
          modelSelect.disabled = false;
          if (!settingsPane.hidden) renderSettings();
        }
        break;
      case "done":
        if (event.usage) {
          append(el("div", "log", `done · ${event.usage.prompt_tokens} in / ${event.usage.completion_tokens} out tokens`));
        }
        break;
      case "turn_end":
        for (const id of [...openApprovals.keys()]) resolveApproval(id, "Cancelled");
        break;
      case "reset_done":
        break;
      case "models":
        providers = event.providers;
        backendError = "";
        modelSelect.disabled = false;
        renderModelSelect();
        if (!settingsPane.hidden) renderSettings();
        break;
    }
  }

  function setBusy(value) {
    busy = value;
    sendBtn.disabled = value;
    stopBtn.hidden = !value;
    status.textContent = value ? "Working…" : "";
  }

  function clear() {
    messages.querySelectorAll(":scope > :not(#empty)").forEach((n) => n.remove());
    toolRows.clear();
    openApprovals.clear();
    empty.hidden = false;
  }

  function submit() {
    const text = input.value.trim();
    if (!text || busy) return;
    vscode.postMessage({
      kind: "send", text, includeContext: includeContext.checked, model: selectedModel,
      mode: securityMode.checked ? "security" : "default",
    });
    input.value = "";
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
  });
  stopBtn.addEventListener("click", () => vscode.postMessage({ kind: "cancel" }));

  // --- model picker -----------------------------------------------------

  function renderModelSelect() {
    modelSelect.textContent = "";
    modelSelect.appendChild(new Option("Auto (best available, with fallback)", "auto"));
    let found = selectedModel === "auto";
    const configured = providers.filter((p) => p.configured);
    for (const p of configured) {
      const group = document.createElement("optgroup");
      group.label = p.error ? `${p.label} -- ${p.error}` : `${p.label} (${p.models.length})`;
      for (const m of p.models) {
        const label = m.name && m.name !== m.id.slice(p.id.length + 1) ? `${m.name}` : m.id.slice(p.id.length + 1);
        group.appendChild(new Option(label + (m.free ? "  · free" : ""), m.id));
        if (m.id === selectedModel) found = true;
      }
      if (!p.models.length) {
        const none = new Option(p.error ? "couldn't load models" : "no models", "");
        none.disabled = true;
        group.appendChild(none);
      }
      modelSelect.appendChild(group);
    }
    if (!found) modelSelect.appendChild(new Option(`${selectedModel} (saved)`, selectedModel));
    if (!configured.length) {
      const hint = new Option("No API keys yet -- click the gear to add one", "");
      hint.disabled = true;
      modelSelect.appendChild(hint);
    }
    modelSelect.value = selectedModel;
  }

  modelSelect.addEventListener("change", () => {
    if (!modelSelect.value) return;
    selectedModel = modelSelect.value;
    vscode.postMessage({ kind: "selectModel", model: selectedModel });
  });
  document.getElementById("refresh-models").addEventListener("click", () => {
    modelSelect.disabled = true;
    vscode.postMessage({ kind: "listModels", refresh: true });
  });
  document.getElementById("open-settings").addEventListener("click", () => toggleSettings(true));

  // --- models & keys screen --------------------------------------------

  // Every platform aiswe supports -- mirrors PROVIDERS in src/aiswe/providers.py,
  // so keys can be added even before (or without) the Python backend running.
  const PLATFORMS = [
    { id: "openrouter", label: "OpenRouter", keyEnv: "OPENROUTER_API_KEY", keyUrl: "https://openrouter.ai/settings/keys", free: true },
    { id: "groq", label: "Groq", keyEnv: "GROQ_API_KEY", keyUrl: "https://console.groq.com/keys", free: true },
    { id: "anthropic", label: "Claude (Anthropic)", keyEnv: "ANTHROPIC_API_KEY", keyUrl: "https://console.anthropic.com/settings/keys" },
    { id: "openai", label: "OpenAI", keyEnv: "OPENAI_API_KEY", keyUrl: "https://platform.openai.com/api-keys" },
    { id: "gemini", label: "Google Gemini", keyEnv: "GEMINI_API_KEY", keyUrl: "https://aistudio.google.com/apikey", free: true },
    { id: "dashscope", label: "Qwen (Alibaba DashScope)", keyEnv: "DASHSCOPE_API_KEY", keyUrl: "https://modelstudio.console.alibabacloud.com/" },
    { id: "modelscope", label: "ModelScope", keyEnv: "MODELSCOPE_API_KEY", keyUrl: "https://modelscope.cn/my/myaccesstoken", free: true },
    { id: "nvidia", label: "NVIDIA NIM", keyEnv: "NVIDIA_API_KEY", keyUrl: "https://build.nvidia.com/settings/api-keys", free: true },
    { id: "deepseek", label: "DeepSeek", keyEnv: "DEEPSEEK_API_KEY", keyUrl: "https://platform.deepseek.com/api_keys" },
    { id: "mistral", label: "Mistral", keyEnv: "MISTRAL_API_KEY", keyUrl: "https://console.mistral.ai/api-keys" },
    { id: "xai", label: "xAI (Grok)", keyEnv: "XAI_API_KEY", keyUrl: "https://console.x.ai/" },
    { id: "cerebras", label: "Cerebras", keyEnv: "CEREBRAS_API_KEY", keyUrl: "https://cloud.cerebras.ai/", free: true },
    { id: "together_ai", label: "Together AI", keyEnv: "TOGETHERAI_API_KEY", keyUrl: "https://api.together.ai/settings/api-keys" },
    { id: "fireworks_ai", label: "Fireworks AI", keyEnv: "FIREWORKS_AI_API_KEY", keyUrl: "https://fireworks.ai/account/api-keys" },
    { id: "deepinfra", label: "DeepInfra", keyEnv: "DEEPINFRA_API_KEY", keyUrl: "https://deepinfra.com/dash/api_keys" },
  ];
  const CUSTOM = "__custom__";
  let addPlatform = PLATFORMS[0].id; // the platform picked in the add form, kept across re-renders

  /** Built-in platforms: the static list plus any newer ones the server reports. */
  function platforms() {
    const known = new Set(PLATFORMS.map((p) => p.id));
    return PLATFORMS.concat(providers.filter((p) => !p.custom && !known.has(p.id)));
  }

  function toggleSettings(show) {
    settingsPane.hidden = !show;
    messages.hidden = show;
    if (show) renderSettings();
  }

  /** Saved custom endpoints in the shape saveSettings expects (null key = keep the saved one). */
  function keptEndpoints() {
    return customEndpoints.map((c) => ({ name: c.name, baseUrl: c.baseUrl, apiKey: null }));
  }

  function save(keys, endpoints) {
    vscode.postMessage({ kind: "saveSettings", keys, customEndpoints: endpoints });
  }

  function textInput(placeholder, type) {
    const node = el("input");
    node.placeholder = placeholder;
    if (type) node.type = type;
    return node;
  }

  function labelled(text, control) {
    const wrap = el("label", "field");
    wrap.appendChild(el("span", "field-label", text));
    wrap.appendChild(control);
    return wrap;
  }

  function removeButton(onRemove) {
    const remove = el("button", "secondary", "Remove");
    remove.type = "button";
    remove.onclick = () => {
      remove.disabled = true;
      remove.textContent = "Removing…";
      onRemove();
    };
    return remove;
  }

  function renderAddForm() {
    const card = el("div", "add-card");
    card.appendChild(el("h4", "", "Add an API key"));

    const pick = el("select", "platform-select");
    for (const p of platforms()) pick.appendChild(new Option(p.label + (p.free ? "  · free tier" : ""), p.id));
    pick.appendChild(new Option("Custom endpoint (Ollama, LM Studio, any OpenAI-compatible API)", CUSTOM));
    pick.value = addPlatform;
    if (!pick.value) pick.value = PLATFORMS[0].id;
    card.appendChild(labelled("Platform", pick));

    const fields = el("div", "add-fields");
    card.appendChild(fields);
    const problem = el("div", "provider-warning");
    const submit = el("button", "", "Save key");
    submit.type = "button";

    // Returns {keys, endpoints} for the picked platform, or a string saying what's missing.
    let read = () => "";

    function draw() {
      addPlatform = pick.value;
      fields.textContent = "";
      problem.textContent = "";
      if (pick.value === CUSTOM) {
        const name = textInput("e.g. ollama");
        const url = textInput("e.g. http://localhost:11434/v1");
        const key = textInput("optional", "password");
        fields.append(labelled("Name", name), labelled("Base URL", url), labelled("API key", key));
        read = () => {
          if (!name.value.trim() || !url.value.trim()) return "Enter a name and a base URL.";
          const others = keptEndpoints().filter((c) => c.name !== name.value.trim());
          return { keys: {}, endpoints: others.concat({ name: name.value, baseUrl: url.value, apiKey: key.value.trim() }) };
        };
        return;
      }
      const p = platforms().find((x) => x.id === pick.value);
      const key = textInput(savedKeys.includes(p.keyEnv) ? "key saved -- paste a new one to replace it" : `Paste your ${p.label} API key`, "password");
      key.addEventListener("keydown", (e) => {
        if (e.key === "Enter") submit.click();
      });
      const line = el("div", "provider-line");
      line.appendChild(key);
      if (p.keyUrl) {
        const link = el("button", "link", "Get a key ↗");
        link.type = "button";
        link.onclick = () => vscode.postMessage({ kind: "openUrl", url: p.keyUrl });
        line.appendChild(link);
      }
      fields.appendChild(labelled("API key", line));
      read = () => (key.value.trim() ? { keys: { [p.keyEnv]: key.value.trim() }, endpoints: keptEndpoints() } : "Paste an API key first.");
    }

    pick.addEventListener("change", draw);
    draw();
    submit.onclick = () => {
      const payload = read();
      if (typeof payload === "string") {
        problem.textContent = payload;
        return;
      }
      submit.disabled = true;
      submit.textContent = "Saving…";
      save(payload.keys, payload.endpoints);
    };

    const actions = el("div", "settings-actions");
    actions.append(problem, submit);
    card.appendChild(actions);
    return card;
  }

  function platformRow(label, info, detail, onRemove) {
    const row = el("div", "provider-row");
    const top = el("div", "provider-top");
    top.appendChild(el("span", "provider-name", label));
    let status = "saved";
    let cls = "";
    if (info && info.error) [status, cls] = [info.error, " bad"];
    else if (info && info.configured) [status, cls] = [`${info.models.length} models`, " ok"];
    top.appendChild(el("span", "provider-status" + cls, status));
    row.appendChild(top);
    if (info && info.warning) row.appendChild(el("div", "provider-warning", info.warning));
    const line = el("div", "provider-line");
    line.appendChild(el("span", "hint grow", detail));
    if (onRemove) line.appendChild(removeButton(onRemove));
    row.appendChild(line);
    return row;
  }

  function renderConnected() {
    const box = el("div");
    box.appendChild(el("h4", "", "Your platforms"));
    const list = el("div", "provider-list");
    box.appendChild(list);

    for (const p of platforms()) {
      const info = providers.find((x) => x.id === p.id);
      const saved = savedKeys.includes(p.keyEnv);
      if (!saved && !(info && info.configured)) continue;
      list.appendChild(platformRow(p.label, info,
        saved ? `${p.keyEnv} · stored in VS Code` : `${p.keyEnv} · from .env`,
        saved ? () => save({ [p.keyEnv]: "" }, keptEndpoints()) : null));
    }
    for (const c of customEndpoints) {
      const info = providers.find((x) => x.custom && x.label === `${c.name} (custom)`);
      list.appendChild(platformRow(`${c.name} (custom)`, info, c.baseUrl,
        () => save({}, keptEndpoints().filter((x) => x.name !== c.name))));
    }
    if (!list.children.length) list.appendChild(el("p", "hint", "No keys yet -- pick a platform above and paste its key."));
    return box;
  }

  function renderSettings() {
    settingsPane.textContent = "";
    const head = el("div", "settings-head");
    head.appendChild(el("h3", "", "Models & API keys"));
    const close = el("button", "link", "Close");
    close.type = "button";
    close.onclick = () => toggleSettings(false);
    head.appendChild(close);
    settingsPane.appendChild(head);
    settingsPane.appendChild(el("p", "hint",
      "Pick a platform, paste its API key and save -- all its models then show up in the model picker. Keys are stored encrypted by VS Code. Keys in ~/.aiswe/.env or the project's .env work too."));
    if (backendError && !providers.length) {
      settingsPane.appendChild(el("div", "backend-error",
        "The aiswe agent isn't running, so models can't be listed yet. Keys you save are kept and used once it starts.\n\n" + backendError));
    }
    settingsPane.appendChild(renderAddForm());
    settingsPane.appendChild(renderConnected());
  }

  window.addEventListener("message", ({ data }) => {
    switch (data.kind) {
      case "event":
        handleEvent(data.event);
        break;
      case "user":
        addUser(data.text, data.contextLabel);
        break;
      case "busy":
        setBusy(data.busy);
        break;
      case "context":
        contextRow.hidden = !data.label;
        contextLabel.textContent = data.label ? `Include ${data.label}` : "";
        break;
      case "clear":
        clear();
        break;
      case "selectedModel":
        selectedModel = data.model || "auto";
        renderModelSelect();
        break;
      case "settings":
        savedKeys = data.savedKeys;
        customEndpoints = data.customEndpoints;
        if (!settingsPane.hidden) renderSettings();
        break;
      case "showSettings":
        toggleSettings(true);
        break;
    }
  });

  vscode.postMessage({ kind: "ready" });
  input.focus();
})();
