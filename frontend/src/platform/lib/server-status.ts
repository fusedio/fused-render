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
  | "slow"
  | "down"
  | "reconnected"
  | "update-refresh"
  | "update-restart";

/** WHY a probe failed (SPEC §50, D1). Before this every failure collapsed to
 *  `{ok: false}`, so "the server is gone" and "the server is alive but its
 *  event loop is wedged for 4 s" read the same — and they want different
 *  cards and different fixes:
 *    timeout    — the request went out and nothing came back inside the
 *                 probe's abort window (a busy or blocked server).
 *    refused    — the fetch itself failed: nothing listening, or a network
 *                 error before any response.
 *    http-5xx   — the server answered, and said it was broken.
 *    http-other — any other non-2xx (a proxy, a 404 from an older server).
 *    parse      — a 2xx whose body was not the JSON the probe expects. */
export type ProbeFailKind = "timeout" | "refused" | "http-5xx" | "http-other" | "parse";

export interface StatusState {
  banner: ServerBanner;
  /** Consecutive failed probes in the current streak. */
  fails: number;
  /** Last served version a healthy probe reported; undefined before one. */
  served?: string;
  /** Last healthy probe's server boot id (/api/health `boot_id`). A change
   *  between two healthy probes is a restart this tab never saw fail — the
   *  only way a sub-5 s same-version restart is noticed at all. */
  bootId?: string;
  /** When (ms epoch) the last healthy probe landed — the lower bound of a
   *  restart that no probe saw fail. */
  lastOkAt?: number;
  /** When (ms epoch) the current failure streak began; undefined when healthy. */
  firstFailAt?: number;
  /** The kinds of the current failure streak, oldest first. */
  failKinds: ProbeFailKind[];
  /** The last ≤ LATENCY_RING healthy probe latencies (ms), oldest first — sent
   *  with an outage record so a slow-then-dead server shows its slope. */
  latencies: number[];
}

export interface ProbeResult {
  ok: boolean;
  /** Set on a failed probe — see `ProbeFailKind`. */
  kind?: ProbeFailKind;
  /** The answering server's boot id (/api/health). */
  bootId?: string;
  /** Wall time of the probe request, ms. */
  latencyMs?: number;
  /** Running server's version, from /api/config — fetched far less often
   *  than the /api/health probe and merged in by the component. */
  version?: string;
  /** Version installed on disk (bundle Info.plist); null when unpackaged. */
  installedVersion?: string | null;
  /** True when the server is a dev.sh run (/api/config `dev`). */
  dev?: boolean;
}

/** One outage as this tab saw it — the body of POST /api/health/outage, less
 *  the fields only the component knows (`visible`, `page`, `latencies_ms`).
 *  Times are epoch SECONDS, the server's own unit (fused_render/health.py). */
export interface OutageRecord {
  t_down: number;
  /** Null on the partial record a tab sends while going away mid-outage. */
  t_up: number | null;
  strikes: number;
  kinds: ProbeFailKind[];
  boot_id_before: string | null;
  boot_id_after: string | null;
  recovered: boolean;
}

/** Three, not two: two 4 s timeouts in a row are a busy server more often
 *  than a dead one, and the first misses now draw the quieter "slow" line
 *  rather than the "isn't running" card. */
export const FAIL_THRESHOLD = 3;

/** How many healthy latencies the state keeps. */
export const LATENCY_RING = 50;

const toSec = (ms: number) => Math.round(ms) / 1000;

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
  return { banner: "hidden", fails: 0, failKinds: [], latencies: [] };
}

/** The partial record a tab sends when it goes away mid-outage (`pagehide`):
 *  no `t_up`, not recovered. Null when nothing is failing. */
export function pendingOutage(state: StatusState): OutageRecord | null {
  if (state.fails < 1 || state.firstFailAt === undefined) return null;
  return {
    t_down: toSec(state.firstFailAt),
    t_up: null,
    strikes: state.fails,
    kinds: state.failKinds,
    boot_id_before: state.bootId ?? null,
    boot_id_after: null,
    recovered: false,
  };
}

