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
// `useScheduleEvents` and every other narrator-gated caller are UNCHANGED —
// this is scoped to task status notices only.
//
// EMBED GUARD (2026-09-18 fix): this hook used to assert above that mounting
// only inside the shell's `App` already excluded embeds — that assertion was
// wrong. `App.tsx` renders the same shell `App` component for an embedded
// pane too (e.g. a split view's left pane), so "mounts inside App" does not
// imply "never runs as an embed". A shell App rendered as an embed ran this
// hook's poll and raised the same finished-task notice as the top shell, and
// `notifications.ts`'s pane->shell forwarding carried it up again — N
// documents watching one task raising N notices for it. The `IS_EMBED` check
// in the effect body below (not around `useEffect` itself, to keep this a
// plain, unconditional hook call per the rules of hooks) is what actually
// enforces "only the top-level shell raises task-status notices" now.
//
// NARROWED TO `IS_EMBED && !IS_TOP_EMBED` (F3, 2026-09-18 fix, code review
// round): a bare `if (IS_EMBED) return;` also silences a standalone TOP-EMBED
// window — a Finder double-click on a `.fused` file, a CLI/deeplink
// `/explorer/embed/` URL (router.ts's `IS_TOP_EMBED` comment) — which is a
// WHOLE window with no parent pane to forward a notice on its behalf
// (`notifications.ts`'s pane->shell forwarding only exists for a non-top
// embed in the first place; there is nothing above a top embed to forward
// to). A task finishing while the user sits in one of those windows
// previously produced no notice at all where it did before this hook grew an
// embed guard — the guard was too wide, not merely unnecessary. Every other
// embed rule in `notifications.ts` already uses this exact
// `IS_EMBED && !IS_TOP_EMBED` pairing for the identical reason (see its
// `neverExpiresHere`/`effectiveIsTopEmbed()` uses) — this hook now matches
// that convention instead of standing apart from it.
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
import { snapshotIsOpenExact } from "@platform/lib/presence";
import { IS_EMBED, IS_TOP_EMBED } from "@platform/lib/router";
import { useTasksPulseRows } from "@shell/tasksPulse";
import { taskColumn } from "@shell/tasks-lib";
import { notificationForTransition } from "@shell/task-status-notify";
import { useTaskNotifyTerminalSessions } from "@shell/task-notify-terminal-flag";

export function useTaskStatusNotify(): void {
  const tasks = useTasksPulseRows();
  const previous = useRef<Map<string, string>>(new Map());
  const watchStartS = useRef<number | null>(null);
  // The Preferences toggle (default off — see task-notify-terminal-flag.ts's
  // own header) for whether a finished-task notice fires for an interactive-
  // terminal session too. Read as a live subscription, not a snapshot, so
  // flipping it in Preferences takes effect on the very next tick without a
  // reload.
  const notifyTerminalSessions = useTaskNotifyTerminalSessions();

  useEffect(() => {
    if (IS_EMBED && !IS_TOP_EMBED) return;
    if (watchStartS.current === null) watchStartS.current = Date.now() / 1000;
    const prev = previous.current;
    const liveKeys = new Set<string>();
    // ONE presence snapshot for the whole tick (F8) — see
    // `snapshotIsOpenAnywhere`'s own doc comment: this loop calls
    // `notificationForTransition` once per task, and a fresh
    // `localStorage` read/parse per task per tick is exactly the pattern
    // it exists to avoid. `snapshotIsOpenExact`, not `snapshotIsOpenAnywhere`
    // (F9 fix): this gate needs an EXACT destination match, never the wider
    // ancestor/descendant prefix rule `matchesSource` applies for other
    // callers — see `snapshotIsOpenExact`'s own doc comment.
    const isOpenAnywhere = snapshotIsOpenExact();
    for (const task of tasks) {
      liveKeys.add(task.key);
      const was = prev.get(task.key);
      const column = taskColumn(task);
      const input = notificationForTransition(
        was, task, watchStartS.current, notifyTerminalSessions, isOpenAnywhere);
      prev.set(task.key, column);
      if (input) notify(input);
    }
    for (const key of Array.from(prev.keys())) {
      if (!liveKeys.has(key)) prev.delete(key);
    }
  }, [tasks, notifyTerminalSessions]);
}
