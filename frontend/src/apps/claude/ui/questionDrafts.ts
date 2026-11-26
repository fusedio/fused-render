// A question card's half-made answer, kept across the card's unmount.
//
// The card's picks, typed "Other" text, skips and open tab lived only in its
// own React state, so walking to another task and back — which unmounts the
// chat — threw them away and the reader had to answer every question again
// (Akshil, 2026-11-26). This is the one place that survives that: a map keyed
// by the request's id. Pure and React-free.

export interface QuestionDraft {
  /** How many questions the draft was made for — a row whose question list
   *  changed length is a different card, and its draft does not apply. */
  n: number;
  picked: string[][];
  otherOpen: boolean[];
  otherText: string[];
  skipped: boolean[];
  active: number;
}

/** Newest kept; an unanswered card that expired never clears its own entry. */
const MAX_DRAFTS = 40;

/** In memory on purpose: per D1386 (tests/test_browser_storage_allowlist.py)
 *  anything the reader would miss across a relaunch belongs behind an API, not
 *  in browser storage. The ask is "leave the task and come back", which never
 *  leaves this document, so a module map covers it. */
const drafts = new Map<string, QuestionDraft>();

/** Nothing chosen, typed, skipped or moved to — not worth keeping. */
function isEmpty(d: QuestionDraft): boolean {
  return (
    d.active === 0 &&
    d.picked.every((p) => !p.length) &&
    d.otherOpen.every((o) => !o) &&
    d.otherText.every((t) => !t) &&
    d.skipped.every((s) => !s)
  );
}

/** The saved draft for request `id`, or null when there is none for a card of
 *  `n` questions. */
export function readQuestionDraft(id: string, n: number): QuestionDraft | null {
  if (!id) return null;
  const d = drafts.get(id);
  if (!d || d.n !== n) return null;
  return { ...d, active: Math.min(Math.max(0, d.active), n - 1) };
}

export function writeQuestionDraft(id: string, draft: QuestionDraft): void {
  if (!id) return;
  // Delete first so a write moves the entry to the newest end of the map.
  drafts.delete(id);
  if (!isEmpty(draft)) drafts.set(id, draft);
  while (drafts.size > MAX_DRAFTS) {
    const oldest = drafts.keys().next().value;
    if (oldest === undefined) break;
    drafts.delete(oldest);
  }
}

export function clearQuestionDraft(id: string): void {
  drafts.delete(id);
}

/** Test seam: the map is module-level and bun shares one process per run. */
export function resetQuestionDraftsForTests(): void {
  drafts.clear();
}
