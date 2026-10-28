/**
 * THE GLOBAL MODEL / THINKING PAIR, in one place for every surface that shows
 * it — `model` and `effortLevel` in `~/.claude/settings.json`, the two keys the
 * app's own Claude settings page edits.
 *
 * ONE EDITOR, MANY READERS (Akshil, 2026-10-28: "from fused-render only
 * settings page should be able to edit global claude code config"). The New
 * task card, the New app composer (Apps page, Home's Build widget, the
 * sidebar's New app dialog, onboarding) and a session-less chat all OPEN on
 * this pair and follow it while untouched, but a pick on any of them is that
 * task's or app's own and is never written back. The New app composer used to
 * write its picks here (#1479), which moved every later Claude run on the
 * machine from a dropdown that read as local.
 *
 * So this module only:
 *   * READS the pair (`GET /api/claude-sessions/defaults`), de-duplicating
 *     concurrent reads — a wall of mounts asking at once is one request;
 *   * RE-READS it when the settings page has just saved one of the two keys
 *     (`refreshClaudeDefaults`), so a card that has not chosen follows;
 *   * ANNOUNCES every change to the subscribers in this document AND, through
 *     one `localStorage` write, to every other tab of the app — the same
 *     mechanism and the same reason as `apps/claude/feature-flag.ts`'s
 *     `publishProjectQueueEnabled`: a surface that was already open must not go
 *     on showing the answer it read at mount.
 *
 * A chat that HAS a session id never reads this. Its pills write that
 * conversation's own record (`recordChatSettings`), because what a running
 * conversation is set to is a fact about that conversation and not about this
 * machine's next new chat.
 */
import { getTaskDefaults } from "./api";

export interface ClaudeDefaults {
  /** A short family name ("fable" | "opus" | "sonnet" | "haiku"), or "" when
   *  the file sets nothing this build's pickers can say. "" is an answer: it
   *  leaves each surface's own fallback speaking, which is what the CLI would
   *  have resolved anyway. */
  model: string;
  effort: string;
}

const EMPTY: ClaudeDefaults = { model: "", effort: "" };

/** The last answer this document has — `null` until the first read lands, which
 *  is why it is not simply `EMPTY`: "unknown" and "the file says nothing" are
 *  different, and a pill must not paint the second while it is in the first. */
let current: ClaudeDefaults | null = null;
let reading: Promise<ClaudeDefaults> | null = null;
function fromServer(d: { model?: unknown; effort?: unknown } | null | undefined): ClaudeDefaults {
  return {
    model: typeof d?.model === "string" ? d.model : "",
    effort: typeof d?.effort === "string" ? d.effort : "",
  };
}
const listeners = new Set<(next: ClaudeDefaults) => void>();

/** The `localStorage` key a write announces itself on, so every OTHER tab hears
 *  it through the `storage` event instead of keeping what it read at load. */
export const CLAUDE_DEFAULTS_BROADCAST_KEY = "fused-render:claude-defaults";

/** What this document last heard, or `null` if it has not asked yet. Synchronous
 *  on purpose: a surface mounting after another one has already read paints the
 *  right value on its FIRST render rather than flipping a tick later. */
export function getClaudeDefaults(): ClaudeDefaults | null {
  return current;
}

export function subscribeClaudeDefaults(
  cb: (next: ClaudeDefaults) => void,
): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

function same(a: ClaudeDefaults | null, b: ClaudeDefaults): boolean {
  return !!a && a.model === b.model && a.effort === b.effort;
}

/** Remember and tell. `broadcast` is false for a value that ARRIVED from
 *  somewhere else (a read, or another tab's announcement): re-announcing it
 *  would bounce the same pair between tabs for ever. */
function announce(next: ClaudeDefaults, broadcast: boolean): ClaudeDefaults {
  const changed = !same(current, next);
  current = next;
  if (changed) for (const cb of [...listeners]) cb(next);
  if (broadcast) {
    try {
      localStorage.setItem(
        CLAUDE_DEFAULTS_BROADCAST_KEY,
        JSON.stringify({ ...next, at: Date.now() }),
      );
    } catch {
      // Storage may be unavailable (private window, blocked site data). The
      // other tabs simply keep their own value until they read again; nothing
      // about THIS tab's write has failed.
    }
  }
  return next;
}

/** Ask the server. Concurrent callers share one request — six task cards and a
 *  composer mounting together must not be seven round trips. A failed read
 *  resolves to the last known pair (or the empty one), never rejects: every
 *  caller of this treats "no answer" and "no opinion" the same way, and an
 *  unhandled rejection in a mount effect is a worse bug than a stale pill. */
export function readClaudeDefaults(): Promise<ClaudeDefaults> {
  if (reading) return reading;
  reading = getTaskDefaults()
    .then((d) => announce(fromServer(d), false))
    .catch(() => current ?? EMPTY)
    .finally(() => {
      reading = null;
    });
  return reading;
}

/** Re-read the pair after the Claude settings page saved `model` or
 *  `effortLevel`, and tell every surface — this tab's subscribers and, through
 *  the broadcast, every other tab's. A FRESH request rather than a share of one
 *  already out: a read that departed before the save would answer with the
 *  file as it was. A failed read keeps what was known. */
export function refreshClaudeDefaults(): Promise<ClaudeDefaults> {
  return getTaskDefaults()
    .then((d) => announce(fromServer(d), true))
    .catch(() => current ?? EMPTY);
}

/** One `storage` event, as the listener below sees it. Exported so the rule can
 *  be exercised where the test DOM has no `StorageEvent`. */
export function applyClaudeDefaultsBroadcast(
  key: string | null,
  newValue: string | null,
): void {
  if (key !== CLAUDE_DEFAULTS_BROADCAST_KEY || !newValue) return;
  try {
    announce(fromServer(JSON.parse(newValue) as Partial<ClaudeDefaults>), false);
  } catch {
    // A malformed broadcast is ignored; the next open re-reads.
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (ev) =>
    applyClaudeDefaultsBroadcast(ev.key, ev.newValue),
  );
}

/** TEST SEAM. `bun test` shares one `globalThis` across suites, so the
 *  module-level pair outlives the suite that set it. */
export function resetClaudeDefaultsForTests(): void {
  current = null;
  reading = null;
  listeners.clear();
}
