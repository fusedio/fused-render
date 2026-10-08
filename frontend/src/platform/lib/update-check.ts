// The manual "Check for updates" press, as one hook two surfaces share: the
// Preferences › Updates section (shell/Preferences.tsx) and the status bar's
// Updates chip (shell/UpdatesDock.tsx). Both show the same four-phase row —
// "Check for updates" → "Checking…" → "Up to date · vX" / "Couldn't check" →
// back to rest after CHECK_RESULT_HOLD_MS — and the carried-over bug fixes
// below belong to the press, not to either page, so they live here once.
//
// The phase is LOCAL to the surface that pressed, not the shared store: it is
// about THIS press (the answer for a few seconds), the same split the deleted
// `UpdateBadge` used between its own phase and the durable store state.
import { useCallback, useEffect, useRef, useState } from "react";

import type { UpdateStatus } from "@platform/lib/api";
import {
  CHECK_RESULT_HOLD_MS,
  checkForUpdates,
  updateRelevant,
  type ManualCheckPhase,
} from "@platform/lib/update-status";

export interface ManualCheck {
  phase: ManualCheckPhase;
  /** Fire the check. A no-op while one is already in flight. */
  check: () => Promise<void>;
}

/** `status` is the shared store's current value (`useUpdateStatus()`),
 *  which the hook watches to settle a check the server answered late. */
export function useManualUpdateCheck(status: UpdateStatus | null): ManualCheck {
  const [phase, setPhase] = useState<ManualCheckPhase>("rest");
  const holdTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(holdTimer.current), []);
  // WHEN THE SERVER WAS ALREADY LOOKING (bugbot, PR #1097): a non-forced
  // check() that lands while the auto tick's own fetch is already out returns
  // at once with "checking" — a promise of an answer, not the answer — and
  // without this flag the row would misread that arrival as "Up to date" the
  // instant it landed rather than waiting for the real result.
  const awaiting = useRef(false);

  const settle = useCallback((result: UpdateStatus) => {
    // `updateRelevant` gates "current" (finding #2, code review): a check that
    // lands while the store is already sitting on `available` must not claim
    // "Up to date" over the "Update available" notification popping at the
    // same instant. `rest` then, not "failed" — nothing here failed.
    setPhase(result.check_error ? "failed" : updateRelevant(result) ? "rest" : "current");
    clearTimeout(holdTimer.current);
    holdTimer.current = setTimeout(() => setPhase("rest"), CHECK_RESULT_HOLD_MS);
  }, []);

  useEffect(() => {
    if (!awaiting.current || !status || status.state === "checking") return;
    awaiting.current = false;
    settle(status);
  }, [status, settle]);

  const check = useCallback(async () => {
    if (phase === "checking") return;
    clearTimeout(holdTimer.current);
    setPhase("checking");
    try {
      const result = await checkForUpdates();
      if (result.state === "checking") {
        // Not an answer yet — see `awaiting` above.
        awaiting.current = true;
        return;
      }
      settle(result);
    } catch {
      // 404 (no updater), offline, server down — say so briefly; the poll
      // that drives `UpdateNotifier` owns the durable story.
      setPhase("failed");
      holdTimer.current = setTimeout(() => setPhase("rest"), CHECK_RESULT_HOLD_MS);
    }
  }, [phase, settle]);

  return { phase, check };
}