export function reduceProbe(
  state: StatusState,
  probe: ProbeResult,
  buildVersion: string,
  now: number = Date.now(),
): { state: StatusState; reload: boolean; outage?: OutageRecord } {
  if (!probe.ok) {
    const kind: ProbeFailKind = probe.kind ?? "refused";
    const fails = state.fails + 1;
    // A timeout below the threshold is "slow", not "down": the request went
    // out and the server has not answered YET. Any other kind below the
    // threshold leaves the banner where it was — one refused probe is as
    // often a waking laptop's network as a dead server. "slow" only ever
    // replaces a banner that says nothing (hidden) or a transient one
    // (reconnected, or slow itself): the update dialogs are statements
    // about a version mismatch that a busy probe does not change, and a
    // blocking refresh dialog must not blink out for one poll (bugbot,
    // PR #1399).
    const quiet = state.banner === "hidden" || state.banner === "slow" || state.banner === "reconnected";
    const banner: ServerBanner =
      fails >= FAIL_THRESHOLD ? "down" : kind === "timeout" && quiet ? "slow" : state.banner;
    return {
      state: {
        ...state,
        banner,
        fails,
        firstFailAt: state.firstFailAt ?? now,
        failKinds: [...state.failKinds, kind],
      },
      reload: false,
    };
  }

  const wasDown = state.banner === "down";
  const served = probe.version;
  const installed = probe.installedVersion ?? null;
  const latencies =
    probe.latencyMs !== undefined
      ? [...state.latencies, probe.latencyMs].slice(-LATENCY_RING)
      : state.latencies;
  // A boot id moving between two healthy probes is a restart no probe saw
  // fail (faster than one poll, or the tab was hidden through it).
  const bootChanged =
    state.bootId !== undefined && probe.bootId !== undefined && probe.bootId !== state.bootId;

  // THE OUTAGE RECORD. Even a single missed probe is one row: the server
  // dedups nothing and a row is cheap, while a missing row is exactly the
  // blind spot this exists to close. A boot-id change with no failed probe at
  // all is a row too (strikes 0), bounded below by the last healthy probe.
  let outage: OutageRecord | undefined;
  if (state.fails >= 1) {
    outage = {
      t_down: toSec(state.firstFailAt ?? now),
      t_up: toSec(now),
      strikes: state.fails,
      kinds: state.failKinds,
      boot_id_before: state.bootId ?? null,
      boot_id_after: probe.bootId ?? null,
      recovered: true,
    };
  } else if (bootChanged) {
    outage = {
      t_down: toSec(state.lastOkAt ?? now),
      t_up: toSec(now),
      strikes: 0,
      kinds: [],
      boot_id_before: state.bootId ?? null,
      boot_id_after: probe.bootId ?? null,
      recovered: true,
    };
  }

  const next = (banner: ServerBanner): StatusState => ({
    banner,
    fails: 0,
    served: served ?? state.served,
    bootId: probe.bootId ?? state.bootId,
    lastOkAt: now,
    firstFailAt: undefined,
    failKinds: [],
    latencies,
  });
  const done = (banner: ServerBanner, reload = false) =>
    outage ? { state: next(banner), reload, outage } : { state: next(banner), reload };

  if (served && installed && installed !== served) {
    return done("update-restart");
  }
  if (served && served !== buildVersion) {
    // A version can only change under a process swap, so seeing it move —
    // either across a "down" gap or between two healthy probes (a restart
    // faster than the down threshold, or one that happened while the tab was
    // hidden) — means the server restarted updated. The user asked for that
    // (or was blocked by it), so reload without asking; views are URL-synced.
    const transitioned = wasDown || (state.served !== undefined && state.served !== served);
    if (transitioned) return done("reconnected", true);
    return done("update-refresh");
  }
  // A same-version restart (the boot id moved, the version did not) gets the
  // reconnected line too, so the reader learns the app restarted under them
  // even when no probe failed long enough to say so.
  if (wasDown || bootChanged) return done("reconnected");
  // "reconnected" is dismissed by the component's timer, but never held past
  // the next probe: an in-flight probe can write a stale "reconnected" back
  // AFTER the timer fired, with no new timer armed (wasDown is false) — held
  // here, that card would stick until the next outage. "slow" clears here too.
  return done("hidden");
}

// ---- what the banner actually PUTS ON SCREEN ------------------------------
//
// `reduceProbe` above answers "what did that probe mean". This answers the
// second question, which has three more inputs than a probe — the dev
// suppression, the shared update store, and whether a restart is in flight —
// and which two surfaces have to agree on: a "down" card under a dialog that
// says the app is coming back is the exact contradiction this flow exists to
// remove. Pure, so the agreement is a test rather than a reading of JSX.

export type BannerSurface = "none" | "slow" | "down" | "reconnected" | "refresh-dialog";

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
  // "slow" sits behind the same restart-in-flight door as "down" above: a
  // restart's first missed probe is the restart working, not a slow server.
  if (banner === "slow") return "slow";
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
