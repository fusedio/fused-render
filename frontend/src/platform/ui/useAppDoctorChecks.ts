// Follows the App Doctor checklist for the status dot (AppDoctorStatusDot.tsx),
// on BOTH surfaces that show it (shell/AppPage.tsx tab trigger,
// apps/explorer/EntryActionsMenu.tsx) — one policy, not two, and one
// subscription per folder: the events bus's `apps.doctor {path}` topic,
// refcounted by the client, so the dot, a second dot on the same app and the
// Doctor panel (AppDoctorModal.tsx) all share one server-side subscription and
// one cached snapshot.
//
// The report is a full content scan of the app folder, so it must never
// block or delay the page it decorates: the subscription opens in an effect
// (after the caller's own first paint, never during render), keyed on `dir`,
// and a refused frame or a slow answer just leaves the dot in its neutral
// "not known yet" state rather than surfacing anywhere else. Same rule
// app_doctor.py's own docstring states about a doctor that must never crash
// the thing it reviews — a doctor that slows an app down is just as unwelcome.
//
// The snapshot is the `fetch=0` variant of `GET /api/apps/doctor`: this dot
// decorates a page, it is never the modal's own "load", so it must not force
// a git fetch on every app open.
//
// WHEN IT MOVES. The server re-reads the report every 4 s while a CHECK TASK is
// live on any row (the old poll's cadence, now the server's) and pushes each
// change, so a check that finishes after a tab switch still moves the dot off
// "clean" over a row that just went red — the reason this dot used to poll
// while the Doctor panel was gone. Otherwise it re-reads only when asked: an
// on-demand Check (AppDoctorModal.tsx's `runCheck`) announces
// `APP_DOCTOR_CHANGED_EVENT` with its folder, and this hook answers it with a
// resync of the folder's subscription — the verdict's task is cached
// server-side now, and the topic's last snapshot predates the POST that made
// it (with no live task in it, the server would not look again on its own).
import { useEffect, useRef, useState } from "react";
import type { AppCheck, AppDoctorReport } from "@platform/lib/api";
import { resyncTopic, subscribeTopic } from "@platform/lib/events";
import { useAppDoctorChanged } from "@platform/lib/tasksChanged";

/** `checks` from `dir`'s App Doctor report, or null while unknown — no
 *  snapshot yet, or the subscription was refused. */
export function useAppDoctorChecks(dir: string | null): AppCheck[] | null {
  const [checks, setChecks] = useState<AppCheck[] | null>(null);
  useAppDoctorChanged((changed) => {
    if (dir && changed === dir) resyncTopic("apps.doctor", { path: dir });
  });
  const lastDir = useRef<string | null>(null);
  useEffect(() => {
    // A new snapshot replaces the old checks in place — a dot that blinks to
    // "unknown" for a folder walk would read as a change that did not happen.
    // Only a change of FOLDER resets it.
    if (lastDir.current !== dir) {
      lastDir.current = dir;
      setChecks(null);
    }
    if (!dir) return;
    let alive = true;
    const off = subscribeTopic<AppDoctorReport>("apps.doctor", { path: dir }, (r, _delta, meta) => {
      // A refused frame: stays as it was — the dot reads as unknown or stale,
      // nothing else is affected.
      if (!alive || meta.error !== undefined || r === null) return;
      if (Array.isArray(r.checks)) setChecks(r.checks);
    });
    return () => {
      alive = false;
      off();
    };
  }, [dir]);
  return checks;
}
