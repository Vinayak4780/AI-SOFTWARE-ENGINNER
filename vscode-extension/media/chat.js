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
  const settingsPane = document.getElementById("settings");

  let providers = []; // from the server's "models" event
  let selectedModel = "auto";
  let savedKeys = []; // env var names saved in VS Code (never the values)
  let customEndpoints = []; // [{name, baseUrl, hasKey}]

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
    vscode.postMessage({ kind: "send", text, includeContext: includeContext.checked, model: selectedModel });
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

  function toggleSettings(show) {
    settingsPane.hidden = !show;
    messages.hidden = show;
    if (show) renderSettings();
  }

  function customRow(entry) {
    const row = el("div", "custom-row");
    const name = el("input");
    name.placeholder = "Name (e.g. ollama)";
    name.value = entry.name || "";
    name.className = "c-name";
    const url = el("input");
    url.placeholder = "Base URL (e.g. http://localhost:11434/v1)";
    url.value = entry.baseUrl || "";
    url.className = "c-url";
    const key = el("input");
    key.type = "password";
    key.placeholder = entry.hasKey ? "key saved -- type to replace" : "API key (optional)";
    key.className = "c-key";
    key.dataset.hasKey = entry.hasKey ? "1" : "";
    const remove = el("button", "secondary", "Remove");
    remove.type = "button";
    remove.onclick = () => row.remove();
    row.append(name, url, key, remove);
    return row;
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
      "Add a key for any platform and all its models show up in the model picker. Keys are stored encrypted by VS Code. Keys in ~/.aiswe/.env or the project's .env work too."));

    const list = el("div", "provider-list");
    const builtIn = providers.filter((p) => !p.custom);
    if (!builtIn.length) list.appendChild(el("p", "hint", "Loading providers…"));
    for (const p of builtIn) {
      const row = el("div", "provider-row");
      const top = el("div", "provider-top");
      top.appendChild(el("span", "provider-name", p.label));
      const saved = savedKeys.includes(p.keyEnv);
      let status = "not set";
      if (p.configured) status = p.error ? "key set -- " + p.error : `${p.models.length} models`;
      top.appendChild(el("span", "provider-status" + (p.configured && !p.error ? " ok" : p.error ? " bad" : ""), status));
      row.appendChild(top);
      const line = el("div", "provider-line");
      const input = el("input");
      input.type = "password";
      input.dataset.env = p.keyEnv;
      input.placeholder = saved ? "saved -- type to replace" : p.configured ? `set in .env (${p.keyEnv})` : p.keyEnv;
      line.appendChild(input);
      if (saved) {
        const remove = el("button", "secondary", "Remove");
        remove.type = "button";
        remove.onclick = () => {
          input.value = "";
          input.dataset.remove = "1";
          input.placeholder = "will be removed on save";
        };
        line.appendChild(remove);
      }
      if (p.keyUrl) {
        const link = el("button", "link", "Get key");
        link.type = "button";
        link.onclick = () => vscode.postMessage({ kind: "openUrl", url: p.keyUrl });
        line.appendChild(link);
      }
      row.appendChild(line);
      list.appendChild(row);
    }
    settingsPane.appendChild(list);

    settingsPane.appendChild(el("h4", "", "Custom endpoints (OpenAI-compatible)"));
    settingsPane.appendChild(el("p", "hint", "Ollama, LM Studio, vLLM, a company gateway… anything with an OpenAI-style /v1 API."));
    const customs = el("div", "custom-list");
    customEndpoints.forEach((c) => customs.appendChild(customRow(c)));
    settingsPane.appendChild(customs);
    const add = el("button", "secondary", "+ Add endpoint");
    add.type = "button";
    add.onclick = () => customs.appendChild(customRow({}));
    settingsPane.appendChild(add);

    const actions = el("div", "settings-actions");
    const save = el("button", "", "Save");
    save.type = "button";
    save.onclick = () => {
      const keys = {};
      settingsPane.querySelectorAll("input[data-env]").forEach((input) => {
        if (input.value.trim()) keys[input.dataset.env] = input.value.trim();
        else if (input.dataset.remove) keys[input.dataset.env] = "";
      });
      const endpoints = [...customs.querySelectorAll(".custom-row")].map((row) => {
        const key = row.querySelector(".c-key");
        return {
          name: row.querySelector(".c-name").value,
          baseUrl: row.querySelector(".c-url").value,
          apiKey: key.value.trim() ? key.value : key.dataset.hasKey ? null : "",
        };
      });
      vscode.postMessage({ kind: "saveSettings", keys, customEndpoints: endpoints });
      settingsPane.querySelectorAll(".provider-status").forEach((s) => (s.textContent = "checking…"));
    };
    actions.appendChild(save);
    settingsPane.appendChild(actions);
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
