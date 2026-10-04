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
// `update-restart` USED TO be a blocking dialog (`UpdateDialog`'s "restart"
// mode, D1) and before that a card in the notification stack. Both are gone
// now (SPEC-update-notifications.md): the decision moment is a status-bar
// notification (`platform/ui/UpdateNotifier.tsx`), driven off the shared
// update store and `restart-flow.ts` directly, not off this banner. This
// table is unchanged by any of that churn — what a probe MEANS is the same
// fact regardless of which surface narrates it — except that `bannerSurface`
// below no longer has a dialog to point at, only a card to suppress.
import { restartInFlight, type RestartStage } from "@platform/lib/restart-flow";

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

/** The one thing the preview flag can ask for — the refresh dialog. It used to
 *  also carry `"restart"` (`?update_modal=restart&stage=...`) for
 *  `UpdateDialog`'s now-deleted restart mode; that value is gone along with
 *  the mode it previewed (SPEC-update-notifications.md) rather than kept
 *  alive for a component that no longer exists. `"1"` keeps its spelling so a
 *  bookmarked dev URL still works. */
const PREVIEW_VALUES = ["1"] as const;
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
 * WHICH dialog the preview flag is asking for. Null when the flag is not set
 * at all. Only the refresh dialog can be previewed now — `updateDialogPreview`
 * used to also answer for `UpdateDialog`'s "restart" mode
 * (`?update_modal=restart&stage=...`), freezing it at one of its six faces
 * with no real restart behind it. That mode is deleted
 * (SPEC-update-notifications.md: the restart decision is a notification, not
 * a dialog), so there is nothing left for a `stage`/`slow` flag to preview —
 * dropped rather than kept pointed at a component that no longer exists.
 */
export function updateDialogPreview(
  search: string,
  stored: string | null,
): { kind: "refresh" } | null {
  const value = previewValue(search, stored);
  return value === null ? null : { kind: "refresh" };
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

export type BannerSurface = "none" | "down" | "reconnected" | "refresh-dialog";

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
  // THE SERVER IS STILL ANSWERING, JUST AN OLDER BUILD (2026-09-22 fix,
  // finding #5, code review). `banner === "update-restart"` is `reduceProbe`
  // saying the disk is ahead of what the running server just served on a
  // HEALTHY probe — the app is demonstrably up, just not yet restarted onto
  // the new build, so the "isn't running" card would be a lie here.
  //
  // `updateState === "installed"` used to be OR'd in alongside it (the same
  // fact two ticks earlier, off the update store's 2s busy poll, per D2 —
  // `UpdateNotifier` reads the SAME field to raise its own restart
  // notification without waiting for a 5s probe). That reasoning is sound
  // for as long as the server is actually still responding. The bug: this
  // field never goes back to anything else until an ACTUAL restart happens
  // (installing on disk does not touch the live process), so it stayed true
  // for the rest of the session regardless of what happened next. If the
  // running server then genuinely crashed for an unrelated reason — no
  // restart ever requested, `stage` still sitting at "ready" — two failed
  // probes flipped `banner` to "down" while `updateState` was still
  // "installed" from minutes earlier, and this OR suppressed the real outage
  // forever: a session-long "everything's fine" over an app that had
  // actually died. Dropping it and keeping only `banner === "update-restart"`
  // fixes that — that condition is false the instant probes start failing
  // (`reduceProbe` overwrites `banner` with "down" past `FAIL_THRESHOLD`,
  // whatever it was before), so it can no longer paper over a real outage,
  // while a genuine "disk ahead, server still healthy" wait still shows
  // nothing, exactly as before.
  const diskAheadOfHealthyServer = banner === "update-restart";

  // THE CAP'S FALL-THROUGH (D4), and it is the FIRST thing asked. `gave-up`
  // means the stage machine stopped promising; what the page shows from then on
  // is whatever the SERVER says, with no stage attached. While the server is
  // still not answering that is the ordinary down card — the case the cap
  // exists for — and it has to outrank the door below, which stays open
  // through an outage: `update-restart` is the last thing the banner knew
  // before probes started failing. Without this the down card would stay
  // suppressed past the promise that suppressed it.
  //
  // WITH THE SERVER ANSWERING THE DOWN CARD STAYS SUPPRESSED TOO, and that is
  // deliberate (bugbot, PR #1214): a restart that did not take leaves the app
  // demonstrably running with the disk still ahead, so "fused-render isn't
  // running" would be a lie. The ordinary door below takes it instead — there
  // is no dialog left to draw a button on `gave-up`; the restart notification
  // (`UpdateNotifier`) offers the retry instead, and it is not this function's
  // concern.
  if (stage === "gave-up" && banner === "down") return "down";

  // A restart in flight (or a disk-ahead wait with the server still
  // healthy) suppresses the "isn't running" card on its own — this IS step
  // 5, the whole of what PR #1214 fixed, and the one thing this function
  // still owes the restart flow now that the dialog itself is gone: the app
  // being gone from the first failed probe onward, DURING an actual
  // restart, is the restart working, not an outage to report.
  // `restartInFlight(stage)` alone already covers every in-flight restart
  // stage regardless of `banner` (see this file's own test asserting the
  // down card never wins during one) — `diskAheadOfHealthyServer` only adds
  // the pre-request "ready to restart, haven't pressed it yet" wait.
  if (restartInFlight(stage) || diskAheadOfHealthyServer) return "none";
  if (banner === "hidden") return "none";
  if (banner === "reconnected") return "reconnected";
  if (banner === "update-refresh") return mode === "off" ? "none" : "refresh-dialog";
  return "down";
}

/**
 * WHETHER A HIDDEN TAB KEEPS PROBING. Normally it does not: a tab nobody is
 * looking at has no banner to keep honest, and the probe on `visibilitychange`
 * catches it up. During a RESTART it must: the reader presses Restart, sees
 * the app come back in the Dock 20s later and clicks it — the tab goes hidden
 * at exactly the moment the new server starts answering. A tab that stops
 * probing there never sees the version move, never reaches `back`, never
 * reloads, and sits on "Reconnecting…" until the reader comes back and
 * reloads it by hand (Akshil, 2026-09-19). The stage is the restart store's,
 * so the rule holds for a press made in another window too.
 */
export function probeWhileHidden(stage: RestartStage): boolean {
  return restartInFlight(stage);
}

/** THE POLL TICK'S WHOLE DECISION, so it can be tested without a 5s timer: a
 *  visible tab always probes; a hidden one only during a restart. `visibility`
 *  is `document.visibilityState`, which a DOM shim may leave undefined — that
 *  is not "hidden", so it probes as a visible tab would. */
export function probeOnTick(visibility: string | undefined, stage: RestartStage): boolean {
  return visibility !== "hidden" || probeWhileHidden(stage);
}
