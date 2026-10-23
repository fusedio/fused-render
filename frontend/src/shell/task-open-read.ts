// A task you have open is never unread (Workspace scope, exception 1).
//
// Read state used to be set only on a click-through, so a message that arrived
// while the task sat open in the peek (or in its full chat page) stayed unread
// and the row, the sidebar badge and the notice all kept calling for a task the
// reader was looking at. The rule here: while a task is open AND the document is
// visible, every unread message on it is marked read as the poll reports it. A
// hidden or backgrounded window marks nothing, so what arrives while you are
// away is still news when you come back.
//
// The decisions are pure (`openTaskKeys`, `unreadOpenKeys`); the hook at the
// foot only feeds them the peek store, the URL and the visibility state, and
// spends the answer on the one whole-task read call the List's Mark read button
// already makes.
import { useEffect, useRef, useState } from "react";
import { markWholeTaskRead } from "@platform/lib/api";
import type { TaskPulseTask } from "@platform/lib/api";
import { usePeekedKey } from "@shell/task-peek-store";

export interface OpenContext {
  /** The task the side peek shows, if any (`usePeekedKey`). */
  peekedKey: string | null;
  /** `location.search` of this document: a full chat page carries the
   *  conversation as `session_id`. */
  search: string;
  /** `document.visibilityState === "visible"`. */
  visible: boolean;
}

/** Keys of the tasks the reader has open and can see right now. */
export function openTaskKeys(tasks: readonly TaskPulseTask[], ctx: OpenContext): string[] {
  if (!ctx.visible) return [];
  let session = "";
  try {
    const q = new URLSearchParams(ctx.search);
    // session_id survives a switch to another side view, so it only counts as
    // the chat being open when the side is the chat (absent, empty or claude).
    const side = q.get("_side") ?? "";
    if (side === "" || side === "claude") session = q.get("session_id") ?? "";
  } catch {
    session = "";
  }
  const out: string[] = [];
  for (const t of tasks) {
    const peeked = ctx.peekedKey != null && ctx.peekedKey === t.key;
    const onPage = session !== "" && t.session_id === session;
    if (peeked || onPage) out.push(t.key);
  }
  return out;
}

/** Of the open keys, those whose row still reports unread messages. */
export function unreadOpenKeys(tasks: readonly TaskPulseTask[], open: readonly string[]): string[] {
  const want = new Set(open);
  return tasks.filter((t) => want.has(t.key) && t.unread > 0).map((t) => t.key);
}

function documentVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

/** Tracks `document.visibilityState` as state, so a window coming back to the
 *  front re-runs the effect that marks what arrived while it was away. */
export function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState<boolean>(documentVisible);
  useEffect(() => {
    if (typeof document === "undefined") return;
    const on = (): void => setVisible(documentVisible());
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}

/** The open-and-visible keys, recomputed on every poll. */
export function useOpenTaskKeys(tasks: readonly TaskPulseTask[]): string[] {
  const peekedKey = usePeekedKey();
  const visible = useDocumentVisible();
  const search = typeof location === "undefined" ? "" : location.search;
  return openTaskKeys(tasks, { peekedKey, search, visible });
}

/** Marks every unread message of an open, visible task read. One request per
 *  task at a time: the next poll reports the new count, and a still-unread
 *  answer after a failed write is simply asked again. */
export function useAutoReadOpenTasks(
  tasks: readonly TaskPulseTask[],
  open: readonly string[],
): void {
  const inFlight = useRef<Set<string>>(new Set());
  useEffect(() => {
    for (const key of unreadOpenKeys(tasks, open)) {
      if (inFlight.current.has(key)) continue;
      inFlight.current.add(key);
      void markWholeTaskRead(key)
        .catch(() => {})
        .finally(() => {
          inFlight.current.delete(key);
        });
    }
  }, [tasks, open.join("\u0000")]);
}
