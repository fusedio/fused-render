// BREAKOUTS — the shell's open research-only side chats, each a fresh session
// seeded with messages picked from a parent chat.
//
// A MODULE STORE, not React state: a breakout is requested from any chat mount
// (a message's Breakout button, the explorer pane, the rail card) and drawn by
// one component the shell mounts once (`BreakoutCards`). Same shape as
// `task-peek-store`: pure rules at the top, a thin `useSyncExternalStore`
// layer under them.
import { useSyncExternalStore } from "react";
import { IS_EMBED } from "./router";
import type { HandoffTurn } from "./breakout-context";
import { chatDraftKey, fetchChatDraft, saveChatDraft, type ChatDraft, type DraftAttachment } from "./drafts";

export { BREAKOUT_CTX_TAG } from "./wire-tags";
export {
  buildBreakoutContext,
  exchangeKeys,
  liveKeys,
  formatBreakoutContext,
  type HandoffTurn,
} from "./breakout-context";

/** The parent messages a breakout opens with: the `<breakout-context>` wire
 *  block its first send carries, how many messages it quotes, and the parent
 *  chat's title when known. */
export interface BreakoutContext {
  block: string;
  count: number;
  title?: string;
}

/**
 * A request to open a breakout (a research-only side chat seeded with messages
 * from a chat), handed from a chat's Breakout buttons to this store. Lives in
 * platform (every host may import it); `controller-api` re-exports it for the
 * chat.
 */
export interface BreakoutRequest {
  sessionId: string;
  file: string;
  context: BreakoutContext;
  /** Dedupe key for the card store: the selected turn keys, sorted and joined. */
  forkKey?: string;
}

export interface BreakoutCard {
  id: string;
  /** The PARENT chat's session: what Send back fills. */
  parentSessionId: string;
  file: string;
  context: BreakoutContext;
  /** Dedupe key so a second press of the same selection finds the card. */
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

/** Open a card for `req` and return its id. The same selection of the same
 *  parent answers with the card already open (raised to the top)
 *  rather than a second one. */
export function openBreakout(req: BreakoutRequest): string {
  if (req.forkKey) {
    const same = cards.find(
      (c) => c.parentSessionId === req.sessionId && c.file === req.file && c.forkKey === req.forkKey,
    );
    if (same) {
      // Raise it: the list order is the stacking order, so the last card is on top.
      if (cards[cards.length - 1] !== same) emit([...cards.filter((c) => c !== same), same]);
      return same.id;
    }
  }
  seq += 1;
  const card: BreakoutCard = {
    id: `breakout-${Date.now().toString(36)}-${seq}`,
    parentSessionId: req.sessionId,
    file: req.file,
    context: req.context,
    ...(req.forkKey ? { forkKey: req.forkKey } : {}),
  };
  emit([...cards, card]);
  return card.id;
}

/** What a host passes as `onBreakout`: `openBreakout`, or nothing inside an
 *  embedded document, where the shell mounts no `BreakoutCards` to draw the
 *  card (so no Breakout seat should exist there). */
export function breakoutOpener(embed: boolean = IS_EMBED): typeof openBreakout | undefined {
  return embed ? undefined : openBreakout;
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
 * its own page) or nowhere. The window event is CANCELABLE: a mounted composer
 * appends the text and calls `preventDefault()`, and then its own syncer owns
 * the persisted draft, so this does not touch it (a write here would clobber
 * what the reader typed since). Unhandled means no parent is mounted: read the
 * saved draft and file `existing + text` so nothing the reader left is lost,
 * and the composer that mounts LATER seeds from it.
 *
 * Resolves `true` once handed off (the card is closed), `false` when the draft
 * could not be read or saved (the card stays so the text is not lost).
 * `save` and `load` are seams for tests.
 */
export async function sendBackBreakout(
  id: string,
  text: string,
  save: (key: string, text: string, attachments: readonly DraftAttachment[]) => unknown = (k, t, a) =>
    saveChatDraft(k, t, a),
  load: (key: string) => Promise<ChatDraft | null | undefined> = fetchChatDraft,
): Promise<boolean> {
  const card = cards.find((c) => c.id === id);
  if (!card) return false;
  const detail: ComposerDraftDetail = { sessionId: card.parentSessionId, file: card.file, text };
  const handled = !window.dispatchEvent(new CustomEvent(COMPOSER_DRAFT_EVENT, { detail, cancelable: true }));
  if (!handled) {
    const key = chatDraftKey(card.parentSessionId, card.file);
    // `undefined` = the read failed: writing now could erase a saved draft.
    const existing = await load(key);
    if (existing === undefined) return false;
    const prior = existing?.text ?? "";
    const merged = prior.trim() ? `${prior}\n\n${text}` : text;
    const res = (await save(key, merged, existing?.attachments ?? [])) as { ok?: boolean } | undefined;
    if (res && res.ok === false) return false;
  }
  closeBreakout(id);
  return true;
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
      // A refused or failed write left no file behind.
      if (seg.status === "error") continue;
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
