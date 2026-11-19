// SPECIALIST BREAKOUTS — the shell's open specialist breakout cards
// (docs/specialists.md). A MODULE STORE like
// breakouts.ts: a card is requested from inside a task chat (the offer card,
// the Breakout menu, a closed chip's Reopen) and drawn by the one overlay the
// shell mounts (shell/breakout/BreakoutCards → SpecialistBreakoutCard).
//
// In-page only (docs/specialists.md §11): after a reload the task lists its breakouts
// from the server as chips with Reopen.
import { useEffect, useSyncExternalStore } from "react";
import { listBreakouts, setBreakoutDraft, type BreakoutRecord, type Brief } from "./specialists-api";
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
// Send to the task) until the reader sends or clears it. The SERVER keeps it
// (D1386: a fact the reader would miss across a relaunch): every close on the
// breakout record carries its draft and a `draft_state`, and the block is the
// task's closes still `pending` (GET /api/specialists/breakouts). This module
// holds the last answer per task session in memory, for immediacy, and
// Send / Clear mark the closes through POST …/breakouts/{bid}/draft. The
// reader's own typed words stay in the composer, untouched, and go first
// when it is sent.

export interface SummaryDraftPart {
  text: string;
  label: string;
  /** The specialist's name. */
  name: string;
  breakoutId?: string;
  specialistId?: string;
  /** The close's key on the breakout record. */
  close?: string;
  at: number;
}

const draftLabel = (name: string): string => `Draft from the ${name} specialist · not sent yet`;

/** The task's waiting summary draft from its breakout records: every close
 *  still `pending`, of a breakout that is not discarded or deleted. */
export function summaryDraftFromRecords(records: readonly BreakoutRecord[] | null | undefined): SummaryDraftPart[] {
  const out: SummaryDraftPart[] = [];
  for (const rec of records ?? []) {
    if (!rec || rec.status === "discarded" || rec.status === "deleted") continue;
    const name = rec.name || "";
    for (const c of rec.closes ?? []) {
      if (!c || c.draft_state !== "pending" || typeof c.draft !== "string" || !c.key) continue;
      out.push({
        text: c.draft,
        label: draftLabel(name),
        name,
        breakoutId: rec.id,
        specialistId: rec.specialist,
        close: c.key,
        at: Number(c.at) || 0,
      });
    }
  }
  return out;
}

const drafts = new Map<string, readonly SummaryDraftPart[]>();
/** Bumped by every local change: a read that started before it is stale. */
const draftGen = new Map<string, number>();
const draftListeners = new Set<() => void>();
const NO_PARTS: readonly SummaryDraftPart[] = [];

const bump = (sessionId: string): number => {
  const n = (draftGen.get(sessionId) ?? 0) + 1;
  draftGen.set(sessionId, n);
  return n;
};

function setParts(sessionId: string, parts: readonly SummaryDraftPart[]): void {
  if (parts.length) drafts.set(sessionId, parts);
  else drafts.delete(sessionId);
  for (const l of [...draftListeners]) l();
}

export function readSummaryDraft(sessionId: string): readonly SummaryDraftPart[] {
  return drafts.get(sessionId) ?? NO_PARTS;
}

/** Show a summary at once (the close that made it is already on the server;
 *  the next read confirms it). Same close twice is one part. */
export function addSummaryDraft(sessionId: string, part: SummaryDraftPart): void {
  const prior = readSummaryDraft(sessionId).filter((p) =>
    part.close && p.close ? p.close !== part.close : p.text !== part.text,
  );
  bump(sessionId);
  setParts(sessionId, [...prior, part]);
}

/** Read the task's waiting draft from the server. Resolves false when the
 *  read failed (what is on screen stays). */
export async function loadSummaryDraft(
  sessionId: string,
  list: (sid: string) => Promise<{ breakouts: BreakoutRecord[] }> = listBreakouts,
): Promise<boolean> {
  if (!sessionId) return false;
  const gen = draftGen.get(sessionId) ?? 0;
  try {
    const r = await list(sessionId);
    if ((draftGen.get(sessionId) ?? 0) !== gen) return true; // a newer local change wins
    setParts(sessionId, summaryDraftFromRecords(r?.breakouts));
    return true;
  } catch {
    return false;
  }
}

