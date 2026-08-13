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
    return { state: { banner, fails }, reload: false };
  }

  const wasDown = state.banner === "down";
  const served = probe.version;
  const installed = probe.installedVersion ?? null;

  if (served && installed && installed !== served) {
    return { state: { banner: "update-restart", fails: 0 }, reload: false };
  }
  if (served && served !== buildVersion) {
    // Recovering from "down" onto a new version means the server restarted
    // updated — the user was blocked anyway, so reload without asking.
    if (wasDown) return { state: { banner: "reconnected", fails: 0 }, reload: true };
    return { state: { banner: "update-refresh", fails: 0 }, reload: false };
  }
  if (wasDown) return { state: { banner: "reconnected", fails: 0 }, reload: false };
  // "reconnected" lingers until the component's dismiss timer hides it.
  if (state.banner === "reconnected") return { state, reload: false };
  return { state: { banner: "hidden", fails: 0 }, reload: false };
}
