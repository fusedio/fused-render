// Fetches the App Doctor checklist for the header status dot
// (AppDoctorStatusDot.tsx), on BOTH surfaces that show it (shell/AppPage.tsx,
// apps/explorer/Preview.tsx) — one fetch policy, not two.
//
// The report is a full content scan of the app folder, so it must never
// block or delay the page it decorates: the fetch starts in an effect (after
// the caller's own first paint, never during render), runs once per app open
// (keyed on `dir` — no polling, no re-fetch on an unrelated re-render), and a
// failure or a slow response just leaves the dot in its neutral "not known
// yet" state rather than surfacing anywhere else. Same rule app_doctor.py's
// own docstring states about a doctor that must never crash the thing it
// reviews — a doctor that slows an app down is just as unwelcome.
import { useEffect, useState } from "react";
import { getAppDoctor, type AppCheck } from "@platform/lib/api";

/** `checks` from `dir`'s App Doctor report, or null while unknown — not
 *  fetched yet, or the fetch failed. */
export function useAppDoctorChecks(dir: string | null): AppCheck[] | null {
  const [checks, setChecks] = useState<AppCheck[] | null>(null);
  useEffect(() => {
    setChecks(null);
    if (!dir) return;
    let alive = true;
    getAppDoctor(dir)
      .then((r) => {
        if (alive) setChecks(r.checks);
      })
      .catch(() => {
        /* stays null — the dot reads as unknown, nothing else is affected */
      });
    return () => {
      alive = false;
    };
  }, [dir]);
  return checks;
}