/** A close that is not there any more, already resolved, or whose specialist
 *  is gone has nothing left to mark: drop it like a success. */
const nothingToMark = (err: unknown): boolean => {
  const status = (err as { status?: number })?.status;
  return status === 404 || status === 409 || status === 410;
};

/** Send to the task / Clear draft: mark each part's close `state` on the
 *  server, then drop the marked parts from the block. Resolves false when a
 *  mark failed (those parts stay). */
export async function resolveSummaryDraft(
  sessionId: string,
  parts: readonly SummaryDraftPart[],
  state: "sent" | "cleared",
  mark: (id: string, bid: string, close: string, state: "sent" | "cleared") => Promise<unknown> = setBreakoutDraft,
): Promise<boolean> {
  const done = new Set<SummaryDraftPart>();
  let ok = true;
  for (const p of parts) {
    if (!p.specialistId || !p.breakoutId || !p.close) {
      ok = false;
      continue;
    }
    try {
      await mark(p.specialistId, p.breakoutId, p.close, state);
      done.add(p);
    } catch (err) {
      if (nothingToMark(err)) done.add(p);
      else ok = false;
    }
  }
  const keys = new Set([...done].map((p) => p.close));
  bump(sessionId);
  setParts(sessionId, readSummaryDraft(sessionId).filter((p) => !keys.has(p.close)));
  return ok;
}

function subscribeDrafts(fn: () => void): () => void {
  draftListeners.add(fn);
  return () => void draftListeners.delete(fn);
}

/** The task's waiting summary draft; read from the server when the task's
 *  chat mounts (and whenever `refreshKey` changes). */
export function useSummaryDraft(sessionId: string, refreshKey: unknown = 0): readonly SummaryDraftPart[] {
  useEffect(() => {
    if (sessionId) void loadSummaryDraft(sessionId);
  }, [sessionId, refreshKey]);
  const get = () => (sessionId ? readSummaryDraft(sessionId) : NO_PARTS);
  return useSyncExternalStore(subscribeDrafts, get, get);
}

/** The summary as it goes to the task: each part, a blank line between. */
export function summaryDraftText(parts: readonly SummaryDraftPart[]): string {
  return parts.map((p) => p.text.trim()).filter(Boolean).join("\n\n");
}

/** Test seam. */
export function resetSummaryDraftsForTests(): void {
  drafts.clear();
  draftGen.clear();
  for (const l of [...draftListeners]) l();
}

/**
 * A SPECIALIST BREAKOUT'S SUMMARY, to the task. With a `label` (a specialist
 * breakout) it is shown as the task's summary draft (above; the server
 * already holds it, pending) and resolves true. A
 * plain draft with no label keeps Send back's two roads (breakouts.ts
 * `sendBackBreakout`): a mounted task chat takes the cancelable
 * `fused:composer-draft` event and appends it below what the reader typed;
 * unhandled, the persisted draft is read and `existing + text` is saved.
 * Resolves false when the saved draft could not be read or written.
 */
export async function deliverComposerDraft(
  detail: ComposerDraftDetail & { name?: string; breakoutId?: string; specialistId?: string; close?: string },
  save: (key: string, text: string, attachments: readonly DraftAttachment[]) => unknown = (k, t, a) =>
    saveChatDraft(k, t, a),
  load: (key: string) => Promise<ChatDraft | null | undefined> = fetchChatDraft,
): Promise<boolean> {
  if (detail.label) {
    addSummaryDraft(detail.sessionId, {
      text: detail.text,
      label: detail.label,
      name: detail.name || "",
      ...(detail.breakoutId ? { breakoutId: detail.breakoutId } : {}),
      ...(detail.specialistId ? { specialistId: detail.specialistId } : {}),
      ...(detail.close ? { close: detail.close } : {}),
      at: Date.now() / 1000,
    });
    // The close is already on the server: read the task's draft back from it.
    void loadSummaryDraft(detail.sessionId);
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
