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
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
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
  /** Keys a card holds open on screen (`useHoldOpenTask`): the rail's
   *  pressed card, a held favourite's card. */
  held?: readonly string[];
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
  const held = new Set(ctx.held ?? []);
  const out: string[] = [];
  for (const t of tasks) {
    const peeked = ctx.peekedKey != null && ctx.peekedKey === t.key;
    const onPage = session !== "" && t.session_id === session;
    if (peeked || onPage || held.has(t.key)) out.push(t.key);
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

// A CHAT A CARD HOLDS OPEN (2026-11-11: "status update and unread dot
// clearing from the canvas rail side"). The rail's card (RailPeek) shows a
// chat over any page — a canvas, a folder — that is neither the side peek nor
// the URL's `session_id`, so a reply landing while the reader watched it there
// lit the ring's dot (and raised a "Task finished" notice) for a chat on
// screen. A card that holds a chat open says so here, and the document's one
// open-task reader (`useTaskStatusNotify`) counts it like the peek: marked
// read as it arrives, no notice. Counted, so two holders of one key (the list
// card and the favourite's) let go independently.
const heldOpen = new Map<string, number>();
let heldSnapshot: readonly string[] = [];
const heldListeners = new Set<() => void>();

function publishHeld(): void {
  heldSnapshot = Array.from(heldOpen.keys());
  for (const fn of heldListeners) fn();
}

/** Hold `key` open until the returned release is called (once). */
export function holdOpenTask(key: string): () => void {
  heldOpen.set(key, (heldOpen.get(key) ?? 0) + 1);
  publishHeld();
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const n = (heldOpen.get(key) ?? 1) - 1;
    if (n > 0) heldOpen.set(key, n);
    else heldOpen.delete(key);
    publishHeld();
  };
}

/** The keys cards hold open right now. */
export function readHeldOpenTasks(): readonly string[] {
  return heldSnapshot;
}

function subscribeHeld(fn: () => void): () => void {
  heldListeners.add(fn);
  return () => {
    heldListeners.delete(fn);
  };
}

/** Holds `key` open while it is non-null (a card showing that chat to the
 *  reader); null holds nothing. */
export function useHoldOpenTask(key: string | null): void {
  useEffect(() => (key ? holdOpenTask(key) : undefined), [key]);
}

/** The open-and-visible keys, recomputed on every poll. */
export function useOpenTaskKeys(tasks: readonly TaskPulseTask[]): string[] {
  const peekedKey = usePeekedKey();
  const visible = useDocumentVisible();
  const held = useSyncExternalStore(subscribeHeld, readHeldOpenTasks, readHeldOpenTasks);
  const search = typeof location === "undefined" ? "" : location.search;
  return openTaskKeys(tasks, { peekedKey, search, visible, held });
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
