// Shows a proposed edit_file / write_file in VS Code's diff editor before the
// user approves it. The repo is mounted into the sandbox at /workspace, so
// the agent's paths map straight onto the workspace folder on disk.

import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

export const PROPOSED_SCHEME = "aiswe-proposed";

export class ProposedContent implements vscode.TextDocumentContentProvider {
  private readonly docs = new Map<string, string>();
  private counter = 0;

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.docs.get(uri.toString()) ?? "";
  }

  add(relPath: string, content: string): vscode.Uri {
    // Unique query per proposal so VS Code never shows a stale cached doc.
    const uri = vscode.Uri.from({ scheme: PROPOSED_SCHEME, path: "/" + relPath, query: String(++this.counter) });
    this.docs.set(uri.toString(), content);
    return uri;
  }
}

/** Agent path ("/workspace/a.py", "./a.py", "a.py") -> path relative to the repo. */
export function toRelative(agentPath: string): string {
  let p = agentPath.replace(/\\/g, "/");
  if (p === "/workspace") return "";
  if (p.startsWith("/workspace/")) p = p.slice("/workspace/".length);
  return p.replace(/^\.\//, "").replace(/^\/+/, "");
}

export function hasDiff(tool: string): boolean {
  return tool === "edit_file" || tool === "write_file";
}

export async function showProposedDiff(
  provider: ProposedContent,
  repo: string,
  tool: string,
  args: { path?: string; old_string?: string; new_string?: string; content?: string },
): Promise<void> {
  const rel = toRelative(args.path ?? "");
  const abs = path.resolve(repo, rel);
  const inside = path.relative(repo, abs);
  if (inside.startsWith("..") || path.isAbsolute(inside)) {
    vscode.window.showWarningMessage(`aiswe: refusing to preview ${args.path} -- it points outside the workspace.`);
    return;
  }
  const exists = fs.existsSync(abs);
  const current = exists ? fs.readFileSync(abs, "utf8") : "";

  let proposed: string;
  if (tool === "write_file") {
    proposed = args.content ?? "";
  } else {
    const oldString = args.old_string ?? "";
    const index = current.indexOf(oldString);
    if (!oldString || index < 0) {
      vscode.window.showWarningMessage(
        `aiswe: the text this edit replaces wasn't found in ${rel} -- the edit will fail if approved.`,
      );
      return;
    }
    proposed = current.slice(0, index) + (args.new_string ?? "") + current.slice(index + oldString.length);
  }

  const left = exists ? vscode.Uri.file(abs) : provider.add(rel + " (new file)", "");
  const right = provider.add(rel, proposed);
  await vscode.commands.executeCommand("vscode.diff", left, right, `${rel} (proposed by aiswe)`, { preview: true });
}
