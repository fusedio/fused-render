// Decision table behind ServerStatusBanner. Pure — the component owns the
// polling, timers and rendering; this owns what one probe result means.
//
// Two update cases, deliberately distinct verbs:
//   update-refresh  — the server serves a newer version than this bundle was
//                     built from: a page refresh picks up the new shell.
//   update-restart  — the version installed on disk is newer than the running
//                     server (DMG replaced the bundle under a live process):
//                     only an app restart helps, a refresh would change nothing.
// Restart outranks refresh: while the disk is ahead of the server, a refresh
// still leaves a stale server, so never advertise it (and never auto-reload).

export type ServerBanner =
  | "hidden"
  | "down"
  | "reconnected"
  | "update-refresh"
  | "update-restart";

export interface StatusState {
  banner: ServerBanner;
  fails: number;
  /** Last served version a healthy probe reported; undefined before one. */
  served?: string;
}

export interface ProbeResult {
  ok: boolean;
  /** Running server's version, from /api/config. */
  version?: string;
  /** Version installed on disk (bundle Info.plist); null when unpackaged. */
  installedVersion?: string | null;
}

export const FAIL_THRESHOLD = 2;

export function initialStatus(): StatusState {
  return { banner: "hidden", fails: 0 };
}

export function reduceProbe(
  state: StatusState,
  probe: ProbeResult,
  buildVersion: string,
): { state: StatusState; reload: boolean } {
  if (!probe.ok) {
    const fails = state.fails + 1;
    const banner = fails >= FAIL_THRESHOLD ? "down" : state.banner;
    return { state: { banner, fails, served: state.served }, reload: false };
  }

  const wasDown = state.banner === "down";
  const served = probe.version;
  const installed = probe.installedVersion ?? null;
  const next = (banner: ServerBanner) => ({
    banner,
    fails: 0,
    served: served ?? state.served,
  });

  if (served && installed && installed !== served) {
    return { state: next("update-restart"), reload: false };
  }
  if (served && served !== buildVersion) {
    // A version can only change under a process swap, so seeing it move —
    // either across a "down" gap or between two healthy probes (a restart
    // faster than the down threshold, or one that happened while the tab was
    // hidden) — means the server restarted updated. The user asked for that
    // (or was blocked by it), so reload without asking; views are URL-synced.
    const transitioned = wasDown || (state.served !== undefined && state.served !== served);
    if (transitioned) return { state: next("reconnected"), reload: true };
    return { state: next("update-refresh"), reload: false };
  }
  if (wasDown) return { state: next("reconnected"), reload: false };
  // "reconnected" is dismissed by the component's timer, but never held past
  // the next probe: an in-flight probe can write a stale "reconnected" back
  // AFTER the timer fired, with no new timer armed (wasDown is false) — held
  // here, that card would stick until the next outage.
  return { state: next("hidden"), reload: false };
}
