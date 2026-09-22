// SPEC-update-notifications.md — the self-update's whole UI, consolidated
// from five simultaneous surfaces (UpdateBadge, its Preferences rail dot,
// UpdateProgressCard, the restart-mode UpdateDialog, and a same-shaped
// Activity job row) down to the one rule the spec states up front:
//
//   Activity = progress. Notifications = decisions.
//
// The `sys:update:<version>` job already narrates PROGRESS in the Activity
// chip (untouched by this file). What used to be duplicated everywhere else
// was the two moments that need a DECISION — "fetch it?" and "restart for
// it?" — so this headless component raises exactly two notifications and
// nothing else. It renders `null`; it exists purely to bridge three read
// hooks (`useUpdateStatus`, `useRestartFlow`, `useRetainedNotifications`)
// into `notify()`/`replaceId` calls. Mounted once in the top document beside
// `NotificationHost` in `App.tsx`, behind the same `!IS_EMBED` guard as its
// siblings — a pane raising its own copy would pop the same decision twice.
//
// NOT DRIVEN FROM `update-status.ts`'s `set()` (the spec is explicit about
// this): the restart narration below needs `useRestartFlow()`, which is a
// hook, and a module-store subscriber fired from inside `update-status.ts`
// would run once per pane rather than once for the top document.
import { useEffect, useRef } from "react";

import { updateInstall, type UpdateStatus } from "@platform/lib/api";
import {
  dismissNotification,
  notify,
  useRetainedNotifications,
  type NotificationInput,
} from "@platform/lib/notifications";
import {
  restartInFlight,
  restartStageLabel,
  type RestartStage,
} from "@platform/lib/restart-flow";
import { requestRestart, restartStageNow, useRestartFlow } from "@platform/lib/restart-store";
import { pokeUpdateStatus, setUpdateStatus, useUpdateStatus } from "@platform/lib/update-status";

// `sessionStorage`, not `localStorage` (spec, Notification #2's own
// paragraph): a "later" dismissal must survive a reload in the SAME window
// (the whole point of deferring) but must not survive the app actually
// quitting — the agreed "re-offers on next launch" semantic. A key collision
// with anything else is not a concern; this is the only writer.
const RESTART_DISMISSED_KEY = "fused_update_restart_dismissed";

function wasRestartDismissed(version: string): boolean {
  try {
    return sessionStorage.getItem(RESTART_DISMISSED_KEY) === version;
  } catch {
    // Private window, quota, embed without storage access — treat as "not
    // dismissed" rather than throw; the worst case is one extra re-raise.
    return false;
  }
}

function recordRestartDismissed(version: string): void {
  try {
    sessionStorage.setItem(RESTART_DISMISSED_KEY, version);
  } catch {
    // Same as above — losing the "later" memory is not worth crashing over.
  }
}

// The Download action's body — VERBATIM from the deleted `UpdateBadge.tsx`'s
// own `install()` (git history, pre-SPEC-update-notifications), comment
// included: the server force-rechecks the manifest before it installs, and
// always installs the NEWEST version it finds — the one this card showed, or
// a newer one published since. It never installs anything older than what
// was on screen.
async function install(status: UpdateStatus): Promise<void> {
  try {
    setUpdateStatus(await updateInstall(status.latest_version));
  } catch {
    // Fall through — the re-armed poll picks up the real state.
  }
  pokeUpdateStatus();
}

// Stage copy for the in-flight card. Three of the four stages reuse
// `restart-flow.ts`'s own `restartStageLabel` verbatim (the spec: "reuse the
// vocabulary UpdateDialog uses today rather than inventing new words") — but
// `back` is the one exception: the OLD restart-mode `UpdateDialog` (git
// history, `restartBody`) said `Back on v<installed> — reloading…`, naming
// the version, and the spec calls that out by name as the wording to keep.
// `restartStageLabel`'s own generic "Reconnected — fused-render is back." is
// still correct for the OLD dialog's other reader (none — that mode is
// deleted) but is not what this card should say, so this function overrides
// only that one stage rather than changing `restartStageLabel` itself, which
// has no other caller left to disagree with but is not this component's file
// to redefine.
function inFlightLabel(stage: RestartStage, installedVersion: string | null): string {
  if (stage === "back") {
    return installedVersion ? `Back on v${installedVersion} — reloading…` : "Back — reloading…";
  }
  return restartStageLabel(stage);
}

