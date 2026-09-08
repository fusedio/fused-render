// Persistence for Notifications-panel row dismissals — a repo row's ✕ and a
// waiting-task row's ✕ each keep a `{ id: signature }` map in `localStorage`
// under their own key, surviving reload. Split out of RepoUpdatesDock.tsx so
// that file stays free of `localStorage` itself: it is the panel's FOLD that
// must never persist (tests/test_activity_bar_structure.py), not a per-row
// dismissal, and keeping the string entirely out of that file is what makes
// the guard checkable as an absence rather than a judgment call.
//
// Same defensive, best-effort pattern as sidebarstate.ts: a private window, a
// full quota or malformed JSON all just behave as "nothing dismissed" rather
// than throwing.
export function loadDismissed(key: string): Record<string, string> {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object") return {};
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === "string") out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

export function saveDismissed(key: string, next: Record<string, string>): void {
  try {
    localStorage.setItem(key, JSON.stringify(next));
  } catch {
    // storage unavailable — dismissal is best-effort, so a failed write is fine
  }
}
