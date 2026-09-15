// The landing's "Recent chats" list: one read, then a long-poll that re-reads
// it the moment anything in this folder changes — a chat started or resumed in a
// TERMINAL included, which no poke from this page could ever know about
// (T:18340-18394 `watchRecent`, T:18399-18470 `loadRecent`).
//
// WHAT IT READS IS `/api/tasks` NOW, not agent.py's `sessions` action
// (.claude-design/design.md §B: "Recent chats = TaskList"). The list draws the
// Tasks page's own row, so it needs the Tasks page's own model — a `Task`, with
// the status, the title source, the unread count and the message count the row
// is made of, all decided by the server once for every view. The `sessions`
// action answered a thinner shape that only this list could read, and pairing
// it with a row built for `Task` would have meant inventing the missing halves
// here, which is exactly the client-side model the tasks endpoint exists to
// retire.
//
// THE READ AND THE WATCH ARE NO LONGER THIS MODULE'S, since 2026-09-15. Both
// live in `shell/tasksPulse.subscribeListing`, one `GET /api/tasks` and one
// `/api/tasks/changes` long-poll for the whole DOCUMENT — because this function
// ran per SUBSCRIPTION, a ClaudeChat mounts per card on the Tasks wall, and
// twelve cards therefore held twelve 25-second sockets against a browser cap of
// six. What stayed here is the only part that was ever about one subscription:
// `changeIsHere`, the question of whether a change is worth repainting THIS
// folder's list, which is the same question it always was.
//
// Semantics the UI depends on (05-sched-live-lists-boot.md §B):
//   * `null` is NOT `0`. `null` means "we do not have rows yet" and draws the
//     skeleton; `[]` means "this folder has no chats" and hides the section
//     entirely (T:18458-18463). A FAILED read is also `[]` — count 0, no error
//     UI (T:18469-18477).
//   * the rows are the WHOLE listing, unfiltered. Which of them this pane shows
//     is a question about paths, and paths are the row layer's vocabulary
//     (`ui/list-rows.taskInPane`); this module's only use for the pane is
//     deciding whether a change is worth a re-read.
//   * the skeleton stands in for rows we do not have, never for rows already
//     up: a re-read over a drawn list repaints in place (T:18411). That is why
//     this only ever emits `null` ONCE, before the first read lands.
//   * only the NEWEST read may write, because reads overlap by design (the back
//     handler retries over a just-left run's spawn window) — the same seat idiom
//     `sendSeq`/`loopSeq` use (T:18402-18407, Bugbot PR #653).
import type { Task } from "@platform/lib/api";
import { refreshListing, subscribeListing } from "@shell/tasksPulse";
import type { ListingEnv } from "@shell/tasksPulse";

/** The long-poll's wait and its backoff, now the shared feed's
 *  (`shell/tasksPulse`) — re-exported because they are this module's published
 *  numbers and its tests assert on them. */
export { CHANGES_WAIT_S, CHANGES_BACKOFF_MS } from "@shell/tasksPulse";

/**
 * T:13066-13072 — TWO MORE LOOKS AFTER LANDING, and they are the difference
 * between a chat you just had being in this list and not (R3-1).
 *
 * A brand-new session becomes a row only once the CLI has written the first
 * lines of its transcript and the server's watcher has seen them — which is
 * SECONDS after the read this mount fires. T covers
 * exactly that window from its Back handler, and it is not a poll loop: two
 * looks a few seconds apart cover the write, and after that the list is what it
 * honestly is.
 *
 * GATED, exactly as T gates them, on having left a LIVE chat (`leftLive`,
 * T:13066). PR4 shipped them unconditionally on the theory that a native
 * landing had no handler to gate in — but the window these cover only exists
 * for a chat left mid-turn, and a cold landing boot therefore spent two extra
 * `sessions` reads for a transcript write that had already happened. The caller
 * says so through `coverWrite`, which ClaudeChat's Back path can answer because
 * it already knows `activeRun`/`sending`/`run` (P4-21).
 */
export const RECENT_RETRY_MS = [2500, 6000];

/**
 * The transport and its seams, owned by the shared feed now (`ListingEnv`) —
 * kept under this module's old name so the tests that hand one over, and the
 * callers that type one, need not learn where the loop moved to.
 *
 * Plus the one clock that is still THIS subscription's own: `RECENT_RETRY_MS`,
 * the two write-covering looks, which belong to an ARRIVAL at a landing and not
 * to the document's feed.
 */
