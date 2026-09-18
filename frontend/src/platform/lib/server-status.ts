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
//
// BOTH CASES ARE ONE DIALOG NOW (D1, 2026-09-18): `update-restart` used to be a
// card in the notification stack and is `UpdateDialog`'s "restart" mode — see
// that component, and `restart-flow.ts` for the stages it shows once the button
// is pressed. This table is unchanged by that: what a probe MEANS is the same
// fact either way.
import { restartInFlight, RESTART_STAGES, type RestartStage } from "@platform/lib/restart-flow";

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
  /** True when the server is a dev.sh run (/api/config `dev`). */
  dev?: boolean;
}

export const FAIL_THRESHOLD = 2;

/** The URL param and the localStorage key that turn the refresh dialog into a
 *  PREVIEW in dev — how the dialog itself is looked at (see
 *  `updateDialogMode`). */
export const UPDATE_DIALOG_PARAM = "update_modal";
export const UPDATE_DIALOG_KEY = "fused_update_modal";
/** Which stage the RESTART preview is frozen at — `?update_modal=restart&
 *  stage=reconnecting`. Ignored by the refresh preview, which has no stages. */
export const UPDATE_STAGE_PARAM = "stage";

/** The two things the preview flag can ask for. `"1"` is the original refresh
 *  preview and keeps its spelling so a bookmarked dev URL still works. */
const PREVIEW_VALUES = ["1", "restart"] as const;
type PreviewValue = (typeof PREVIEW_VALUES)[number];

function previewValue(search: string, stored: string | null): PreviewValue | null {
  const isValue = (v: string | null): v is PreviewValue =>
    v !== null && (PREVIEW_VALUES as readonly string[]).includes(v);
  if (isValue(stored)) return stored;
  const fromUrl = new URLSearchParams(search).get(UPDATE_DIALOG_PARAM);
  return isValue(fromUrl) ? fromUrl : null;
}

/** What the refresh dialog is doing right now:
 *   "real"    — a genuine mismatch blocks the page (every packaged server).
 *   "off"     — suppressed, because on a dev run the mismatch is a lie.
 *   "preview" — forced on with no mismatch, to work ON the dialog. */
export type UpdateDialogMode = "real" | "off" | "preview";

/**
 * THE REFRESH DIALOG HAS THREE MODES, and only the first is a product state.
 *
 * "real" — everywhere but a dev run. The tab is on an older shell than the
 * server it is talking to, and every click from here is a guess about which
 * side is answering, so `update-refresh` blocks the page.
 *
 * "off" — A DEV RUN IS THE EXCEPTION, and not as a convenience. There the
 * served `version` is whatever the checkout says while the bundle in the tab
 * was rebuilt by the vite watch seconds ago — the two disagree on the ONE fact
 * the dialog is about, so it fired on a version bump or a branch switch at a
 * developer already looking at the newest code, and blocked the page they were
 * looking at it in. Suppressed rather than softened: a card that is always
 * wrong here teaches you to ignore the card.
 *
 * "preview" — dev plus `?update_modal=1` (or `localStorage.fused_update_modal
 * = "1"` for a run of page loads). Merely LIFTING the suppression showed
 * nothing (Akshil, 2026-09-14: "just adding ?update_modal=1 doesn't work"),
 * because a dev server and its own freshly built bundle report the same
 * version — there is no mismatch left to reveal. So the flag forces the dialog
 * up regardless of the banner state, with the REAL numbers on it (the server's
 * `version` as the new one, this bundle's `__BUILD_VERSION__` as the page's):
 * it is the dialog, not a mock of it, and the only thing invented is the
 * disagreement.
 *
 * Prod is untouched by the flag: it is already "real" and never suppressed.
 */
export function updateDialogMode(
  dev: boolean,
  search: string,
  stored: string | null,
): UpdateDialogMode {
  if (!dev) return "real";
  return previewValue(search, stored) ? "preview" : "off";
}

/**
 * WHICH dialog the preview flag is asking for, and — for the restart one —
 * which stage to freeze it at. Null when the flag is not set at all.
 *
 * The restart mode needs this where the refresh mode did not, because it has
 * SIX faces and five of them only exist while the app is actually gone: the
 * only way to look at "Reconnecting…" otherwise is to quit the app you are
 * working in and race the respawn. `?update_modal=restart&stage=reconnecting`
 * paints that face with no restart behind it, exactly as the refresh preview
 * paints a mismatch that is not there.
 *
 * An unknown or missing `stage` is `ready` — the face a real restart starts on,
 * so a typo shows the dialog rather than nothing.
 */
export function updateDialogPreview(
  search: string,
  stored: string | null,
): { kind: "refresh" } | { kind: "restart"; stage: RestartStage } | null {
  const value = previewValue(search, stored);
  if (value === null) return null;
  if (value === "1") return { kind: "refresh" };
  const raw = new URLSearchParams(search).get(UPDATE_STAGE_PARAM);
  const known = (RESTART_STAGES as readonly string[]).includes(raw ?? "");
  return { kind: "restart", stage: known ? (raw as RestartStage) : "ready" };
}

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

// ---- what the banner actually PUTS ON SCREEN ------------------------------
//
// `reduceProbe` above answers "what did that probe mean". This answers the
// second question, which has three more inputs than a probe — the dev
// suppression, the shared update store, and whether a restart is in flight —
// and which two surfaces have to agree on: a "down" card under a dialog that
// says the app is coming back is the exact contradiction this flow exists to
// remove. Pure, so the agreement is a test rather than a reading of JSX.

export type BannerSurface =
  | "none"
  | "down"
  | "reconnected"
  | "refresh-dialog"
  | "restart-dialog";

export interface SurfaceInput {
  banner: ServerBanner;
  /** `updateDialogMode`'s answer. "preview" is handled by the component ahead
   *  of this — a preview is a hand-set flag, not a state to reason about. */
  mode: UpdateDialogMode;
  /** The shared update store's `state`, when there is an updater at all
   *  (platform/lib/update-status). */
  updateState?: string;
  stage: RestartStage;
}

export function bannerSurface({ banner, mode, updateState, stage }: SurfaceInput): BannerSurface {
  // TWO DOORS INTO THE RESTART DIALOG, and it needs both. The banner's own
  // `update-restart` is the durable fact (the disk is ahead of the running
  // app), reached on the 5 s probe; the update store saying "installed" is the
  // same news two ticks earlier, on its 2 s busy cadence, which is what makes
  // the dialog POP ON ITS OWN the moment the install lands (D2) rather than
  // whenever the next probe happens to be.
  const installedReady = banner === "update-restart" || updateState === "installed";
  // A restart already in flight holds the dialog on its own: from the first
  // failed probe onwards there is no /api/config left to ask, so neither door
  // above can stay open — and this is also what SUPPRESSES THE DOWN CARD
  // (step 5), because the dialog is returned before it.
  //
  // `gave-up` is the one stage that overrides both doors. Its whole job (D4) is
  // to stop promising, and both doors stay open through an outage — the update
  // store keeps its last value when the poll fails — so without this the modal
  // would outlive the promise it made and the down card would never come back.
  if (stage !== "gave-up" && (restartInFlight(stage) || installedReady)) return "restart-dialog";
  if (banner === "hidden") return "none";
  if (banner === "reconnected") return "reconnected";
  if (banner === "update-refresh") return mode === "off" ? "none" : "refresh-dialog";
  return "down";
}
