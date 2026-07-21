// Shared reads of the persisted /api/prefs state (shell/prefs.py; SPEC §20).
import { useEffect, useRef, useState } from "react";
import { getPrefs } from "./api";
import { useRefreshOnReturn } from "./hooks";

// Cross-component "prefs changed" signal (the notifyAccountChanged pattern,
// lib/account.ts): Preferences and the sidebar are both mounted at once —
// unlike Preview, the sidebar never remounts on navigation, so it has no
// "initial read" to catch a same-tab toggle, and the toggle itself raises no
// focus/visibility event. Without this the sidebar's signed-in dot would
// stay stale until the user left and returned to the tab even though the
// Preferences page's own account-tab visibility already updated.
const PREFS_EVENT = "fused:prefschange";

export function notifyPrefsChanged() {
  window.dispatchEvent(new Event(PREFS_EVENT));
}

// Whether the Deploy affordance is enabled (Preferences → Deploy to Fused
// account; SPEC §20). Deploy is opt-in, so callers stay hidden until the
// pref reads on — default false while loading means it never flashes on for
// a user who left it off. Re-read on focus/visibility (the deploy-dot and
// account-dot cadence) and on notifyPrefsChanged, so a same-tab toggle on
// the Preferences page shows through immediately, not just after a refocus.
export function useDeployEnabled(): boolean {
  const [enabled, setEnabled] = useState(false);
  const alive = useRef(true);
  useEffect(() => () => {
    alive.current = false;
  }, []);
  const refresh = () => {
    getPrefs()
      .then((p) => {
        if (alive.current) setEnabled(p.deploy.enabled);
      })
      .catch(() => {});
  };
  useRefreshOnReturn(refresh);
  useEffect(() => {
    refresh(); // initial read
    const onChange = () => refresh();
    window.addEventListener(PREFS_EVENT, onChange);
    return () => window.removeEventListener(PREFS_EVENT, onChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return enabled;
}
