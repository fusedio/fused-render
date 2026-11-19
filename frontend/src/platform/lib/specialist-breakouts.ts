// SPECIALIST BREAKOUTS — the shell's open specialist breakout cards
// (docs/specialists.md). A MODULE STORE like
// breakouts.ts: a card is requested from inside a task chat (the offer card,
// the Breakout menu, a closed chip's Reopen) and drawn by the one overlay the
// shell mounts (shell/breakout/BreakoutCards → SpecialistBreakoutCard).
//
// In-page only (docs/specialists.md §11): after a reload the task lists its breakouts
// from the server as chips with Reopen.
import { useSyncExternalStore } from "react";
import type { Brief } from "./specialists-api";
import { COMPOSER_DRAFT_EVENT, type ComposerDraftDetail } from "./breakouts";
import { chatDraftKey, fetchChatDraft, saveChatDraft, type ChatDraft, type DraftAttachment } from "./drafts";

export interface SpecialistBreakoutCard {
  id: string;
  breakoutId: string;
  specialistId: string;
  specialistName: string;
  /** The specialist's home folder: the card's chat runs there. */
  home: string;
  parentSessionId: string;
  parentFile: string;
  parentTitle?: string;
  /** Set on reopen / after the first start. */
  sessionId?: string;
  /** The first message, sent once. */
  seed?: string;
  /** `{specialist: "<id>:breakout:<bid>"}`, merged into every start. */
  startParams: Record<string, string>;
  closing?: boolean;
  /** How many closes this breakout has had (a second close drafts a follow-up). */
  closes?: number;
  /** The brief, from the breakout record: the thread draws the seed as it. */
  brief?: Brief;
  /** The task's folder: file links in the replies resolve there (not in the
   *  breakout's private workroom). */
  parentFolder?: string;
  /** Where the task's chat sat on screen when the card was asked for, so the
   *  card opens beside it rather than on top of it. Viewport pixels. */
  anchor?: { left: number; top: number; right: number; bottom: number };
}

/** The on-screen box of the chat an element sits in, for a card's `anchor`. */
export function chatAnchorOf(el: Element | null | undefined): SpecialistBreakoutCard["anchor"] | undefined {
  const root = el?.closest?.(".chat-root");
  const r = root?.getBoundingClientRect?.();
  if (!r || !(r.width > 0)) return undefined;
  return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
}

let cards: readonly SpecialistBreakoutCard[] = [];
let seq = 0;
const listeners = new Set<() => void>();
const emit = (next: readonly SpecialistBreakoutCard[]): void => {
  cards = next;
  for (const l of [...listeners]) l();
};

export const getSpecialistBreakouts = (): readonly SpecialistBreakoutCard[] => cards;
export const subscribeSpecialistBreakouts = (fn: () => void): (() => void) => {
  listeners.add(fn);
  return () => void listeners.delete(fn);
};
export const useSpecialistBreakouts = (): readonly SpecialistBreakoutCard[] =>
  useSyncExternalStore(subscribeSpecialistBreakouts, getSpecialistBreakouts, getSpecialistBreakouts);

/** Open (or raise) the card for a breakout. Deduped on `breakoutId`: a second
 *  open of the same breakout raises the card and takes the newer facts (a
 *  reopen hands the session id). Returns the card id. */
export function openSpecialistBreakout(c: Omit<SpecialistBreakoutCard, "id">): string {
  const same = cards.find((x) => x.breakoutId === c.breakoutId);
  if (same) {
    const merged: SpecialistBreakoutCard = { ...same, ...c, id: same.id };
    emit([...cards.filter((x) => x !== same), merged]);
    return same.id;
  }
  seq += 1;
  const card: SpecialistBreakoutCard = { ...c, id: `spx-breakout-${Date.now().toString(36)}-${seq}` };
  emit([...cards, card]);
  return card.id;
}

/** Patch a card in place (its session id once known, the seed spent, closing). */
export function updateSpecialistBreakout(id: string, patch: Partial<Omit<SpecialistBreakoutCard, "id">>): void {
  const c = cards.find((x) => x.id === id);
  if (!c) return;
  const next = { ...c, ...patch };
  for (const k of Object.keys(patch) as (keyof typeof patch)[]) if (patch[k] === undefined) delete next[k];
  emit(cards.map((x) => (x === c ? next : x)));
}

export function closeSpecialistBreakoutCard(id: string): void {
  if (cards.some((c) => c.id === id)) emit(cards.filter((c) => c.id !== id));
}

/** Test seam. */
export function resetSpecialistBreakouts(): void {
  seq = 0;
  emit([]);
}

// ── THE SUMMARY DRAFT (docs/specialists.md §5) ────────────────────────────────
// A closed breakout's summary waits as its OWN block above the task's
// composer ("Draft from the Canvas specialist · not sent yet", Clear draft,
// Send to the task) until the reader sends or clears it. It is kept per task
// chat (session + folder) in this module and in localStorage, so a reload,
// or a task chat that mounts later, finds it again. The reader's own typed
// words stay in the composer, untouched, and go first when it is sent.

export interface SummaryDraftPart {
  text: string;
  label: string;
  /** The specialist's name. */
  name: string;
  breakoutId?: string;
  at: number;
}

