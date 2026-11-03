// BREAKOUTS — the shell's open research-only side chats (docs: the Breakout
// plan, .claude/handoffs/claude-chat-breakout-plan.md).
//
// A MODULE STORE, not React state: a breakout is requested from any chat mount
// (a message's Breakout button, the explorer pane, the rail card) and drawn by
// one component the shell mounts once (`BreakoutCards`). Same shape as
// `task-peek-store`: pure rules at the top, a thin `useSyncExternalStore`
// layer under them.
import { useSyncExternalStore } from "react";
import { chatDraftKey, saveChatDraft } from "./drafts";

/**
 * A request to open a breakout (a research-only side chat forked from a chat),
 * handed from a chat's Breakout buttons to this store. `forkAt` is the user
 * message's uuid; absent means "from the latest message". Lives in platform
 * (every host may import it); `controller-api` re-exports it for the chat.
 */
export interface BreakoutRequest {
  sessionId: string;
  file: string;
  forkAt?: string;
  /** Optional dedupe key for the card store (e.g. the turn key). */
  forkKey?: string;
}

/** The slice of a chat transcript `buildHandoffDraft` reads (the chat's `Turn`
 *  satisfies it structurally; platform cannot import the chat's own types). */
export type HandoffTurn =
  | { role: "user"; key: string; text: string }
  | {
      role: "assistant";
      key: string;
      text: string;
      segments?: readonly { kind: string; name?: string; input?: Record<string, unknown> }[];
    }
  | { role: string; key: string; text?: string };

export interface BreakoutCard {
  id: string;
  /** The PARENT chat's session: what the card forks and what Send back fills. */
  parentSessionId: string;
  file: string;
  /** The parent's user message the card forks at; absent = the tip. */
  forkAt?: string;
  /** Dedupe key (the clicked turn's key) so a second press finds the card. */
  forkKey?: string;
}

/** The window event Send back raises; `ClaudeChat` listens for its own chat. */
export const COMPOSER_DRAFT_EVENT = "fused:composer-draft";

export interface ComposerDraftDetail {
  sessionId: string;
  file: string;
  text: string;
}

let cards: readonly BreakoutCard[] = [];
let seq = 0;
const listeners = new Set<() => void>();
const emit = (next: readonly BreakoutCard[]): void => {
  cards = next;
  for (const l of [...listeners]) l();
};

export const getBreakouts = (): readonly BreakoutCard[] => cards;
export const subscribeBreakouts = (fn: () => void): (() => void) => {
  listeners.add(fn);
  return () => void listeners.delete(fn);
};
export const useBreakouts = (): readonly BreakoutCard[] =>
  useSyncExternalStore(subscribeBreakouts, getBreakouts, getBreakouts);

/** Open a card for `req` and return its id. The same message of the same
 *  parent answers with the card already open rather than a second one. */
export function openBreakout(req: BreakoutRequest): string {
  if (req.forkKey) {
    const same = cards.find(
      (c) => c.parentSessionId === req.sessionId && c.file === req.file && c.forkKey === req.forkKey,
    );
    if (same) return same.id;
  }
  seq += 1;
  const card: BreakoutCard = {
    id: `breakout-${Date.now().toString(36)}-${seq}`,
    parentSessionId: req.sessionId,
    file: req.file,
    ...(req.forkAt ? { forkAt: req.forkAt } : {}),
    ...(req.forkKey ? { forkKey: req.forkKey } : {}),
  };
  emit([...cards, card]);
  return card.id;
}

export function closeBreakout(id: string): void {
  if (cards.some((c) => c.id === id)) emit(cards.filter((c) => c.id !== id));
}

/** Test seam. */
export function resetBreakouts(): void {
  seq = 0;
  emit([]);
}

/**
 * SEND BACK: hand `text` to the parent's composer, then drop the card.
 *
 * Two roads, because the parent may be mounted anywhere (side pane, rail card,
 * its own page) or nowhere: the window event reaches a mounted composer now,
 * and the persisted draft is what a composer that mounts LATER seeds from.
 * `save` is a seam for tests.
 */
export function sendBackBreakout(
  id: string,
  text: string,
  save: (key: string, text: string) => unknown = (k, t) => void saveChatDraft(k, t),
): void {
  const card = cards.find((c) => c.id === id);
  if (!card) return;
  const detail: ComposerDraftDetail = { sessionId: card.parentSessionId, file: card.file, text };
  window.dispatchEvent(new CustomEvent(COMPOSER_DRAFT_EVENT, { detail }));
  save(chatDraftKey(card.parentSessionId, card.file), text);
  closeBreakout(id);
}

const HANDOFF_ANY = /\/\.claude\/handoffs\/[^/]+\.md$/;
const WRITE_TOOLS = new Set(["Write", "Edit", "MultiEdit"]);

/** The last handoff markdown the breakout wrote (a Write/Edit tool_use under
 *  `handoffDir`, or under any `.claude/handoffs/` when the dir is unknown). */
function lastHandoffPath(turns: readonly HandoffTurn[], handoffDir?: string): string {
  let found = "";
  for (const t of turns) {
    if (t.role !== "assistant") continue;
    const segs = "segments" in t ? t.segments : undefined;
    for (const seg of segs ?? []) {
      if (seg.kind !== "tool" || !seg.name || !WRITE_TOOLS.has(seg.name)) continue;
      const p = seg.input?.["file_path"];
      if (typeof p !== "string" || !p.endsWith(".md")) continue;
      const ok = handoffDir ? p.startsWith(handoffDir.replace(/\/+$/, "") + "/") : HANDOFF_ANY.test(p);
      if (ok) found = p;
    }
  }
  return found;
}

/** The composer draft Send back files: the breakout's last assistant text, and
 *  a `Handoff file:` line when it wrote one. "" for a transcript with neither. */
export function buildHandoffDraft(turns: readonly HandoffTurn[], handoffDir?: string): string {
  let last = "";
  for (const t of turns) if (t.role === "assistant" && t.text?.trim()) last = t.text.trim();
  const file = lastHandoffPath(turns, handoffDir);
  return [last, file ? `Handoff file: ${file}` : ""].filter(Boolean).join("\n\n");
}
