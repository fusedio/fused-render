// The hook driving task-status-notify.ts's decision table off the EXISTING
// task-status poll (tasksPulse.ts's useTasksPulseRows) — no new server
// channel, per SPEC-quiet-notifications.md §5's own "Sources" instruction.
// Mounted once at the app root (App.tsx). It USED to gate its `notify()`
// call on the narrator election, the way `useScheduleEvents` still does; it
// no longer does (see NO NARRATOR GATE, below).
//
// NO NARRATOR GATE (removed 2026-09-17): `isNarrator()` (platform/lib/
// presence.ts) elects the OLDEST open top-level window — lowest `windowId`,
// minted off `Date.now()` — among non-stale entries. That is routinely a
// long-lived tab nobody is looking at right now. Client-raised notifications
// are per-document, in-memory state (platform/lib/notifications.ts), so no
// window OTHER than the elected narrator could ever see a row it alone
// raised. Live report: "i do see the notification listed in the cmux
// browser, but not in the browser I was using" — the active window called
// `notify()` zero times because some other, older tab happened to win the
// election. The user was shown the trade-off this creates — two open
// windows now means two toasts, the exact duplicate-alerting the election
// was built to prevent — and chose it explicitly: every top-level shell
// window now pops and retains its own copy of every task notice.
// `useTaskStatusNotify` only mounts inside the shell's `App`, so embeds are
// already excluded from this, same as before. `useScheduleEvents` and every
// other narrator-gated caller are UNCHANGED — this is scoped to task status
// notices only.
//
// ONE MAP OF "the status this task was in last time this document looked",
// keyed by the pulse row's own key — not state, so it survives re-renders
// without re-running the effect, and prunes entries for tasks no longer
// listed so a key reused by an unrelated future task starts fresh rather
// than replaying a stale transition. This used to also matter for a
// narrator HANDOFF (Finding 9, 2026-09-16: a new narrator's `prev` had to
// already be accurate the instant it was elected, or a transition landing on
// the handoff tick read as a first sighting and got dropped). There is no
// narrator any more for this hook, so that scenario cannot happen here —
// but the bookkeeping stays unconditional regardless, because it is simply
// this document's own record of what it last saw, which task-status-
// notify.ts's backfill-vs-news split (below) still depends on being right
// on every tick, narrator or not.
//
// WATCH START (2026-09-17 fix, see task-status-notify.ts's own header): this
// document's own "since when have I been polling", in unix seconds — the
// same unit as a pulse row's `happened_at`. Fixed once, at this hook's own
// first tick, never advanced again: it is deliberately NOT "the last tick's
// time", which would make the backfill window a single poll interval wide
// and miss most short runs again. This is what lets a run that starts and
// finishes between two polls still notify (task-status-notify.ts's
// first-sighting-but-after-watch-start branch), without also flooding a
// fresh tab with a popup for every one of its already-done rows.
import { useEffect, useRef } from "react";
import { notify } from "@platform/lib/notifications";
import { useTasksPulseRows } from "@shell/tasksPulse";
import { taskColumn } from "@shell/tasks-lib";
import { notificationForTransition } from "@shell/task-status-notify";

export function useTaskStatusNotify(): void {
  const tasks = useTasksPulseRows();
  const previous = useRef<Map<string, string>>(new Map());
  const watchStartS = useRef<number | null>(null);

  useEffect(() => {
    if (watchStartS.current === null) watchStartS.current = Date.now() / 1000;
    const prev = previous.current;
    const liveKeys = new Set<string>();
    for (const task of tasks) {
      liveKeys.add(task.key);
      const was = prev.get(task.key);
      const column = taskColumn(task);
      const input = notificationForTransition(was, task, watchStartS.current);
      prev.set(task.key, column);
      if (input) notify(input);
    }
    for (const key of Array.from(prev.keys())) {
      if (!liveKeys.has(key)) prev.delete(key);
    }
  }, [tasks]);
}
