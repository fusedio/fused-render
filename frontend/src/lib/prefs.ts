// Shared reads of the persisted /api/prefs state (shell/prefs.py; SPEC §20).
import { useEffect, useRef, useState } from "react";
import { getPrefs } from "./api";
import { useRefreshOnReturn } from "./hooks";

// Whether the Deploy affordance is enabled (Preferences → Deployments; SPEC
// §20). Deploy is opt-in, so callers stay hidden until the pref reads on —
// default false while loading means it never flashes on for a user who left
// it off. Re-read on focus/visibility so toggling it in the Preferences tab
// shows through without a reload (same cheap-local-read posture as the
// deploy dot and the account signed-in dot).
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
  useEffect(refresh, []); // initial read
  useRefreshOnReturn(refresh);
  return enabled;
}
