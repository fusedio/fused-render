// THE CHAT THE READER LAST PICKED (owner, 2026-11-02: "last selected stuff
// should be in expanded claude sidebar view as well").
//
// The canvas rail marks the ring pressed last (ClaudeRail `.is-last`); the
// pane's Recent chats list (apps/claude ui/Lists) marks the same row once the
// pane is open. One answer for both, so collapsing or expanding the pane
// keeps the mark where the reader left it.
//
// TWO MARKS, NOT ONE (owner, 2026-11-04: "last select of fav is diff from
// last selected of the list, both of them should show their last selected"):
// the FAVOURITES keep their own (`useLastFav`, sessionStorage
// `fused-render:last-fav`) — a press on a favourite's ring (the rail's
// favourites section, the pane's Favourites row) moves only that one, and a
// press on a list ring (the rail's chats, the pane's Recent row) only the
// list's. Both show at once, one in each section. Only a PRESS moves either:
// a favourite opened from another window (`starred_open`) moves nothing. A
// chat unstarred back into the list keeps the list's mark where it was; the
// favourites' mark on it just matches nothing any more.
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

/** A row's identity for the mark: its project and its task number (or its
 *  key, before it has one). */
export function lastChatId(t: { project?: string | null; task_id?: string | null; key: string }): string {
  return `${t.project || ""}\u0000${t.task_id || t.key}`;
}

/** One mark, kept under one sessionStorage key (header): the list's, or the
 *  favourites'. The two run the same code and never touch each other. */
interface MarkStore {
  get(): string | null;
  set(id: string): void;
  onStorage(e: Pick<StorageEvent, "key" | "storageArea">): void;
  subscribe(fn: () => void): () => void;
}

function markStore(key: string): MarkStore {
  const subs = new Set<() => void>();
  const read = (): string | null => {
    try {
      return typeof sessionStorage === "undefined" ? null : sessionStorage.getItem(key);
    } catch {
      return null;
    }
  };
  let mem: string | null = read();
  /** The last write was refused (private mode, quota): `mem` is the only
   *  copy, and a re-read from storage would throw it away. */
  let unsaved = false;
  const notify = () => {
    for (const fn of subs) fn();
  };
  const onStorage = (e: Pick<StorageEvent, "key" | "storageArea">): void => {
    if (typeof sessionStorage === "undefined" || e.storageArea !== sessionStorage) return;
    if (e.key !== key && e.key !== null) return;
    const next = read();
    unsaved = false;
    if (next === mem) return;
    mem = next;
    notify();
  };
  let listening = false;
  return {
    get: () => mem,
    set: (id) => {
      if (id === mem) return;
      mem = id;
      try {
        sessionStorage.setItem(key, id);
        unsaved = false;
      } catch {
        // Storage refused (private mode, quota): this document still knows.
        unsaved = true;
      }
      notify();
    },
    onStorage,
    subscribe: (fn) => {
      subs.add(fn);
      // A second document (the node chat's frame) loaded its copy of this
      // module before a press elsewhere: catch up before the first read —
      // unless this document holds a pick storage refused, which a re-read
      // would drop.
      if (!unsaved) mem = read();
      if (!listening && typeof window !== "undefined") {
        listening = true;
        window.addEventListener("storage", onStorage);
      }
      return () => {
        subs.delete(fn);
      };
    },
  };
}

const list = markStore("fused-render:last-chat");
const fav = markStore("fused-render:last-fav");

/** The last chat picked in this window's LISTS, as `lastChatId` spells it. */
export function lastChat(): string | null {
  return list.get();
}

/** A press on a list chat (a rail ring, a Recent row) makes it the last one. */
export function setLastChat(id: string): void {
  list.set(id);
}

/** Another document of this window wrote the mark (the node chat's frame,
 *  or the page around it): adopt it. The `storage` listener; exported so the
 *  sync can be tested without a browser. */
export function onLastChatStorage(e: Pick<StorageEvent, "key" | "storageArea">): void {
  list.onStorage(e);
}

/** Follow the mark: `fn` runs on every change, this document's or another's.
 *  Returns the unsubscribe. */
export function subscribeLastChat(fn: () => void): () => void {
  return list.subscribe(fn);
}

/** The last chat picked, live: re-renders when any document of this window
 *  picks another. */
export function useLastChat(): string | null {
  return useSyncExternalStore(subscribeLastChat, lastChat, lastChat);
}

/** The FAVOURITES' own last pick (header), as `lastChatId` spells it. */
export function lastFav(): string | null {
  return fav.get();
}

/** A press on a favourite (the rail's favourites ring, the pane's Favourites
 *  row) makes it the favourites' last one; the list's mark stays put. */
export function setLastFav(id: string): void {
  fav.set(id);
}

/** `onLastChatStorage`, for the favourites' mark. */
export function onLastFavStorage(e: Pick<StorageEvent, "key" | "storageArea">): void {
  fav.onStorage(e);
}

/** `subscribeLastChat`, for the favourites' mark. */
export function subscribeLastFav(fn: () => void): () => void {
  return fav.subscribe(fn);
}

/** The favourites' last pick, live. */
export function useLastFav(): string | null {
  return useSyncExternalStore(subscribeLastFav, lastFav, lastFav);
}
