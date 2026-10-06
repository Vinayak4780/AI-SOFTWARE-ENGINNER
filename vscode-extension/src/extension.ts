import * as crypto from "crypto";
import * as path from "path";
import * as vscode from "vscode";
import { Backend, BackendEvent } from "./backend";
import { PROPOSED_SCHEME, ProposedContent, hasDiff, showProposedDiff } from "./diff";

const MAX_SELECTION_CHARS = 20000;
// SecretStorage entries (encrypted by VS Code / the OS keychain).
const KEYS_SECRET = "aiswe.apiKeys"; // JSON {ENV_VAR_NAME: key}
const CUSTOM_SECRET = "aiswe.customEndpoints"; // JSON [{name, base_url, api_key}]
const MODEL_STATE = "aiswe.selectedModel";

interface CustomEndpoint {
  name: string;
  base_url: string;
  api_key: string;
}

interface EditorContext {
  file: string;
  language: string;
  selection?: string;
  startLine?: number;
  endLine?: number;
}

class ChatViewProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;
  private backend: Backend | undefined;
  private busy = false;
  private readonly approvals = new Map<number, { tool: string; args: any }>();
  private starting: Promise<Backend | undefined> | undefined;
  private readonly disposables: vscode.Disposable[] = [];

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly log: vscode.OutputChannel,
    private readonly proposed: ProposedContent,
  ) {
    this.disposables.push(
      vscode.window.onDidChangeActiveTextEditor(() => this.postContext()),
      vscode.window.onDidChangeTextEditorSelection(() => this.postContext()),
      vscode.workspace.onDidChangeConfiguration((e) => {
        // New settings apply from the next message (a fresh server process).
        if (e.affectsConfiguration("aiswe") && !this.busy) this.stopBackend();
      }),
    );
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    view.webview.onDidReceiveMessage((m) => this.onWebviewMessage(m), undefined, this.disposables);
  }

  private get repo(): string | undefined {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  }

  private post(message: object): void {
    this.view?.webview.postMessage(message);
  }

  private setBusy(busy: boolean): void {
    this.busy = busy;
    this.post({ kind: "busy", busy });
  }

  private editorContext(): EditorContext | undefined {
    const editor = vscode.window.activeTextEditor;
    const repo = this.repo;
    if (!editor || !repo || editor.document.uri.scheme !== "file") return undefined;
    const rel = path.relative(repo, editor.document.uri.fsPath);
    if (rel.startsWith("..") || path.isAbsolute(rel)) return undefined;
    const ctx: EditorContext = { file: rel.replace(/\\/g, "/"), language: editor.document.languageId };
    const sel = editor.selection;
    if (!sel.isEmpty) {
      ctx.selection = editor.document.getText(sel).slice(0, MAX_SELECTION_CHARS);
      ctx.startLine = sel.start.line + 1;
      ctx.endLine = sel.end.line + 1;
    }
    return ctx;
  }

  private contextLabel(ctx: EditorContext | undefined): string | null {
    if (!ctx) return null;
    return ctx.startLine ? `${ctx.file}:${ctx.startLine}-${ctx.endLine}` : ctx.file;
  }

  private postContext(): void {
    this.post({ kind: "context", label: this.contextLabel(this.editorContext()) });
  }

  private async readJsonSecret<T>(key: string, fallback: T): Promise<T> {
    try {
      const raw = await this.context.secrets.get(key);
      return raw ? (JSON.parse(raw) as T) : fallback;
    } catch {
      return fallback;
    }
  }

  private async backendEnv(): Promise<Record<string, string>> {
    const env: Record<string, string> = { ...(await this.readJsonSecret<Record<string, string>>(KEYS_SECRET, {})) };
    const custom = await this.readJsonSecret<CustomEndpoint[]>(CUSTOM_SECRET, []);
    if (custom.length) env.AISWE_CUSTOM_ENDPOINTS = JSON.stringify(custom);
    return env;
  }

  /** Saved-key names and custom endpoints (never the key values) for the settings screen. */
  private async postSettings(): Promise<void> {
    const keys = await this.readJsonSecret<Record<string, string>>(KEYS_SECRET, {});
    const custom = await this.readJsonSecret<CustomEndpoint[]>(CUSTOM_SECRET, []);
    this.post({
      kind: "settings",
      savedKeys: Object.keys(keys),
      customEndpoints: custom.map((c) => ({ name: c.name, baseUrl: c.base_url, hasKey: !!c.api_key })),
    });
  }

  private ensureBackend(): Promise<Backend | undefined> {
    if (this.backend) return Promise.resolve(this.backend);
    if (!this.starting) {
      this.starting = this.createBackend().finally(() => (this.starting = undefined));
    }
    return this.starting;
  }

  private async createBackend(): Promise<Backend | undefined> {
    const repo = this.repo;
    if (!repo) {
      this.post({ kind: "event", event: { type: "error", text: "Open a folder first -- aiswe works on the first workspace folder." } });
      return undefined;
    }
    const cfg = vscode.workspace.getConfiguration("aiswe");
    const backend = new Backend(
      {
        pythonPath: cfg.get<string>("pythonPath") || "python",
        repo,
        autoApprove: cfg.get<boolean>("autoApprove") ?? false,
        network: cfg.get<boolean>("network") ?? false,
        env: await this.backendEnv(),
      },
      (event) => this.onBackendEvent(event),
      this.log,
    );
    backend.start();
    this.backend = backend;
    return backend;
  }

  private async listModels(refresh: boolean): Promise<void> {
    const backend = await this.ensureBackend();
    backend?.send({ type: "list_models", refresh });
  }

  private async saveSettings(m: {
    keys: Record<string, string>;
    customEndpoints: { name: string; baseUrl: string; apiKey: string | null }[];
  }): Promise<void> {
    if (this.busy) {
      vscode.window.showWarningMessage("aiswe is still working -- save your keys after it finishes (or press Stop).");
      return;
    }
    const keys = await this.readJsonSecret<Record<string, string>>(KEYS_SECRET, {});
    for (const [env, value] of Object.entries(m.keys ?? {})) {
      if (value) keys[env] = value.trim();
      else delete keys[env];
    }
    await this.context.secrets.store(KEYS_SECRET, JSON.stringify(keys));

    const old = await this.readJsonSecret<CustomEndpoint[]>(CUSTOM_SECRET, []);
    const custom: CustomEndpoint[] = (m.customEndpoints ?? [])
      .filter((c) => c.name.trim() && c.baseUrl.trim())
      .map((c) => ({
        name: c.name.trim(),
        base_url: c.baseUrl.trim(),
        // null = "keep the key already saved for this endpoint"
        api_key: c.apiKey === null ? old.find((o) => o.name === c.name.trim())?.api_key ?? "" : c.apiKey.trim(),
      }));
    await this.context.secrets.store(CUSTOM_SECRET, JSON.stringify(custom));

    // Keys reach the server as environment variables, so restart it.
    this.stopBackend();
    await this.postSettings();
    await this.listModels(true);
    vscode.window.setStatusBarMessage("aiswe: keys saved", 3000);
  }

  private stopBackend(): void {
    this.backend?.dispose();
    this.backend = undefined;
    this.approvals.clear();
  }

  private onBackendEvent(event: BackendEvent): void {
    switch (event.type) {
      case "approval_request":
        this.approvals.set(event.id, { tool: event.tool, args: event.args });
        event.hasDiff = hasDiff(event.tool);
        if (event.hasDiff && this.repo) {
          showProposedDiff(this.proposed, this.repo, event.tool, event.args).catch((e) => this.log.appendLine(String(e)));
        }
        break;
      case "turn_end":
        this.setBusy(false);
        break;
      case "exit":
        this.backend = undefined;
        this.approvals.clear();
        if (this.busy) this.setBusy(false);
        this.post({ kind: "event", event: { type: "error", text: `The agent process stopped (exit code ${event.code}). See "aiswe: Show Log".` } });
        return;
      case "error":
        if (this.busy && !this.backend?.running) this.setBusy(false);
        break;
    }
    this.post({ kind: "event", event });
  }

  private onWebviewMessage(m: any): void {
    switch (m.kind) {
      case "ready":
        this.postContext();
        this.post({ kind: "busy", busy: this.busy });
        this.post({ kind: "selectedModel", model: this.context.globalState.get<string>(MODEL_STATE, "auto") });
        this.postSettings();
        this.listModels(false);
        break;
      case "send":
        this.sendMessage(m);
        break;
      case "selectModel":
        this.context.globalState.update(MODEL_STATE, String(m.model || "auto"));
        break;
      case "listModels":
        this.listModels(!!m.refresh);
        break;
      case "saveSettings":
        this.saveSettings(m);
        break;
      case "openUrl":
        if (/^https:\/\//.test(String(m.url))) vscode.env.openExternal(vscode.Uri.parse(m.url));
        break;
      case "approve":
        if (this.approvals.delete(m.id)) this.backend?.send({ type: "approval", id: m.id, approved: !!m.approved });
        break;
      case "viewDiff": {
        const a = this.approvals.get(m.id);
        if (a && this.repo) showProposedDiff(this.proposed, this.repo, a.tool, a.args);
        break;
      }
      case "cancel":
        this.cancel();
        break;
      case "newChat":
        this.newChat();
        break;
    }
  }

  private async sendMessage(m: any): Promise<void> {
    const text = String(m.text ?? "").trim();
    if (!text || this.busy) return;
    this.setBusy(true); // before the await, so a double Enter can't send twice
    const backend = await this.ensureBackend();
    if (!backend) {
      this.setBusy(false);
      return;
    }
    const ctx = m.includeContext ? this.editorContext() : undefined;
    this.post({ kind: "user", text, contextLabel: this.contextLabel(ctx) });
    const model = String(m.model || this.context.globalState.get<string>(MODEL_STATE, "auto"));
    const mode = m.mode === "security" ? "security" : "default";
    backend.send({ type: "message", text, context: ctx, model, mode });
  }

  securityAudit(): void {
    if (this.busy) {
      vscode.window.showInformationMessage("aiswe is still working -- stop it first.");
      return;
    }
    vscode.commands.executeCommand("aiswe.chat.focus");
    this.sendMessage({ text: "Run a full security audit of this repository and give me a ranked report. Don't edit files.", mode: "security" });
  }

  showSettings(): void {
    this.post({ kind: "showSettings" });
  }

  cancel(): void {
    if (this.busy) this.backend?.send({ type: "cancel" });
  }

  newChat(): void {
    if (this.busy) {
      vscode.window.showInformationMessage("aiswe is still working -- stop it first.");
      return;
    }
    if (this.backend?.running) this.backend.send({ type: "reset" });
    this.approvals.clear();
    this.post({ kind: "clear" });
  }

  restart(): void {
    this.stopBackend();
    this.setBusy(false);
    this.post({ kind: "clear" });
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const nonce = crypto.randomBytes(16).toString("base64");
    const script = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(media, "chat.css"));
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link href="${style}" rel="stylesheet">
<title>aiswe</title>
</head>
<body>
<div id="messages">
  <div id="empty">
    <h2>aiswe</h2>
    <p>Ask about your code or describe a change. The agent works in a sandbox and asks before it edits, runs commands or commits.</p>
  </div>
</div>
<div id="settings" hidden></div>
<form id="composer">
  <div id="model-row">
    <select id="model" title="Model"><option value="auto">Auto (best available, with fallback)</option></select>
    <button type="button" id="refresh-models" class="icon" title="Refresh model list">&#x21bb;</button>
    <button type="button" id="open-settings" class="icon" title="Models &amp; API keys">&#x2699;</button>
  </div>
  <div id="options-row">
    <label id="context-row"><input type="checkbox" id="include-context" checked> <span id="context-label"></span></label>
    <label title="Run messages as a security review/audit: scanners + an AI security engineer"><input type="checkbox" id="security-mode"> Security mode</label>
  </div>
  <textarea id="input" rows="3" placeholder="Ask or describe a change… (Enter to send, Shift+Enter for a new line)"></textarea>
  <div id="actions">
    <span id="status"></span>
    <button type="button" id="stop" class="secondary" hidden>Stop</button>
    <button type="submit" id="send">Send</button>
  </div>
</form>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
  }

  dispose(): void {
    this.stopBackend();
    this.disposables.forEach((d) => d.dispose());
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const log = vscode.window.createOutputChannel("aiswe");
  const proposed = new ProposedContent();
  const provider = new ChatViewProvider(context, log, proposed);
  context.subscriptions.push(
    log,
    provider,
    vscode.workspace.registerTextDocumentContentProvider(PROPOSED_SCHEME, proposed),
    vscode.window.registerWebviewViewProvider("aiswe.chat", provider, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.commands.registerCommand("aiswe.newChat", () => provider.newChat()),
    vscode.commands.registerCommand("aiswe.manageKeys", () => provider.showSettings()),
    vscode.commands.registerCommand("aiswe.securityAudit", () => provider.securityAudit()),
    vscode.commands.registerCommand("aiswe.cancel", () => provider.cancel()),
    vscode.commands.registerCommand("aiswe.restart", () => provider.restart()),
    vscode.commands.registerCommand("aiswe.showLog", () => log.show()),
  );
}

export function deactivate(): void {}
