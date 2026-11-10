// QUICK LOOK (owner, 2026-11-10; design.md "Space quick look"): hold Space,
// rest the pointer on a list ring, and that ring's chat floats above every
// card on screen; let Space go and it is gone — nothing under it moved.
// macOS Quick Look's gesture, for the rail: a glance, not a window. With a
// window A card pinned (claude-rail-lib `windowAHovers`) the rail's hover
// shows nothing, and this is the way to look at another chat without a click
// that would replace the one being worked on.
//
// THIS STORE IS THE WHOLE STATE, and it is its own: it never reads window A's
// or window B's stores and none of theirs reads it. The rail reports the
// pointer on its list rings (`aim` / `unaim`), the key layer (quick-look-keys)
// the Space key (`hold` / `release`), the card (QuickLook, mounted once in the
// shell) the pointer on itself (`enterCard` / `leaveCard`) and its Escape
// (`dismiss`).
//
// TIMINGS ARE WINDOW A'S (rail-peek-lib): a ring aimed at opens or swaps the
// card after PEEK_SWAP_MS — Space is already a statement of intent, so no
// PEEK_OPEN_MS dwell — and a pointer that leaves both the ring and the card
// lets it go after PEEK_CLOSE_MS, the grace that carries it across the gap.
// Space up, the window losing focus or going hidden: gone at once.
import { useSyncExternalStore } from "react";
import type { BoardColumn } from "@shell/schedule-lib";
import { PEEK_CLOSE_MS, PEEK_SWAP_MS, type Box, type Timers } from "./rail-peek-lib";

/** The chat a ring stands for, as the rail resolved it at the aim. */
export interface QuickLookChat {
  /** ChatMount `file` (tasks-lib `taskChatTarget`). */
  file: string;
  /** Null: a task with no conversation yet (a scheduled one not yet run). */
  sessionId: string | null;
  title: string;
  /** The ring's column (tasks-lib `taskColumn`), for the head's dot. */
  status: BoardColumn;
  model?: string;
  effort?: string;
  backend?: string;
}

export interface QuickLookState {
  /** Space is down. The card exists only while it is. */
  held: boolean;
  /** The ring whose chat the card shows; null while nothing shows. */
  key: string | null;
  /** That ring's box at the aim (viewport). */
  anchor: Box | null;
  /** That ring itself, for the card's anchor and the rail it stands beside. */
  ring: HTMLElement | null;
  chat: QuickLookChat | null;
  /** Bumped on every opening (nothing shown → shown), never on a swap. */
  seq: number;
}

export interface QuickLookAim {
  anchor: Box;
  ring?: HTMLElement | null;
  chat: QuickLookChat;
}

const IDLE: QuickLookState = { held: false, key: null, anchor: null, ring: null, chat: null, seq: 0 };

const windowTimers: Timers = {
  set: (fn, ms) => window.setTimeout(fn, ms),
  clear: (id) => window.clearTimeout(id),
};

let state: QuickLookState = IDLE;
let timers: Timers = windowTimers;
const subs = new Set<() => void>();
/** The aim waiting out PEEK_SWAP_MS. */
let pending: { key: string; aim: QuickLookAim } | null = null;
let swapTimer = 0;
let closeTimer = 0;
/** The ring the pointer rests on now (an aim not yet un-aimed). */
let pointerKey: string | null = null;
let overCard = false;

function write(next: QuickLookState): void {
  if (
    next.held === state.held &&
    next.key === state.key &&
    next.anchor === state.anchor &&
    next.chat === state.chat &&
    next.seq === state.seq
  )
    return;
  state = next;
  for (const fn of [...subs]) fn();
}

function clearSwap(): void {
  if (swapTimer) timers.clear(swapTimer);
  swapTimer = 0;
  pending = null;
}

function clearClose(): void {
  if (closeTimer) timers.clear(closeTimer);
  closeTimer = 0;
}

/** The shown ring left the DOM (a pulse dropped it, a page hop) while the
 *  pointer rested on it: no pointerleave ever comes, so the pointer is not
 *  "on the ring" any more (review, 2026-11-10). */
function ringGone(): boolean {
  return !!state.ring && state.ring.isConnected === false;
}

function pointerOnRing(): boolean {
  return pointerKey !== null && !ringGone();
}

function graceClose(): void {
  clearClose();
  if (state.key === null) return;
  closeTimer = timers.set(() => {
    closeTimer = 0;
    if (pointerOnRing() || overCard) return;
    write({ ...state, key: null, anchor: null, ring: null, chat: null });
  }, PEEK_CLOSE_MS);
}

/** Space went down (quick-look-keys guards it). Nothing shows yet. */
export function holdQuickLook(): void {
  if (state.held) return;
  write({ ...state, held: true });
}

/** Space came up, the window blurred or went hidden: the card goes now. */
export function releaseQuickLook(): void {
  clearSwap();
  clearClose();
  pointerKey = null;
  overCard = false;
  write({ ...state, held: false, key: null, anchor: null, ring: null, chat: null });
}

/** Escape, or a ring's press: the card goes, Space stays held — the next ring
 *  the pointer enters opens it again. */
export function dismissQuickLook(): void {
  clearSwap();
  clearClose();
  overCard = false;
  if (state.key === null) return;
  write({ ...state, key: null, anchor: null, ring: null, chat: null });
}

/** The pointer entered list ring `key`. Ignored unless Space is held. */
export function aimQuickLook(key: string, aim: QuickLookAim): void {
  if (!state.held) return;
  pointerKey = key;
  clearClose();
  // Back on the ring it shows: a swap still waiting for another is off.
  if (state.key === key) {
    clearSwap();
    return;
  }
  if (pending?.key === key) return;
  clearSwap();
  pending = { key, aim };
  swapTimer = timers.set(() => {
    swapTimer = 0;
    const p = pending;
    pending = null;
    if (!p || !state.held) return;
    const opening = state.key === null;
    write({
      ...state,
      key: p.key,
      anchor: p.aim.anchor,
      ring: p.aim.ring ?? null,
      chat: p.aim.chat,
      seq: opening ? state.seq + 1 : state.seq,
    });
  }, PEEK_SWAP_MS);
}

/** The pointer left the ring it was aimed at: an aim still waiting is called
 *  off; a card up goes after the grace unless the pointer reaches it. */
export function unaimQuickLook(): void {
  pointerKey = null;
  clearSwap();
  if (!overCard) graceClose();
}

/** The pointer is on the card: it stays. */
export function enterQuickLookCard(): void {
  overCard = true;
  clearClose();
}

/** The pointer left the card: the grace, unless it went onto a ring. */
export function leaveQuickLookCard(): void {
  overCard = false;
  if (!pointerOnRing()) graceClose();
}

export function quickLookState(): QuickLookState {
  return state;
}

export function subscribeQuickLook(fn: () => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}

/** The card as it stands, re-rendering on every change. */
export function useQuickLook(): QuickLookState {
  return useSyncExternalStore(subscribeQuickLook, quickLookState, quickLookState);
}

export function resetQuickLookForTests(t?: Timers): void {
  clearSwap();
  clearClose();
  timers = t ?? windowTimers;
  state = IDLE;
  pointerKey = null;
  overCard = false;
  subs.clear();
}
