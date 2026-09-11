// The landing's "Recent chats" list: one read, then a long-poll that re-reads
// it the moment anything in this folder changes — a chat started or resumed in a
// TERMINAL included, which no poke from this page could ever know about
// (T:18340-18394 `watchRecent`, T:18399-18470 `loadRecent`).
//
// Semantics the UI depends on (05-sched-live-lists-boot.md §B):
//   * `null` is NOT `0`. `null` means "we do not have rows yet" and draws the
//     skeleton; `[]` means "this folder has no chats" and hides the section
//     entirely (T:18458-18463). A FAILED read is also `[]` — count 0, no error
//     UI (T:18469-18477).
//   * the skeleton stands in for rows we do not have, never for rows already
//     up: a re-read over a drawn list repaints in place (T:18411). That is why
//     this only ever emits `null` ONCE, before the first read lands.
//   * only the NEWEST read may write, because reads overlap by design (the back
//     handler retries over a just-left run's spawn window) — the same seat idiom
//     `sendSeq`/`loopSeq` use (T:18402-18407, Bugbot PR #653).
import { TASKS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { runAgent } from "./agent";
import type { ErrorOnly, SessionRow, SessionsResponse } from "./types";

/** T:18367 — the long-poll's own wait, in seconds. */
export const CHANGES_WAIT_S = 25;
/** T:18379 — how long a failed change-poll waits before trying again. */
export const CHANGES_BACKOFF_MS = 3000;

/**
 * T:13066-13072 — TWO MORE LOOKS AFTER LANDING, and they are the difference
 * between a chat you just had being in this list and not (R3-1).
 *
 * The list is the transcripts in the cwd's project dir (agent.py `_sessions`),
 * and a brand-new session's transcript only appears once the CLI has written
 * its first rows — which is SECONDS after the read this mount fires. T covers
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

/** ClaudeChat's `CHAT_ACTIVITY_KEY`, restated rather than imported: the chat
 *  imports this module, and a list cannot be made to depend on the view that
 *  draws it to know the name of a localStorage key. One string, in three places
 *  (see `ui/Kebab.tsx`), unchanged since T:16435. */
const CHAT_ACTIVITY_KEY = "fused-render:chat-activity";

/** GET /api/tasks/changes' answer (fused_render/tasks_watch.py). */
interface ChangesResponse {
  generation: number;
  rows?: { project?: unknown }[];
  gone?: string[];
  full?: boolean;
}

/** Only what the change-poll needs off `fetch`, so a bun test can hand over a
 *  three-line stub instead of the whole DOM signature. */
export type FetchLike = (
  url: string,
  init?: { signal?: AbortSignal },
) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

/** The browser pieces this reaches for — injectable for bun tests. */
export interface RecentEnv {
  fetch: FetchLike;
  /** Hidden tabs sit the long-poll out (T:18369-18372). */
  hidden(): boolean;
  /** Resolves on the next `visibilitychange`. The returned disposer must REMOVE
   *  the listener: a `{once:true}` listener that never fires holds the whole
   *  closure — `ids`, `cb`, the loop — alive, and six card mounts leak six. */
  whenVisible(): { promise: Promise<void>; cancel(): void };
  sleep(ms: number): Promise<void>;
  run?: typeof runAgent;
  /**
   * THE PUSH SIDE OF THE LIST, alongside the long-poll's pull side: subscribe
   * `fn` to every signal that says a chat just moved, and return the disposer.
   *
   * The long-poll alone was not enough, and the gap has a shape: the poll's
   * first call is a HANDSHAKE that only learns the current generation, so every
   * change made before this subscription existed is already spent — and a run
   * started and finished while the reader was inside the chat is exactly that.
   * On landing there is then one read, racing the CLI's own transcript write,
   * and if it loses nothing ever comes back to fix it (R3-1).
   *
   * `RECENT_RETRY_MS` covers the write; these cover everything else. Three
   * signals, all pokes and no payloads, so they land on one handler:
   *   * `fused-render:tasks-changed` — THIS document's run controller, which
   *     announces at the start and end of every turn (`noteChatActivity`);
   *   * `storage` on `CHAT_ACTIVITY_KEY` — every OTHER document's chat saying
   *     the same thing (T:16435; `storage` never fires in the writer);
   *   * `focus` — coming back to a window that was away while something else
   *     (a terminal, another window) had the conversation.
   *
   */
  pokes?(fn: () => void): () => void;
  /**
   * The retry schedule's clock (`RECENT_RETRY_MS`), and deliberately NOT
   * `sleep`: that one is the long-poll's backoff, awaited inside its loop, and
   * a test that resolves it instantly to drive the loop would spend the whole
   * retry schedule in the same microtask. Returns the canceller.
   */
  after?(ms: number, fn: () => void): () => void;
}

function browserAfter(ms: number, fn: () => void): () => void {
  const id = setTimeout(fn, ms);
  return () => clearTimeout(id);
}

function browserEnv(): RecentEnv {
  return {
    fetch: (url, init) => fetch(url, init),
    hidden: () => document.hidden,
    whenVisible: () => {
      let fire: () => void = () => {};
      const promise = new Promise<void>((r) => {
        fire = r;
      });
      document.addEventListener("visibilitychange", fire, { once: true });
      return {
        promise,
        cancel: () => {
          document.removeEventListener("visibilitychange", fire);
          fire(); // let the awaiting loop wake and see `stopped`
        },
      };
    },
    sleep: (ms) => new Promise<void>((r) => setTimeout(r, ms)),
    pokes: (fn) => {
      const onStorage = (ev: StorageEvent) => {
        // A `null` key is a `clear()`, which may well have taken the stamp with
        // it — treat it as news rather than working out whether it was ours.
        if (!ev.key || ev.key === CHAT_ACTIVITY_KEY) fn();
      };
      window.addEventListener(TASKS_CHANGED_EVENT, fn);
      window.addEventListener("storage", onStorage);
      window.addEventListener("focus", fn);
      return () => {
        window.removeEventListener(TASKS_CHANGED_EVENT, fn);
        window.removeEventListener("storage", onStorage);
        window.removeEventListener("focus", fn);
      };
    },
  };
}

/** T:18384 `here` — a changed row concerns this folder when its project IS the
 *  target or is a folder above it. */
export function changeIsHere(project: unknown, file: string): boolean {
  return typeof project === "string" && !!project && (file === project || file.startsWith(project + "/"));
}

/**
 * Load the recent list once and keep it fresh until the returned function is
 * called. `cb(null)` fires first (skeleton), then `cb(rows)` for every read.
 *
 * `file` may be `null` — the landing has no target to ask about, so the list is
 * empty and nothing is watched (agent.py refuses `sessions` without a file).
 */
export function subscribeRecent(
  agentDir: string,
  file: string | null,
  cb: (rows: SessionRow[] | null) => void,
  env: RecentEnv = browserEnv(),
  /** T's `leftLive` — see `RECENT_RETRY_MS`. Only a chat left MID-TURN has a
   *  transcript write to race, so only that landing pays for the two extra
   *  looks. */
  coverWrite = false,
): () => void {
  const run = env.run || runAgent;
  let stopped = false;
  /** The session ids the list last painted — what a `gone` key has to be one of
   *  to concern this folder (a `gone` key carries no project of its own,
   *  T:18352-18354). */
  const ids = new Set<string>();
  let abort: AbortController | null = null;
  /** The visibility wait a hidden tab is parked on, so the teardown can end it
   *  rather than leaving the loop (and everything it closes over) alive. */
  let waking: { cancel(): void } | null = null;
  let seat = 0;

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

  // NOT ABORTABLE, deliberately: `{key: null}` opts this read out of the
  // supersede channel because two folders' lists must not cancel each other
  // (T:16620), and the `stopped`/`seat` guards below already make a late result
  // harmless — an aborted or superseded read never paints. A quick
  // enter-and-back therefore spends one `sessions` read that nothing will paint,
  // and that is the whole cost: accepted, because the read is cheap and the
  // alternative is threading a signal through a transport whose whole point
  // here is that these reads do NOT cancel each other.
  const load = async () => {
    const mine = ++seat;
    let rows: SessionRow[] = [];
    try {
      const res = (await run(agentDir, "sessions", { file: target }, { key: null })) as
        | SessionsResponse
        | ErrorOnly;
      const failed = (res as ErrorOnly).error;
      if (failed) throw new Error(failed);
      const list = (res as SessionsResponse).sessions;
      rows = (Array.isArray(list) ? list : []).filter((s): s is SessionRow => !!s && !!s.id);
    } catch {
      // No error UI, by design: a folder whose sessions cannot be read reads as
      // a folder with no chats (T:18469-18477).
      rows = [];
    }
    if (stopped || seat !== mine) return; // a newer read owns the list now
    ids.clear();
    for (const s of rows) ids.add(s.id);
    cb(rows);
  };

  const watch = async () => {
    let gen = -1;
    while (!stopped) {
      if (env.hidden()) {
        const wait = env.whenVisible();
        waking = wait;
        await wait.promise;
        waking = null;
        continue;
      }
      const ctl = new AbortController();
      abort = ctl;
      let r: ChangesResponse;
      try {
        const res = await env.fetch(`/api/tasks/changes?since=${gen}&wait=${CHANGES_WAIT_S}`, {
          signal: ctl.signal,
        });
        if (!res.ok) throw new Error(String(res.status));
        r = (await res.json()) as ChangesResponse;
      } catch {
        if (ctl.signal.aborted) return;
        await env.sleep(CHANGES_BACKOFF_MS);
        continue;
      } finally {
        if (abort === ctl) abort = null;
      }
      if (stopped) return;
      // The first call is a handshake that only learns the current generation
      // (T:18387-18389).
      const handshake = gen < 0;
      gen = r.generation;
      if (handshake) continue;
      const changed = r.rows || [];
      const isMine =
        changed.some((row) => changeIsHere(row?.project, target)) ||
        (r.gone || []).some((k) => ids.has(k));
      if (r.full || isMine) void load();
    }
  };

  void load();
  void watch();

  /** The two extra looks that cover the CLI's transcript write — see
   *  `RECENT_RETRY_MS`. Both measured from the subscription, so a slow first
   *  read cannot push the schedule out behind itself. */
  const after = env.after ?? browserAfter;
  const retries = coverWrite
    ? RECENT_RETRY_MS.map((wait) =>
        after(wait, () => {
          if (!stopped) void load();
        }),
      )
    : [];

  // Every "something moved" signal there is, on one handler — a poke, never a
  // payload, so the answer to all three is the same read. `load`'s seat idiom
  // is what makes a burst of them safe: they overlap, and only the newest may
  // paint.
  const unpoke = env.pokes?.(() => {
    if (!stopped) void load();
  });

  /** T:18358-18361 — abort the in-flight long-poll rather than letting it run
   *  out its 25 s, so a quick enter-and-back never has two loops going (Bugbot
   *  #892). */
  return () => {
    stopped = true;
    if (abort) {
      abort.abort();
      abort = null;
    }
    // A hidden tab's `watch` is parked on a promise nothing else will resolve.
    waking?.cancel();
    waking = null;
    // Listeners on `window` and a pending timer both outlive this closure
    // otherwise, and six card mounts leak six — the same rule `whenVisible`'s
    // disposer exists for.
    unpoke?.();
    for (const cancel of retries) cancel();
  };
}
