// Onboarding PROGRESS — one store for every surface that says how far setup
// has got: the sidebar's "Setup 60%" row, the wizard's top-bar pills, and the
// steps themselves, which are the writers.
//
// THE MODEL. Every wizard step is a STAGE with a status the step decides for
// itself, from the facts it already checks to draw its own rows:
//
//   stage    pending                partial                      complete
//   about    not yet opened         —                            opened once
//   claude   not installed/unknown  runs, but outdated/signed    installed, new
//                                   out/unknown                  enough, signed in
//   fda      not granted            granted, relaunch pending    granted
//   models   none fetched, none     none here, one or more       one or more
//            downloading            downloading                  models here
//   app      nothing built          —                            composer made an
//                                                                app, or a showcase
//                                                                app was opened
//
// plus `n/a` for a stage this machine does not have (Disk Access off macOS,
// Models when no engine can serve anything) — it leaves the denominator, so
// a Linux user is not stuck at 80% over a pane they cannot open.
//
// The status is written to the server (shell/onboarding.py `stages`) with a
// free-form `meta` note for reference, and read back from `config.onboarding`
// like the flags. Server-side, not localStorage: every port is a new origin
// (the server module's docstring). The server OVERRULES the stored status for
// the stages it can see cheaply (FDA, first app, Claude from its health cache),
// so a grant made from the Home strip moves the meter too.
//
// THE PERCENTAGE: over the stages that count (not `n/a`), complete is 1,
// partial is ½, pending is 0, rounded. A stage nobody has written yet is
// pending — a fresh install starts at 0%.
//
// Why a store and not the boot config: `config` is fetched ONCE (main.tsx),
// and the sidebar is unmounted while the wizard is up — so the row would come
// back showing whatever boot said. Every stage write replaces the snapshot
// with the server's reply, and a subscriber mounting re-reads once. Same
// `useSyncExternalStore` shape as platform/lib/fda.ts.
import { useSyncExternalStore } from "react";

import {
  getOnboarding,
  setOnboardingStage,
  type Config,
  type OnboardingStage,
  type OnboardingStageStatus,
  type OnboardingState,
} from "@platform/lib/api";

import { ONBOARDING_PATH } from "./state";

export type StageStatus = OnboardingStageStatus;
export type Stages = Record<string, OnboardingStage>;

/** The stage ids, in wizard order — the server's closed set (STEPS). */
export const STAGE_IDS = ["about", "claude", "fda", "models", "app"] as const;
export type StageId = (typeof STAGE_IDS)[number];

let snapshot: OnboardingState | null = null;
const listeners = new Set<() => void>();
let inflight: Promise<void> | null = null;

function emit() {
  for (const l of listeners) l();
}

function set(next: OnboardingState | null) {
  if (next === snapshot) return;
  snapshot = next;
  emit();
}

// EVERY REPLY IS THE WHOLE STATE, AND REPLIES DO NOT ARRIVE IN ORDER. Two
// stage POSTs in one tick, or the sidebar's mount-time GET racing the last
// write the wizard made on its way out — whichever lands LAST would win, and a
// stale one would put a stage back to what it was (bugbot: a showcase open is
// the sharp case, since the server cannot observe it and nothing would ever
// correct the loss). So a reply is MERGED, not adopted:
//   * flags (completed/dismissed/opened) only ever gain a value — a stamp the
//     server has made cannot be un-made by an older reply;
//   * each stage keeps whichever record is NEWER by `updated_at`. An optimistic
//     local write stamps `now`; the server's own stamp for that write is later
//     still, so the reply replaces it, while a reply from BEFORE the write
//     (older stamp, or no stage at all) leaves the local record standing.
//     A server observation carries the stored record's stamp (or null), which
//     is exactly "not newer than anything the wizard has written since".
function merge(reply: OnboardingState): void {
  const cur = snapshot;
  if (!cur) return set(reply);
  const stages: Stages = { ...(reply.stages ?? {}) };
  for (const [id, mine] of Object.entries(cur.stages ?? {})) {
    const theirs = stages[id];
    if (!theirs || (mine.updated_at ?? -1) > (theirs.updated_at ?? -1)) stages[id] = mine;
  }
  set({
    ...reply,
    completed_at: reply.completed_at ?? cur.completed_at,
    dismissed_at: reply.dismissed_at ?? cur.dismissed_at,
    opened_at: reply.opened_at ?? cur.opened_at,
    stages,
  });
}

/** Adopt a snapshot some other call brought back (the wizard's `opened`
 *  write, complete, dismiss). Merged, like every reply (see `merge`). */
export function setProgress(next: OnboardingState): void {
  merge(next);
}

/** Seed from the boot config, once — a first paint that does not wait. */
export function seedProgress(config: Config): void {
  if (snapshot === null && config.onboarding) set(config.onboarding);
}

