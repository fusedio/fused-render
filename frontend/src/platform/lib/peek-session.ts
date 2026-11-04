// WHICH CHATS ARE ON SCREEN ELSEWHERE, for the favourite's card
// (shell/claude-rail/FavouritePeek): the same session there and in another
// mounted composer would be two composers on one draft, so the card keeps no
// chat of its own while another mount shows it — and ONLY then (owner,
// 2026-11-04: the card said "Open in the Claude panel" with the panel shut).
// Both signals are POSITIVE: a mount says what it shows while it is mounted,
// and takes it back on unmount — never a guess from the page's URL, which
// keeps `session_id` with the pane shut.
//
//   - the Tasks side peek (shell/TaskPeek.tsx) publishes the session its
//     chat is on (`publishPeekSession`), null while it shows none;
//   - every URL-backed chat mount — the app page's and the canvas page's
//     Claude pane, the explorer's sidebar, content pane and folder pane —
//     announces the URL's `session_id` while it is mounted
//     (apps/claude/ChatMount `announcePaneSession`).
//
// THE PANE ANNOUNCEMENTS CROSS FRAMES (Bugbot, PR #2108): the canvas page's
// node chat runs in its own same-origin frame (CanvasWorkspace
// `preview-side-frame`), whose modules are a second copy — an announcement
// kept in module memory alone never reaches the shell's card, and the card
// kept its composer beside the node's. So, like platform/lib/last-chat, each
// document also writes its own announcements to sessionStorage
// (`fused-render:pane-sessions`, a JSON array of `{sid, doc}`; `doc` is an id
// minted per document load) and every document reads the union — its own
// memory plus the other documents' entries — again on each `storage` event.
// A document takes its entries back on unmount and on `pagehide` (a frame
// removed from the page never unmounts its React tree). Leftovers of a
// document that died without a `pagehide`: the top document of the origin
// clears the key when it loads, before it renders any frame. Storage refused
// (private mode, quota): this document's own memory still answers for it.
// The Tasks side peek stays in memory: it is the shell's own.
import { useSyncExternalStore } from "react";

let current: string | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

/** The peek says what it shows (null: nothing, or closed). */
export function publishPeekSession(sid: string | null): void {
  if (current === sid) return;
  current = sid;
  emit();
}

export function peekSession(): string | null {
  return current;
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function usePeekSession(): string | null {
  return useSyncExternalStore(subscribe, peekSession, peekSession);
}

/** One entry per mounted pane chat of THIS document (a mount may repeat
 *  another's session). */
const panes = new Map<number, string>();
let paneSeq = 0;
const NONE: readonly string[] = [];
let paneSnap: readonly string[] = NONE;

const PANES_KEY = "fused-render:pane-sessions";
/** This document's load (header): its entries in storage carry it. */
const DOC = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

interface PaneEntry {
  sid: string;
  doc: string;
}

function storage(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

/** Every document's entries as storage holds them ([] when it holds none,
 *  garbage, or refuses). */
function readStored(): PaneEntry[] {
  try {
    const raw = storage()?.getItem(PANES_KEY);
    if (!raw) return [];
    const v: unknown = JSON.parse(raw);
    if (!Array.isArray(v)) return [];
    return v.filter(
      (e): e is PaneEntry => !!e && typeof e.sid === "string" && typeof e.doc === "string",
    );
  } catch {
    return [];
  }
}

/** This document's entries replace what storage held for it; `own` false
 *  takes them all back (`pagehide`). */
function writeOwn(own: boolean): void {
  const s = storage();
  if (!s) return;
  try {
    const mine = own ? [...new Set(panes.values())].map((sid) => ({ sid, doc: DOC })) : [];
    const next = [...readStored().filter((e) => e.doc !== DOC), ...mine];
    if (next.length) s.setItem(PANES_KEY, JSON.stringify(next));
    else s.removeItem(PANES_KEY);
  } catch {
    // Storage refused (private mode, quota): this document still knows.
  }
}

function settlePanes(): void {
  const others = readStored()
    .filter((e) => e.doc !== DOC)
    .map((e) => e.sid);
  const next = [...new Set([...panes.values(), ...others])].sort();
  if (next.length === paneSnap.length && next.every((s, i) => s === paneSnap[i])) return;
  paneSnap = next.length ? next : NONE;
  emit();
}

/** A pane's chat on `sid` is mounted: the answer takes it back. */
export function announcePaneSession(sid: string): () => void {
  const id = ++paneSeq;
  panes.set(id, sid);
  writeOwn(true);
  settlePanes();
  return () => {
    panes.delete(id);
    writeOwn(true);
    settlePanes();
  };
}

/** Another document of this window announced or took back a pane chat: re-
 *  read. The `storage` listener; exported so the sync is testable without a
 *  browser. */
export function onPaneSessionsStorage(e: Pick<StorageEvent, "key" | "storageArea">): void {
  const s = storage();
  if (!s || e.storageArea !== s) return;
  if (e.key !== PANES_KEY && e.key !== null) return;
  settlePanes();
}

/** The top document of the origin (no same-origin parent): its load starts
 *  the window's page over (header). */
function isTopOfOrigin(): boolean {
  try {
    return window.parent === window || window.parent.location.origin !== location.origin;
  } catch {
    return true;
  }
}

if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  if (isTopOfOrigin()) {
    try {
      storage()?.removeItem(PANES_KEY);
    } catch {
      /* refused: nothing stored to clear */
    }
  }
  window.addEventListener("storage", onPaneSessionsStorage);
  window.addEventListener("pagehide", () => writeOwn(false));
  // Back from the bfcache with its mounts intact: say so again.
  window.addEventListener("pageshow", (e: PageTransitionEvent) => {
    if (e.persisted) writeOwn(true);
  });
}

/** The sessions the window's pane chats show now — this document's and every
 *  same-origin frame's (header) — sorted, deduplicated. */
export function paneSessions(): readonly string[] {
  return paneSnap;
}

function subscribePanes(l: () => void): () => void {
  const off = subscribe(l);
  // Another document may have written before this one first listened.
  settlePanes();
  return off;
}

export function usePaneSessions(): readonly string[] {
  return useSyncExternalStore(subscribePanes, paneSessions, paneSessions);
}