export default function UpdateNotifier(): null {
  const status = useUpdateStatus();
  const flow = useRestartFlow();
  const retained = useRetainedNotifications();

  // The Download/Restart-ready card ids, kept across renders so a repeat
  // `notify()` call updates the SAME row (`replaceId`) instead of stacking a
  // second one every time `status` changes shape without actually changing
  // state. `undefined` means "nothing of this kind is currently up".
  const downloadIdRef = useRef<number | undefined>(undefined);
  // Notification #2's id ALSO doubles as the in-flight restart card's id —
  // the spec's "the same card narrates it, in place, via notify(input,
  // replaceId)": pressing "Restart now" does not raise a new card, it
  // repaints this one.
  const restartIdRef = useRef<number | undefined>(undefined);
  // Which version the card currently up ACTUALLY shows — guards the effect
  // below against re-notifying every render. `notify()`'s own `replaceId`
  // path returns a fresh `retained` ARRAY reference every call (`.map()`), so
  // an effect that reacted to "the card isn't showing this content yet" by
  // simply calling `notify()` unconditionally would see its own write come
  // back as a changed `retained` prop and fire again — forever, pegging the
  // tab (this was caught by this file's own test hanging in CI-grade CPU).
  // Only `notify()` again when something actually needs to change: a fresh
  // version, or the row having been evicted out from under it.
  const restartRaisedForRef = useRef<string | null>(null);

  // ---- Notification #1 — Download / failed -------------------------------
  useEffect(() => {
    if (!status) return;
    if (status.state === "available" && !status.check_only) {
      const input: NotificationInput = {
        title: `Update available${status.latest_version ? ` — v${status.latest_version}` : ""}`,
        detail: status.latest_version ? `v${status.latest_version} is ready to download.` : undefined,
        tier: "attention",
        action: {
          label: "Download",
          onClick: () => {
            // ONE NARRATOR AT A TIME (spec): dismiss the decision the instant
            // it's made — Activity's own `sys:update:` job row takes over the
            // story from here. Replacing this card with a "Downloading…" one
            // would be exactly the duplication SPEC-update-notifications.md
            // removes.
            if (downloadIdRef.current !== undefined) dismissNotification(downloadIdRef.current);
            void install(status);
          },
        },
      };
      downloadIdRef.current = notify(input, downloadIdRef.current);
      return;
    }
    if (status.state === "error") {
      downloadIdRef.current = notify(
        {
          title: "Update failed · Try again",
          detail: status.error ?? undefined,
          tone: "error",
          tier: "attention",
          action: { label: "Try again", onClick: () => void install(status) },
        },
        downloadIdRef.current,
      );
      return;
    }
    // Any other state (idle/checking/installing/installed, or `check_only`
    // itself flipping true) means there is no download decision pending any
    // more — clear whatever this card was showing rather than let a stale
    // "Update available" sit for a status that has already moved on.
    if (downloadIdRef.current !== undefined) {
      dismissNotification(downloadIdRef.current);
      downloadIdRef.current = undefined;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `status` is a
    // fresh object every poll tick (update-status.ts's own `set()` doc); the
    // effect body reads it by field, so re-running once per genuine state
    // change (JSON.stringify-compared upstream) is already the right cadence.
  }, [status]);

  // ---- Notification #2 — Restart ready (before a restart is requested) ---
  useEffect(() => {
    // Once a restart is actually in flight (or over), this card belongs to
    // the in-flight effect below — it owns `restartIdRef` from here on.
    if (flow.stage !== "ready") return;
    if (!status || status.state !== "installed") return;
    const version = status.latest_version;
    if (version && wasRestartDismissed(version)) return;

    // SURVIVE EVICTION (spec): `capRetained` drops the OLDEST row past
    // `MAX_RETAINED = 5`, so five unrelated notifications can silently push
    // this one out from under a user who never dismissed it. If our id is no
    // longer in the retained list, treat it as gone and re-raise fresh
    // (`replaceId: undefined`) rather than pass an id `notify()` will not
    // recognize as either the live popup or a retained row.
    const stillRetained =
      restartIdRef.current !== undefined && retained.some((n) => n.id === restartIdRef.current);

    // Nothing to do: the card is already up, showing this exact version.
    // Skipping here is what breaks the feedback loop described above —
    // without it, `notify()`'s own fresh `retained` reference would re-arm
    // this effect (dep: `retained`) forever.
    if (stillRetained && restartRaisedForRef.current === version) return;

    const replaceId = stillRetained ? restartIdRef.current : undefined;

    restartIdRef.current = notify(
      {
        title: "Update ready",
        detail: version ? `v${version} installed — restart to start using it.` : undefined,
        tier: "attention",
        action: { label: "Restart now", onClick: () => requestRestart() },
        extraAction: {
          label: "Later",
          onClick: () => {
            if (version) recordRestartDismissed(version);
            if (restartIdRef.current !== undefined) dismissNotification(restartIdRef.current);
            restartRaisedForRef.current = null;
          },
        },
      },
      replaceId,
    );
    restartRaisedForRef.current = version ?? null;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, flow.stage, retained]);

  // ---- The restart, in flight ---------------------------------------------
  // Repaints `restartIdRef`'s card with the current stage. Fires once per
  // real stage transition (the `flow.stage` dependency) AND on its own timer
  // below — see that effect's header comment for why a stage-change trigger
  // alone is not enough to keep the card on screen.
  useEffect(() => {
    if (flow.stage === "ready") return; // nothing requested yet — the effect above owns the card
    if (flow.stage === "gave-up") {
      restartIdRef.current = notify(
        {
          title: "fused-render didn't come back",
          detail: "Try restarting again from here, or reopen the app yourself.",
          tone: "error",
          tier: "attention",
          action: { label: "Restart now", onClick: () => requestRestart() },
        },
        restartIdRef.current,
      );
      return;
    }
    restartIdRef.current = notify(
      {
        title: "Restarting fused-render",
        detail: inFlightLabel(flow.stage, status?.latest_version ?? null),
        tier: "attention",
        // DROP THE ✕ WHILE IN FLIGHT (spec, `restartInFlight` is the exact
        // predicate it names) — there is nothing left for "later" to defer
        // once the app is actually going down for this.
        dismissible: !restartInFlight(flow.stage),
      },
      restartIdRef.current,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flow.stage]);

  // KEEPING IT ON SCREEN: a notification popup auto-expires after
  // `JOB_POPUP_VISIBLE_MS` (~2.65s) unless re-armed. The spec's own mechanism
  // is "restart-store already ticks at TICK_MS = 1000, so re-notifying on
  // each tick holds the card open with no change to notifications.ts" — but
  // `restart-store.ts`'s `dispatch()` only calls `publishView()` when `stage`/
  // `requestedAt`/`fails`/`before` actually change BY VALUE (its own
  // byte-identity early return), and an ordinary tick that is not yet at the
  // `RESTART_GIVE_UP_MS` cap changes none of those — so `useRestartFlow()`
  // does NOT necessarily re-render on every single tick while a stage merely
  // continues (e.g. several seconds of "Reconnecting…" between probes). Tying
  // the keep-alive re-notify to `flow.stage`'s OWN change (the effect above)
  // would let the popup's exit timer win during exactly that gap. So this
  // effect runs its OWN interval, independent of the store's render cadence,
  // and reads the stage fresh off the non-reactive `restartStageNow()` on
  // every tick — satisfying the spec's actual requirement ("keep it on
  // screen") without asking `notifications.ts` for a `sticky` flag, which is
  // the part of the spec's intent that does hold.
  useEffect(() => {
    if (!restartInFlight(flow.stage)) return;
    const id = setInterval(() => {
      const stage = restartStageNow();
      if (!restartInFlight(stage)) return; // resolved between ticks — the stage-change effect above already repainted it
      restartIdRef.current = notify(
        {
          title: "Restarting fused-render",
          detail: inFlightLabel(stage, status?.latest_version ?? null),
          tier: "attention",
          dismissible: false,
        },
        restartIdRef.current,
      );
    }, 1000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [restartInFlight(flow.stage)]);

  return null;
}
