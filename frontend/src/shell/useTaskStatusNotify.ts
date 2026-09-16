// The hook driving task-status-notify.ts's decision table off the EXISTING
// task-status poll (tasksPulse.ts's useTasksPulseRows) — no new server
// channel, per SPEC-quiet-notifications.md §5's own "Sources" instruction.
// Mounted once at the app root (App.tsx), alongside useScheduleEvents, whose
// narrator-gating shape this follows exactly.
//
// NARRATOR-ONLY (§1/§5): only the elected top-level window raises these —
// every embed iframe, and every non-narrator top-level tab, would otherwise
// notify the same transition independently and double/triple-alert it.
// Checked every time the pulse rows change (not just once at mount), same
// reasoning as scheduleEvents.ts's per-tick check: narration hands off
// cleanly the moment the current narrator's tab closes.
//
// ONE MAP OF "the status this task was in last time this document looked",
// keyed by the pulse row's own key — not state, so it survives re-renders
// without re-running the effect, and prunes entries for tasks no longer
// listed so a key reused by an unrelated future task starts fresh rather
// than replaying a stale transition.
import { useEffect, useRef } from "react";
import { notify } from "@platform/lib/notifications";
import { isNarrator } from "@platform/lib/presence";
import { useTasksPulseRows } from "@shell/tasksPulse";
import { taskColumn } from "@shell/tasks-lib";
import { notificationForTransition } from "@shell/task-status-notify";

export function useTaskStatusNotify(): void {
  const tasks = useTasksPulseRows();
  const previous = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    // Finding 9 (code review 2026-09-16): the narrator check used to short-
    // circuit BEFORE `prev` was touched at all, so a non-narrator window's
    // book of "what column was this task in last time I looked" simply never
    // advanced. That's fine while narration stays put, but the moment THIS
    // window becomes the new narrator (the old one's tab closed), its first
    // tick as narrator would find `prev` still at whatever it was the last
    // time this window looked (often nothing, if it was never narrator
    // before) — so a transition that genuinely happened while this window
    // was non-narrating either replays a stale one or, if the transition
    // lands in that very handoff tick, gets read as "first sighting" and its
    // notification is silently dropped.
    //
    // The book-keeping (`prev.set`/pruning) now runs on EVERY tick regardless
    // of narrator status, so `prev` is always accurate the instant a window
    // is handed the narrator role. Only the actual `notify()` call stays
    // gated on `isNarrator()` — see header for why only the elected window
    // may raise these.
    const narrator = isNarrator();
    const prev = previous.current;
    const liveKeys = new Set<string>();
    for (const task of tasks) {
      liveKeys.add(task.key);
      const was = prev.get(task.key);
      const column = taskColumn(task);
      const input = notificationForTransition(was, task);
      prev.set(task.key, column);
      if (narrator && input) notify(input);
    }
    for (const key of Array.from(prev.keys())) {
      if (!liveKeys.has(key)) prev.delete(key);
    }
  }, [tasks]);
}
