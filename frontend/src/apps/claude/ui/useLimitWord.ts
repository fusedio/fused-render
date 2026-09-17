// THE CHAT HEADER'S "paused · resumes 4:00 AM", read off THIS conversation's own
// `/api/tasks` row (Bugbot PR #1124).
//
// WHY IT IS NOT `sched.rec`. That row is the scheduled-message block's, and the
// block only fetches it while a CARD is drawn — "is anything of this
// conversation's waiting" (`useSchedule`'s `hasCard`). A session that hit the
// plan's usage limit with nothing pending behind it therefore had no row at all,
// and the one surface the reader is actually looking at said nothing about the
// one thing that had stopped their chat.
//
// SO IT IS ITS OWN READ, and a cheap one: `/api/tasks` on mount, again on
// `tasks-changed` (which is exactly what the comeback's own POST announces —
// protocol/run-controller `scheduleComeback`), and never twice inside
// `LIMIT_REFRESH_MS`. No poll: a usage limit is an event, and the two things
// that can produce one both ring the announcement.
import { useEffect, useState } from "react";
import { getTasks } from "@platform/lib/api";
import { TASKS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { usageLimitStatusWord, type UsageLimitFacts } from "@platform/lib/usage-limit";

/** The floor between two reads, `useSchedule`'s `REC_REFRESH_MS` restated for
 *  the same reason it exists: the announcement can be rung several times in one
 *  second (a turn starting, a comeback landing) and the listing is a glob over
 *  every transcript on the machine. */
export const LIMIT_REFRESH_MS = 5000;

/** Injectable for the suite's sake, so a test proving the floor does not sleep
 *  five real seconds per assertion. */
export interface LimitWordDeps {
  getTasks?: () => Promise<{ tasks?: UsageLimitFactsRow[] }>;
  now?: () => number;
  floorMs?: number;
}

/** Only what this reads — `key` to find the row, and the three fields the
 *  sentence is built from. */
export type UsageLimitFactsRow = UsageLimitFacts & { key?: string };

/**
 * "paused · resumes 4:00 AM" for the chat keyed `taskKey`, "" for every other
 * conversation and for one whose row cannot be read.
 *
 * `taskKey` is the session id, or `pending:<leader entry>` for a chat that has
 * not run yet — the same key `useTaskId` compares, so one read answers for both
 * kinds of conversation.
 */
export function useLimitWord(taskKey: string, deps: LimitWordDeps = {}): string {
  const [word, setWord] = useState("");
  const fetchTasks = deps.getTasks;
  const clock = deps.now;
  const floor = deps.floorMs;
  useEffect(() => {
    if (!taskKey) {
      setWord("");
      return;
    }
    let live = true;
    let busy = false;
    let last = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const now = clock ?? Date.now;
    const gap = floor ?? LIMIT_REFRESH_MS;
    const api = fetchTasks ?? (getTasks as () => Promise<{ tasks?: UsageLimitFactsRow[] }>);
    const read = () => {
      // NEVER TWO AT ONCE: the announcement is rung by several things and a
      // second listing started over the first buys nothing but load.
      if (busy) return;
      busy = true;
      last = now();
      void api()
        .then((data) => {
          if (!live) return;
          const row = (data.tasks || []).find((t) => t && t.key === taskKey);
          setWord(usageLimitStatusWord(row));
        })
        .catch(() => {
          // The header stands: an unreadable listing is not a reason to tell
          // the reader their session is paused, nor to say it stopped being.
        })
        .finally(() => {
          busy = false;
        });
    };
    /** A poke, floored — and a poke that lands inside the floor is not dropped,
     *  it is DEFERRED to the end of it. The one announcement that matters most
     *  (the comeback being scheduled) can easily land a second after a read. */
    const poke = () => {
      if (timer !== null) return;
      const wait = gap - (now() - last);
      if (wait <= 0) {
        read();
        return;
      }
      timer = setTimeout(() => {
        timer = null;
        if (live) read();
      }, wait);
    };
    read();
    window.addEventListener(TASKS_CHANGED_EVENT, poke);
    return () => {
      live = false;
      if (timer !== null) clearTimeout(timer);
      window.removeEventListener(TASKS_CHANGED_EVENT, poke);
    };
  }, [taskKey, fetchTasks, clock, floor]);
  return word;
}
