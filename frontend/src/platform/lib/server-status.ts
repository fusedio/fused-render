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
import {
  restartInFlight,
  RESTART_SLOW_MS,
  RESTART_STAGES,
  type RestartStage,
} from "@platform/lib/restart-flow";

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
/** Freeze the restart preview PAST the 25s mark, so the "taking a little longer
 *  than usual" sentence can be looked at without sitting through the wait —
 *  `?update_modal=restart&stage=restarting&slow=1`. Dev-only, like the rest of
 *  the preview: it only says how far back to place the pretend press. */
export const UPDATE_SLOW_PARAM = "slow";

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
 *
 * `slow` is the SEVENTH face: the in-flight body after `RESTART_SLOW_MS`, which
 * is not a stage (the machine is still in `restarting`) and so cannot be reached
 * by naming one. It is carried as a flag rather than as a stage for exactly that
 * reason — inventing a stage for it here would put a face in the preview that
 * the real machine can never be in.
 */
export function updateDialogPreview(
  search: string,
  stored: string | null,
): { kind: "refresh" } | { kind: "restart"; stage: RestartStage; slow: boolean } | null {
  const value = previewValue(search, stored);
  if (value === null) return null;
  if (value === "1") return { kind: "refresh" };
  const params = new URLSearchParams(search);
  const raw = params.get(UPDATE_STAGE_PARAM);
  const known = (RESTART_STAGES as readonly string[]).includes(raw ?? "");
  return {
    kind: "restart",
    stage: known ? (raw as RestartStage) : "ready",
    slow: params.get(UPDATE_SLOW_PARAM) === "1",
  };
}

/**
 * THE VERSION THE PREVIEW PRETENDS IS WAITING. A dev server is serving the
 * checkout's own version and has nothing newer on disk, so the restart dialog
 * previewed there says "Closing v0.5.53 and starting v0.5.53" — which is not
 * what any reader will ever see, and is the one sentence the whole flow turns
 * on. So the preview invents the DISAGREEMENT, exactly as the refresh preview
 * does and for the same reason: it is the only thing a dev machine cannot
 * supply, and everything else on the dialog stays real.
 *
 * A genuine pending update wins outright — if the disk really is ahead, the
 * preview shows the real number rather than a made-up one.
 */
export function previewInstalledVersion(version: string, installed: string): string {
  if (installed && installed !== version) return installed;
  const parts = version.split(".");
  const patch = Number(parts[parts.length - 1]);
  if (parts.length < 2 || !Number.isFinite(patch)) return version;
  return [...parts.slice(0, -1), String(patch + 1)].join(".");
}

/** The pretend press instant a preview hands the dialog. `slow` places it far
 *  enough back that the dialog's own clock reads the wait as overlong on the
 *  FIRST paint — the preview shows a face, it does not make you wait 25s for
 *  one — and a plain preview places it now, which freezes the ordinary
 *  sentence for as long as the page is left open. */
export function previewRequestedAt(slow: boolean, now: number): number {
  return slow ? now - RESTART_SLOW_MS - 1_000 : now;
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

  // THE CAP'S FALL-THROUGH (D4), and it is the FIRST thing asked. `gave-up`
  // means the stage machine stopped promising; what the page shows from then on
  // is whatever the SERVER says, with no stage attached. While the server is
  // still not answering that is the ordinary down card — the case the cap
  // exists for — and it has to outrank both doors below, because both stay open
  // through an outage: `update-restart` is the last thing the banner knew, and
  // the update store keeps its last value when its poll fails. Without this the
  // dialog would outlive the promise it made.
  //
  // WITH THE SERVER ANSWERING IT IS NOT THE DOWN CARD, and that is deliberate
  // (bugbot, PR #1214): a restart that did not take leaves the app demonstrably
  // running with the disk still ahead, so "fused-render isn't running" would be
  // a lie and the only way back to the button would be a page reload. The
  // ordinary doors below take it instead, and the dialog draws its button again
  // for `gave-up` exactly as it does for `ready` — a restart is worth offering
  // a second time.
  if (stage === "gave-up" && banner === "down") return "down";

  // A restart in flight holds the dialog on its own: from the first failed
  // probe onwards there is no /api/config left to ask, so neither door below
  // can stay open — and this is also what SUPPRESSES THE DOWN CARD (step 5),
  // because the dialog is returned before it.
  if (restartInFlight(stage) || installedReady) return "restart-dialog";
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