const DRAFTS_KEY = "fused:specialist-summary-drafts";
let drafts: Record<string, readonly SummaryDraftPart[]> | null = null;
const draftListeners = new Set<() => void>();

export const summaryDraftKey = (sessionId: string, file: string): string => `${sessionId}\u0000${file}`;

function loadDrafts(): Record<string, readonly SummaryDraftPart[]> {
  if (drafts) return drafts;
  drafts = {};
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(DRAFTS_KEY) : null;
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    if (parsed && typeof parsed === "object") {
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        if (Array.isArray(v)) drafts[k] = v.filter((p) => p && typeof p.text === "string");
      }
    }
  } catch {
    // Unreadable storage: start empty; this page still keeps what arrives.
  }
  return drafts;
}

function writeDrafts(next: Record<string, readonly SummaryDraftPart[]>): void {
  drafts = next;
  try {
    if (typeof localStorage !== "undefined") localStorage.setItem(DRAFTS_KEY, JSON.stringify(next));
  } catch {
    // Kept in this page only.
  }
  for (const l of [...draftListeners]) l();
}

const NO_PARTS: readonly SummaryDraftPart[] = [];

export function readSummaryDraft(sessionId: string, file: string): readonly SummaryDraftPart[] {
  return loadDrafts()[summaryDraftKey(sessionId, file)] ?? NO_PARTS;
}

/** Add a summary to the task's waiting draft (a second breakout's summary, or
 *  a second close of the same one, is added below the first). */
export function addSummaryDraft(sessionId: string, file: string, part: SummaryDraftPart): void {
  const all = loadDrafts();
  const key = summaryDraftKey(sessionId, file);
  const prior = (all[key] ?? []).filter((p) => p.text !== part.text);
  writeDrafts({ ...all, [key]: [...prior, part] });
}

export function clearSummaryDraft(sessionId: string, file: string): void {
  const all = loadDrafts();
  const key = summaryDraftKey(sessionId, file);
  if (!(key in all)) return;
  const next = { ...all };
  delete next[key];
  writeDrafts(next);
}

function subscribeDrafts(fn: () => void): () => void {
  draftListeners.add(fn);
  // Another tab's write (or this one's after a reload) is read afresh.
  const onStorage = (e: StorageEvent) => {
    if (e.key !== DRAFTS_KEY) return;
    drafts = null;
    fn();
  };
  if (typeof window !== "undefined") window.addEventListener?.("storage", onStorage);
  return () => {
    draftListeners.delete(fn);
    if (typeof window !== "undefined") window.removeEventListener?.("storage", onStorage);
  };
}

export function useSummaryDraft(sessionId: string, file: string): readonly SummaryDraftPart[] {
  const get = () => (sessionId ? readSummaryDraft(sessionId, file) : NO_PARTS);
  return useSyncExternalStore(subscribeDrafts, get, get);
}

/** The summary as it goes to the task: each part, a blank line between. */
export function summaryDraftText(parts: readonly SummaryDraftPart[]): string {
  return parts.map((p) => p.text.trim()).filter(Boolean).join("\n\n");
}

/** Test seam. */
export function resetSummaryDraftsForTests(): void {
  drafts = {};
  try {
    if (typeof localStorage !== "undefined") localStorage.removeItem(DRAFTS_KEY);
  } catch {
    // nothing
  }
  for (const l of [...draftListeners]) l();
}

/**
 * A SPECIALIST BREAKOUT'S SUMMARY, to the task. With a `label` (a specialist
 * breakout) it becomes the task's summary draft (above) and resolves true. A
 * plain draft with no label keeps Send back's two roads (breakouts.ts
 * `sendBackBreakout`): a mounted task chat takes the cancelable
 * `fused:composer-draft` event and appends it below what the reader typed;
 * unhandled, the persisted draft is read and `existing + text` is saved.
 * Resolves false when the saved draft could not be read or written.
 */
export async function deliverComposerDraft(
  detail: ComposerDraftDetail & { name?: string; breakoutId?: string },
  save: (key: string, text: string, attachments: readonly DraftAttachment[]) => unknown = (k, t, a) =>
    saveChatDraft(k, t, a),
  load: (key: string) => Promise<ChatDraft | null | undefined> = fetchChatDraft,
): Promise<boolean> {
  if (detail.label) {
    addSummaryDraft(detail.sessionId, detail.file, {
      text: detail.text,
      label: detail.label,
      name: detail.name || "",
      ...(detail.breakoutId ? { breakoutId: detail.breakoutId } : {}),
      at: Date.now() / 1000,
    });
    return true;
  }
  const handled = !window.dispatchEvent(new CustomEvent(COMPOSER_DRAFT_EVENT, { detail, cancelable: true }));
  if (handled) return true;
  const key = chatDraftKey(detail.sessionId, detail.file);
  const existing = await load(key);
  if (existing === undefined) return false;
  const prior = existing?.text ?? "";
  const merged = prior.trim() ? `${prior}\n\n${detail.text}` : detail.text;
  const res = (await save(key, merged, existing?.attachments ?? [])) as { ok?: boolean } | undefined;
  return !(res && res.ok === false);
}
