// Runs `<python> -m aiswe serve` for one workspace folder and speaks its
// JSON-lines protocol (see src/aiswe/server.py in the Python package).

import { ChildProcess, spawn } from "child_process";
import * as vscode from "vscode";

export type BackendEvent = { type: string; [key: string]: any };

export interface BackendOptions {
  pythonPath: string;
  repo: string;
  autoApprove: boolean;
  network: boolean;
  /** Extra environment for the server: API keys and custom endpoints saved in VS Code. */
  env: Record<string, string>;
}

export class Backend implements vscode.Disposable {
  private proc: ChildProcess | undefined;
  private buffer = "";
  private stopping = false;

  constructor(
    private readonly options: BackendOptions,
    private readonly onEvent: (event: BackendEvent) => void,
    private readonly log: vscode.OutputChannel,
  ) {}

  get running(): boolean {
    return this.proc !== undefined && this.proc.exitCode === null;
  }

  start(): void {
    const o = this.options;
    const args = ["-m", "aiswe", "serve", "--repo", o.repo];
    if (o.autoApprove) args.push("--yes");
    if (o.network) args.push("--network");

    this.log.appendLine(`> ${o.pythonPath} ${args.join(" ")}`);
    // cwd = the workspace, so aiswe picks up the project's own .env first
    // (then ~/.aiswe/.env).
    const proc = spawn(o.pythonPath, args, {
      cwd: o.repo,
      env: { ...process.env, ...o.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
      windowsHide: true,
    });
    this.proc = proc;
    proc.stdout!.setEncoding("utf8");
    proc.stderr!.setEncoding("utf8");
    proc.stdout!.on("data", (chunk: string) => this.onStdout(chunk));
    proc.stderr!.on("data", (chunk: string) => this.log.append(chunk));
    // A message written to a process that failed to start or just died.
    proc.stdin!.on("error", (err) => this.log.appendLine(`[stdin] ${err.message}`));
    proc.on("error", (err) => {
      // Spawn failures may never emit "exit", so mark it gone here.
      if (this.proc === proc) this.proc = undefined;
      this.onEvent({
        type: "error",
        text: `Couldn't start "${o.pythonPath}": ${err.message}. Install aiswe (pip install git+https://github.com/Vinayak4780/AI-SOFTWARE-ENGINNER.git) and point the aiswe.pythonPath setting at that Python.`,
      });
    });
    proc.on("exit", (code) => {
      this.log.appendLine(`[aiswe exited with code ${code}]`);
      if (this.proc === proc) this.proc = undefined;
      if (!this.stopping) this.onEvent({ type: "exit", code });
    });
  }

  send(message: object): void {
    if (!this.running) this.start();
    this.proc!.stdin!.write(JSON.stringify(message) + "\n");
  }

  private onStdout(chunk: string): void {
    this.buffer += chunk;
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (!line) continue;
      try {
        this.onEvent(JSON.parse(line));
      } catch {
        this.log.appendLine(`[non-protocol output] ${line}`);
      }
    }
  }

  /** Ask the server to stop its sandbox and exit; kill it if it doesn't. */
  dispose(): void {
    const proc = this.proc;
    if (!proc) return;
    this.stopping = true;
    try {
      proc.stdin!.write(JSON.stringify({ type: "shutdown" }) + "\n");
      proc.stdin!.end();
    } catch {
      // already gone
    }
    const timer = setTimeout(() => proc.kill(), 15000);
    proc.on("exit", () => clearTimeout(timer));
    this.proc = undefined;
  }
}
