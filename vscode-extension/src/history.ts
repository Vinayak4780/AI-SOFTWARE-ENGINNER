// Chat history: one JSON file per chat in VS Code's storage for this
// workspace (so each project has its own history), plus a small index.
//
// A chat keeps two things:
//   transcript -- what the panel showed (user messages and backend events),
//                 replayed to redraw the chat when it's reopened
//   messages   -- the model conversation from the server's "history" event,
//                 sent back with "restore" so the agent remembers it

import * as crypto from "crypto";
import { promises as fs } from "fs";
import * as path from "path";

export type TranscriptItem =
  | { kind: "user"; text: string; contextLabel: string | null }
  | { kind: "event"; event: { type: string; [key: string]: any } };

export interface ChatSummary {
  id: string;
  title: string;
  created: number;
  updated: number;
}

export interface Chat extends ChatSummary {
  transcript: TranscriptItem[];
  messages: object[];
}

const MAX_TITLE = 80;
const MAX_RESULT_CHARS = 6000; // the panel only shows this much of a tool result anyway

export function newChat(firstMessage: string): Chat {
  const now = Date.now();
  const title = firstMessage.replace(/\s+/g, " ").trim();
  return {
    id: `${now.toString(36)}-${crypto.randomBytes(4).toString("hex")}`,
    title: title.length > MAX_TITLE ? title.slice(0, MAX_TITLE - 1) + "…" : title || "New chat",
    created: now,
    updated: now,
    transcript: [],
    messages: [],
  };
}

/** Shrinks what's worth keeping of a backend event before it's stored. */
export function transcriptEvent(event: { type: string; [key: string]: any }): TranscriptItem {
  if (event.type === "tool_result" && typeof event.result === "string" && event.result.length > MAX_RESULT_CHARS) {
    return { kind: "event", event: { ...event, result: event.result.slice(0, MAX_RESULT_CHARS) + "\n…[truncated]" } };
  }
  return { kind: "event", event };
}

export class ChatStore {
  private readonly dir: string;
  private index: ChatSummary[] | undefined;
  private writing: Promise<void> = Promise.resolve();

  constructor(storageDir: string) {
    this.dir = path.join(storageDir, "chats");
  }

  private file(id: string): string {
    if (!/^[a-z0-9-]+$/.test(id)) throw new Error(`bad chat id: ${id}`);
    return path.join(this.dir, `${id}.json`);
  }

  async list(): Promise<ChatSummary[]> {
    if (!this.index) {
      try {
        const parsed = JSON.parse(await fs.readFile(path.join(this.dir, "index.json"), "utf8"));
        this.index = Array.isArray(parsed) ? parsed : [];
      } catch {
        this.index = [];
      }
    }
    return [...this.index!].sort((a, b) => b.updated - a.updated);
  }

  async load(id: string): Promise<Chat | undefined> {
    try {
      return JSON.parse(await fs.readFile(this.file(id), "utf8")) as Chat;
    } catch {
      return undefined;
    }
  }

  /** Writes are queued so two saves of the same chat never interleave. */
  save(chat: Chat): Promise<void> {
    chat.updated = Date.now();
    const data = JSON.stringify(chat);
    const summary: ChatSummary = { id: chat.id, title: chat.title, created: chat.created, updated: chat.updated };
    this.writing = this.writing.then(async () => {
      await this.list();
      this.index = [summary, ...this.index!.filter((c) => c.id !== chat.id)];
      await fs.mkdir(this.dir, { recursive: true });
      await fs.writeFile(this.file(chat.id), data, "utf8");
      await this.writeIndex();
    }).catch(() => undefined);
    return this.writing;
  }

  delete(id: string): Promise<void> {
    this.writing = this.writing.then(async () => {
      await this.list();
      this.index = this.index!.filter((c) => c.id !== id);
      await fs.rm(this.file(id), { force: true });
      await this.writeIndex();
    }).catch(() => undefined);
    return this.writing;
  }

  private async writeIndex(): Promise<void> {
    await fs.mkdir(this.dir, { recursive: true });
    await fs.writeFile(path.join(this.dir, "index.json"), JSON.stringify(this.index), "utf8");
  }
}