/** Re-read the server. A failed fetch keeps what we have. */
export function refreshProgress(): Promise<void> {
  if (inflight) return inflight;
  inflight = getOnboarding()
    .then((s) => merge(s))
    .catch(() => undefined)
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

export function getProgress(): OnboardingState | null {
  return snapshot;
}

// WHEN THE STORE RE-READS. The server overrules stored statuses with what it
// can observe (a grant, a folder under local/, a cached model), but only on a
// read — and a store that read once at mount would show a download finished
// an hour ago as still pending. So, while anyone is subscribed: on every
// return to the tab (the DownloadManager's own pattern), and whenever the
// Activity dock sees a job reach a terminal state (`noteProgressMayHaveMoved`,
// called from ActivityDock). No timer of its own: the dock's poll already runs at
// the right cadence, and finishing a job is the moment a stage can change.
const onVisible = () => {
  if (document.visibilityState === "visible") void refreshProgress();
};

export function subscribeProgress(cb: () => void): () => void {
  listeners.add(cb);
  if (listeners.size === 1) {
    void refreshProgress();
    document.addEventListener("visibilitychange", onVisible);
  }
  return () => {
    listeners.delete(cb);
    if (listeners.size === 0) document.removeEventListener("visibilitychange", onVisible);
  };
}

/** Something that could move a stage just happened elsewhere in the shell (a
 *  download finished, a task ended). Re-read if anyone is looking; a store
 *  with no subscriber re-reads at its next mount anyway. */
export function noteProgressMayHaveMoved(): void {
  if (listeners.size > 0) void refreshProgress();
}

export function useOnboardingState(): OnboardingState | null {
  return useSyncExternalStore(subscribeProgress, getProgress, getProgress);
}

// What each stage last wrote — so a step reporting the same status on every
// poll (Models, every few seconds) writes once, not on every tick. Keyed by
// stage; the meta is not compared (a note may change without a status change,
// and re-sending it is harmless but is not what this dedupes).
const lastWritten = new Map<string, StageStatus>();

/** A step reports its status. Fire-and-forget; the server's reply is the new
 *  snapshot. `force` re-sends even when the status has not changed (a step
 *  with a NEW note to leave). */
export function reportStage(
  stage: StageId,
  status: StageStatus,
  meta?: Record<string, unknown>,
  force = false,
): void {
  if (!force && lastWritten.get(stage) === status && snapshot?.stages?.[stage]?.status === status) return;
  lastWritten.set(stage, status);
  // Optimistic: the pills should move as the step does, not a round trip later.
  if (snapshot) {
    const prev = snapshot.stages?.[stage];
    set({
      ...snapshot,
      stages: {
        ...(snapshot.stages ?? {}),
        [stage]: { status, meta: { ...(prev?.meta ?? {}), ...(meta ?? {}) }, updated_at: Date.now() / 1000 },
      },
    });
  }
  setOnboardingStage(stage, status, meta).then(
    (s) => merge(s),
    () => undefined,
  );
}

export function stageStatus(stages: Stages | undefined, id: string): StageStatus {
  return stages?.[id]?.status ?? "pending";
}

const WEIGHT: Record<StageStatus, number> = { pending: 0, partial: 0.5, complete: 1, "n/a": 0 };

/** The meter. `counted` is the denominator (stages that are not `n/a`);
 *  `percent` is rounded 0..100, and 100 only when every counted stage is
 *  complete. `ids` limits the count to the stages a caller has (the wizard's
 *  visible steps); default is every stage. */
export function progressPercent(
  stages: Stages | undefined,
  ids: readonly string[] = STAGE_IDS,
): { percent: number; counted: number; complete: number; partial: number } {
  let counted = 0;
  let sum = 0;
  let complete = 0;
  let partial = 0;
  for (const id of ids) {
    const s = stageStatus(stages, id);
    if (s === "n/a") continue;
    counted += 1;
    sum += WEIGHT[s];
    if (s === "complete") complete += 1;
    if (s === "partial") partial += 1;
  }
  if (counted === 0) return { percent: 0, counted, complete, partial };
  const raw = (sum / counted) * 100;
  // Never round up to 100 over a stage that is only half done.
  const percent = complete === counted ? 100 : Math.min(99, Math.round(raw));
  return { percent, counted, complete, partial };
}

/** The first stage that still needs doing (not complete, not `n/a`), in
 *  wizard order — where a click on the meter should land. Null when nothing
 *  is left. */
export function firstOpenStage(stages: Stages | undefined): StageId | null {
  for (const id of STAGE_IDS) {
    const s = stageStatus(stages, id);
    if (s !== "complete" && s !== "n/a") return id;
  }
  return null;
}

/** The wizard's URL, open on the first step still to do — what the sidebar
 *  meter, Help › Setup wizard and the boot auto-show all land on. This
 *  REPLACED the stored "last open step" resume point: where the user last
 *  happened to be is a worse answer than what is left to do, and the stages
 *  already know that. Nothing left (or nothing known) opens at the top. */
export function onboardingUrl(stages: Stages | undefined): string {
  const open = firstOpenStage(stages);
  return open && open !== STAGE_IDS[0] ? `${ONBOARDING_PATH}?step=${encodeURIComponent(open)}` : ONBOARDING_PATH;
}

/** Whether the sidebar shows the meter at all. Not before anything has been
 *  written (an install upgrading into this build was seeded `completed` with
 *  no stages — it must not wake to "Setup 0%"), and not once it reads 100
 *  (the wizard stays one click away under Help › Setup wizard). */
export function meterVisible(state: OnboardingState | null): boolean {
  if (!state?.stages) return false;
  // `updated_at` is stamped by a WIZARD write only; the server's own
  // observations leave it null (shell/onboarding.py `_observe`).
  const written = Object.values(state.stages).some((s) => s.updated_at != null);
  if (!written) return false;
  return progressPercent(state.stages).percent < 100;
}