export interface RecentEnv extends ListingEnv {
  /**
   * The retry schedule's clock (`RECENT_RETRY_MS`), and deliberately NOT
   * `sleep`: that one is the long-poll's backoff, awaited inside its loop, and
   * a test that resolves it instantly to drive the loop would spend the whole
   * retry schedule in the same microtask. Returns the canceller.
   */
  after?(ms: number, fn: () => void): () => void;
}
export type { FetchLike } from "@shell/tasksPulse";

function browserAfter(ms: number, fn: () => void): () => void {
  const id = setTimeout(fn, ms);
  return () => clearTimeout(id);
}

/** T:18384 `here` — a changed row concerns this folder when its project IS the
 *  target or is a folder above it. */
export function changeIsHere(project: unknown, file: string): boolean {
  return typeof project === "string" && !!project && (file === project || file.startsWith(project + "/"));
}

/**
 * Load the task listing once and keep it fresh until the returned function is
 * called. `cb(null)` fires first (skeleton), then `cb(rows)` for every read.
 *
 * `file` may be `null` — the landing has no target to ask about, so the list is
 * empty and nothing is watched. It is the WATCH's scope and not a filter on the
 * rows: see the header.
 */
export function subscribeTasks(
  file: string | null,
  cb: (rows: Task[] | null) => void,
  env?: RecentEnv,
  /** T's `leftLive` — see `RECENT_RETRY_MS`. Only a chat left MID-TURN has a
   *  transcript write to race, so only that landing pays for the two extra
   *  looks. */
  coverWrite = false,
  /**
   * ONE MORE THING THIS SUBSCRIPTION IS ABOUT, beyond the folder: a session id.
   *
   * The chat's HEADER follows the row for the conversation on screen
   * (`useSessionTask`), and that conversation need not live under the `file`
   * this mount was opened on — a task opened from the Tasks wall, or one whose
   * project is a folder above. Scoping the re-read to `file` ancestry alone
   * meant the header's ring never heard about its own session's changes.
   */
  watchId: string | null = null,
): () => void {
  let stopped = false;
  /** The task keys the list last painted — what a `gone` key has to be one of
   *  to concern this folder (a `gone` key carries no project of its own,
   *  T:18352-18354). */
  const ids = new Set<string>();

  cb(null); // the skeleton: rows we do not have yet

  if (!file) {
    cb([]);
    return () => {
      stopped = true;
    };
  }
  // Captured after the guard: `file` is a parameter, and TS drops the narrowing
  // inside the closures below.
  const target: string = file;

  // NO ORDER OF ITS OWN (.claude-design/design.md §B). The feed hands the
  // listing over exactly as `/api/tasks` sent it; the ONE sort the chat's list
  // spends is the Tasks page's own `sortForList`, and it is applied where the
  // rows are narrowed to a pane (`ui/useRecentTasks.ts`) so the two surfaces
  // cannot disagree about what is at the top of a list of the same rows.
  const paint = (rows: Task[]) => {
    ids.clear();
    for (const t of rows) ids.add(t.key);
    cb(rows);
  };

  // THE READ AND THE WATCH BOTH BELONG TO THE DOCUMENT NOW (`shell/tasksPulse`
  // subscribeListing). What is left here is the only part that was ever about
  // THIS subscription: whether a change concerns the folder it is watching.
  // A whole listing — the first read, a refresh, a `full`, a failure — always
  // does; a delta does when one of its rows is in this folder or one of its
  // `gone` keys is a row this list was showing.
  const off = subscribeListing((ev) => {
    if (stopped) return;
    if (ev.delta) {
      const mine =
        ev.delta.rows.some(
          (row) =>
            changeIsHere(row?.project, target) ||
            (!!watchId && (row?.key === watchId || row?.session_id === watchId)),
        ) || ev.delta.gone.some((k) => ids.has(k) || (!!watchId && k === watchId));
      if (!mine) return;
    }
    paint(ev.rows);
  }, env);

  /** The two extra looks that cover the CLI's transcript write — see
   *  `RECENT_RETRY_MS`. Both measured from the subscription, so a slow first
   *  read cannot push the schedule out behind itself. */
  const after = env?.after ?? browserAfter;
  const retries = coverWrite
    ? RECENT_RETRY_MS.map((wait) =>
        after(wait, () => {
          if (!stopped) refreshListing();
        }),
      )
    : [];

  return () => {
    stopped = true;
    off();
    // A pending timer outlives this closure otherwise, and six card mounts leak
    // six — the same rule the feed's own disposers exist for.
    for (const cancel of retries) cancel();
  };
}
