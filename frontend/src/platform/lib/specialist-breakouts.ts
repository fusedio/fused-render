// SPECIALIST BREAKOUTS — the shell's open specialist breakout cards
// (docs/specialists/PLAN.md B.9, PRODUCT §3.3). A MODULE STORE like
// breakouts.ts: a card is requested from inside a task chat (the offer card,
// the Breakout menu, a closed chip's Reopen) and drawn by the one overlay the
// shell mounts (shell/breakout/BreakoutCards → SpecialistBreakoutCard).
//
// In-page only (PLAN D.2 #12): after a reload the task lists its breakouts
// from the server as chips with Reopen.
import { useSyncExternalStore } from "react";
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

/**
 * A SPECIALIST BREAKOUT'S SUMMARY, into the task's composer (PRODUCT §3.3.7):
 * the same two roads as Send back (breakouts.ts `sendBackBreakout`). A mounted
 * task chat takes the cancelable `fused:composer-draft` event, appends the text
 * below what the reader typed and shows `label`; unhandled, the persisted draft
 * is read and `existing + text` is saved so the chat that mounts later seeds
 * from it. Resolves false when the saved draft could not be read or written.
 */
export async function deliverComposerDraft(
  detail: ComposerDraftDetail,
  save: (key: string, text: string, attachments: readonly DraftAttachment[]) => unknown = (k, t, a) =>
    saveChatDraft(k, t, a),
  load: (key: string) => Promise<ChatDraft | null | undefined> = fetchChatDraft,
): Promise<boolean> {
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
