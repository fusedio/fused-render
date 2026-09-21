/**
 * THE GLOBAL MODEL / THINKING PAIR, in one place for every surface that shows
 * it — `model` and `effortLevel` in `~/.claude/settings.json`, the two keys the
 * app's own Claude settings page edits.
 *
 * There is ONE value and there are three editors of it: the settings page, the
 * New task card's Model / Thinking dropdowns, and the Explorer composer's pills
 * for a chat that has no session yet. Before this the composer kept its pick in
 * the ADDRESS BAR (`?model=`/`?effort=`) and nowhere else, so a reader who had
 * moved a pill saw Opus / high in the composer and Fable / low on the New task
 * card, in the same window, at the same moment (Akshil's screenshot,
 * 2026-09-21). A value two surfaces both claim to show must have exactly one
 * home, and the file is it.
 *
 * So this module is the only thing either surface talks to. It:
 *   * READS the pair (`GET /api/claude-sessions/defaults`), de-duplicating
 *     concurrent reads — a wall of mounts asking at once is one request;
 *   * WRITES it (`PUT`), OPTIMISTICALLY first so the control that was just
 *     pressed paints the new value in the same tick;
 *   * ANNOUNCES every change to the subscribers in this document AND, through
 *     one `localStorage` write, to every other tab of the app — the same
 *     mechanism and the same reason as `apps/claude/feature-flag.ts`'s
 *     `publishProjectQueueEnabled`: a surface that was already open must not go
 *     on showing the answer it read at mount.
 *
 * A chat that HAS a session id is NOT one of these editors. Its pills write
 * that conversation's own record (`recordChatSettings`), because what a running
 * conversation is set to is a fact about that conversation and not about this
 * machine's next new chat.
 */
import { getTaskDefaults, putTaskDefaults } from "./api";

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
    .then((d) => announce({ model: d?.model || "", effort: d?.effort || "" }, false))
    .catch(() => current ?? EMPTY)
    .finally(() => {
      reading = null;
    });
  return reading;
}

/**
 * Move one or both halves of the global pair.
 *
 * OPTIMISTIC, then corrected. The control that was pressed has to say the new
 * thing on this render — a pill that waits for a round trip to admit what was
 * just clicked reads as a dropped click — so the new pair is announced before
 * the request goes out and re-announced with whatever the file actually says
 * once it answers.
 *
 * A FAILED write is LEFT ALONE, not rolled back — the same doctrine as the
 * composer's own `recordChatSettings` (ui/composer-defaults): nothing about the
 * reader's gesture has failed, and snapping a dropdown back to a value they
 * just moved away from reads as the app fighting them. The next open re-reads
 * the file and settles it.
 */
export function setClaudeDefaults(
  patch: Partial<ClaudeDefaults>,
): Promise<ClaudeDefaults> {
  const optimistic: ClaudeDefaults = {
    model: patch.model ?? current?.model ?? "",
    effort: patch.effort ?? current?.effort ?? "",
  };
  announce(optimistic, true);
  return putTaskDefaults(patch)
    .then((d) => announce({ model: d?.model || "", effort: d?.effort || "" }, true))
    .catch(() => current ?? optimistic);
}

/** One `storage` event, as the listener below sees it. Exported so the rule can
 *  be exercised where the test DOM has no `StorageEvent`. */
export function applyClaudeDefaultsBroadcast(
  key: string | null,
  newValue: string | null,
): void {
  if (key !== CLAUDE_DEFAULTS_BROADCAST_KEY || !newValue) return;
  try {
    const d = JSON.parse(newValue) as Partial<ClaudeDefaults>;
    announce({ model: d.model || "", effort: d.effort || "" }, false);
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
