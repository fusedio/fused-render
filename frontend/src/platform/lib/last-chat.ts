// THE CHAT THE READER LAST PICKED (owner, 2026-11-02: "last selected stuff
// should be in expanded claude sidebar view as well").
//
// The canvas rail marks the ring pressed last (ClaudeRail `.is-last`); the
// pane's Recent chats list (apps/claude ui/Lists) marks the same row once the
// pane is open. One answer for both, so collapsing or expanding the pane
// keeps the mark where the reader left it.
//
// SESSION STORAGE, not module memory: the node chat runs in its own
// same-origin frame (CanvasWorkspace `preview-side-frame`), whose modules are
// a second copy — the `storage` event is what carries a press across. Per
// window (a second window has its own), and it survives a reload.
//
// THE ID is the project plus the task number: `task_id` ("TASK-003") is
// numbered per project, and the key alone is rewritten when a queued row
// dispatches (`pending:<entry>` → its session), which the number survives.
import { useSyncExternalStore } from "react";

const KEY = "fused-render:last-chat";

/** A row's identity for the mark: its project and its task number (or its
 *  key, before it has one). */
export function lastChatId(t: { project?: string | null; task_id?: string | null; key: string }): string {
  return `${t.project || ""}\u0000${t.task_id || t.key}`;
}

const subs = new Set<() => void>();
let mem: string | null = read();

function read(): string | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function notify(): void {
  for (const fn of subs) fn();
}

/** The last chat picked in this window, as `lastChatId` spells it. */
export function lastChat(): string | null {
  return mem;
}

/** A press on a chat (a rail ring, a Recent row) makes it the last one. */
export function setLastChat(id: string): void {
  if (id === mem) return;
  mem = id;
  try {
    sessionStorage.setItem(KEY, id);
  } catch {
    // Storage refused (private mode, quota): this document still knows.
  }
  notify();
}

let listening = false;
function subscribe(fn: () => void): () => void {
  subs.add(fn);
  // A second document (the node chat's frame) loaded its copy of this module
  // before a press elsewhere: catch up before the first read.
  mem = read();
  if (!listening && typeof window !== "undefined") {
    listening = true;
    window.addEventListener("storage", (e) => {
      if (e.storageArea !== sessionStorage || (e.key !== KEY && e.key !== null)) return;
      const next = read();
      if (next === mem) return;
      mem = next;
      notify();
    });
  }
  return () => {
    subs.delete(fn);
  };
}

/** The last chat picked, live: re-renders when any document of this window
 *  picks another. */
export function useLastChat(): string | null {
  return useSyncExternalStore(subscribe, lastChat, lastChat);
}
