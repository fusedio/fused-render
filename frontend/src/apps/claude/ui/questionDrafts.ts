// A question card's half-made answer, kept across the card's unmount.
//
// The card's picks, typed "Other" text, skips and open tab lived only in its
// own React state, so walking to another task and back — which unmounts the
// chat — threw them away and the reader had to answer every question again
// (Akshil, 2026-11-26). This is the one place that survives that: a map keyed
// by the request's id, mirrored into localStorage so a reload (or the same
// chat drawn in another document of this origin) finds it too.
//
// Pure and React-free; every read and write is guarded, because storage can be
// missing (tests), full, or blocked, and the card must work without it.

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

const STORAGE_KEY = "fused-render.claude.question-drafts.v1";
/** Newest kept; an unanswered card that expired never clears its own entry. */
const MAX_DRAFTS = 40;

let mem: Map<string, QuestionDraft> | null = null;

function isDraft(v: unknown): v is QuestionDraft {
  if (!v || typeof v !== "object") return false;
  const d = v as Partial<QuestionDraft>;
  const n = d.n;
  return (
    typeof n === "number" &&
    n > 0 &&
    Array.isArray(d.picked) &&
    d.picked.length === n &&
    d.picked.every((p) => Array.isArray(p) && p.every((l) => typeof l === "string")) &&
    Array.isArray(d.otherOpen) &&
    d.otherOpen.length === n &&
    Array.isArray(d.otherText) &&
    d.otherText.length === n &&
    d.otherText.every((t) => typeof t === "string") &&
    Array.isArray(d.skipped) &&
    d.skipped.length === n &&
    typeof d.active === "number"
  );
}

function storage(): Storage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/** The drafts, RE-READ from storage on every call when there is storage: the
 *  same chat can be open in another document of this origin, and writing back
 *  a map cached at load would drop whatever that document saved since. The
 *  in-memory map is only the fallback for a runner with no storage. */
function load(): Map<string, QuestionDraft> {
  const s = storage();
  if (mem && !s) return mem;
  mem = new Map();
  try {
    const raw = s?.getItem(STORAGE_KEY);
    const list: unknown = raw ? JSON.parse(raw) : [];
    if (Array.isArray(list)) {
      for (const entry of list) {
        if (Array.isArray(entry) && typeof entry[0] === "string" && isDraft(entry[1])) {
          mem.set(entry[0], entry[1]);
        }
      }
    }
  } catch {
    // A corrupt entry is a lost draft, not a broken card.
  }
  return mem;
}

function save(map: Map<string, QuestionDraft>): void {
  while (map.size > MAX_DRAFTS) {
    const oldest = map.keys().next().value;
    if (oldest === undefined) break;
    map.delete(oldest);
  }
  try {
    const s = storage();
    if (!s) return;
    if (map.size) s.setItem(STORAGE_KEY, JSON.stringify([...map]));
    else s.removeItem(STORAGE_KEY);
  } catch {
    // Quota or a blocked store: the in-memory copy still covers this page.
  }
}

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
  const d = load().get(id);
  if (!d || d.n !== n) return null;
  return { ...d, active: Math.min(Math.max(0, d.active), n - 1) };
}

export function writeQuestionDraft(id: string, draft: QuestionDraft): void {
  if (!id) return;
  const map = load();
  // Delete first so a write moves the entry to the newest end of the map.
  map.delete(id);
  if (!isEmpty(draft)) map.set(id, draft);
  save(map);
}

export function clearQuestionDraft(id: string): void {
  const map = load();
  if (!map.delete(id)) return;
  save(map);
}

/** Test seam: the map is module-level and bun shares one process per run. */
export function resetQuestionDraftsForTests(): void {
  mem = null;
  try {
    storage()?.removeItem(STORAGE_KEY);
  } catch {
    // No storage in this runner.
  }
}
