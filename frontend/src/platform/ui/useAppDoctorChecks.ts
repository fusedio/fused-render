// Fetches the App Doctor checklist for the status dot (AppDoctorStatusDot.tsx),
// on BOTH surfaces that show it (shell/AppPage.tsx tab trigger,
// apps/explorer/EntryActionsMenu.tsx) — one fetch policy, not two.
//
// The report is a full content scan of the app folder, so it must never
// block or delay the page it decorates: the fetch starts in an effect (after
// the caller's own first paint, never during render), runs once per app open
// (keyed on `dir` — no polling, no re-fetch on an unrelated re-render), and a
// failure or a slow response just leaves the dot in its neutral "not known
// yet" state rather than surfacing anywhere else. Same rule app_doctor.py's
// own docstring states about a doctor that must never crash the thing it
// reviews — a doctor that slows an app down is just as unwelcome.
//
// The ONE exception to once-per-open: an on-demand Check (AppDoctorModal.tsx's
// `runCheck`) announces `APP_DOCTOR_CHANGED_EVENT` with its folder, and this
// hook refetches for that folder — the verdict is now cached server-side, so
// that GET costs a folder walk and no tokens, and the dot would otherwise
// stay clean over a row that just went red until the app was opened again.
import { useEffect, useRef, useState } from "react";
import { getAppDoctor, type AppCheck } from "@platform/lib/api";
import { useAppDoctorChanged } from "@platform/lib/tasksChanged";

/** `checks` from `dir`'s App Doctor report, or null while unknown — not
 *  fetched yet, or the fetch failed. */
export function useAppDoctorChecks(dir: string | null): AppCheck[] | null {
  const [checks, setChecks] = useState<AppCheck[] | null>(null);
  // Bumped by the changed-event for THIS folder; the effect below keys on
  // it, so a refetch is the same code path as the first fetch.
  const [generation, setGeneration] = useState(0);
  useAppDoctorChanged((changed) => {
    if (dir && changed === dir) setGeneration((g) => g + 1);
  });
  const lastDir = useRef<string | null>(null);
  useEffect(() => {
    // A refetch keeps the old checks on screen until the new ones land — a
    // dot that blinks to "unknown" for a folder walk would read as a change
    // that did not happen. Only a change of FOLDER resets it.
    if (lastDir.current !== dir) {
      lastDir.current = dir;
      setChecks(null);
    }
    if (!dir) return;
    let alive = true;
    // Always the poll variant: this dot decorates a page, it is never the
    // modal's own "load", so it must not force a git fetch on every app open
    // (nor on every 4s tick while a check task is live).
    getAppDoctor(dir, { fetch: false })
      .then((r) => {
        if (alive) setChecks(r.checks);
      })
      .catch(() => {
        /* stays as it was — the dot reads as unknown or stale, nothing else is affected */
      });
    return () => {
      alive = false;
    };
  }, [dir, generation]);

  // The OTHER exception: while a CHECK TASK is live on any row, ask again
  // every few seconds until it is gone. The Doctor panel polls for itself
  // while mounted, but this dot outlives it (the tab trigger, the explorer
  // menu) — a check that finishes after a tab switch would otherwise leave
  // the dot clean over a row that just went red until the app was reopened.
  // Same cheap GET as above; the poll stops the moment no row carries a task.
  const liveCheck = checks?.some((c) => c.check_task) ?? false;
  useEffect(() => {
    if (!dir || !liveCheck) return;
    const timer = window.setInterval(() => setGeneration((g) => g + 1), CHECK_POLL_MS);
    return () => window.clearInterval(timer);
  }, [dir, liveCheck]);
  return checks;
}

// How often the dot re-asks while a check task is live — a Sonnet read of a
// few files takes tens of seconds, so this lands the verdict promptly
// without hammering a folder walk. Matches AppDoctorModal.tsx's own poll.
const CHECK_POLL_MS = 4_000;
