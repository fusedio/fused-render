// ONE NOTIFICATION PER MODEL-SLOT-CONFLICT EPISODE (SPEC-model-slot-
// conflict.md §4) — detected off the SAME `useAiRuntime` poll `ModelsDock.tsx`
// and the sidebar's resident-model dot already share (no new transport; see
// `aiRuntime.ts`'s own header). A conflict standing across many polls in a
// row must raise exactly once, not on every tick it is still refused — the
// same "backfill vs news" shape `useTaskStatusNotify.ts` solves with its own
// `previous` map, reused here at the simpler grain of "have I already
// notified for this capability's CURRENT standoff".
//
// NOT THE SAME THREE CHOICES AS THE DOCK ROW (see `DECISIONS-model-slot-
// conflict.md`): `notify()`'s own `NotificationInput` carries only two action
// slots (`action`/`extraAction`), never the third `liveAction` slot
// `NotificationCard` itself supports — `ModelsDock.tsx`'s `ConflictRow` talks
// straight to `NotificationCard` and so gets all three, but the toast here
// cannot. Dismissing this toast is equivalent to keeping whichever model is
// currently resident: the supervisor already serves that one normally and
// only refuses the OTHER model's request-path loads, held or not — so the
// two slots offered here are "Keep `<the other model>`" (switch residency)
// and "Stop both" (`keep: null`). The dock's row stays the one place that
// also offers "Keep `<the current resident>`" explicitly.
//
// ONLY FRESHLY AUTO-DETECTED CONFLICTS RAISE A TOAST — a capability that
// enters `conflicts` already `held` (a `resolveModelConflict` call pre-
// empting detection, per `supervisor.resolve_conflict`'s own docstring) is
// the user's OWN action a moment ago, not news to tell them.
import { useEffect, useRef } from "react";
import { notify } from "@platform/lib/notifications";
import { repoName } from "@platform/lib/format";
import { resolveModelConflict } from "@platform/lib/api";
import { refreshAiRuntime, useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { IS_EMBED, IS_TOP_EMBED } from "@platform/lib/router";

/** The hook as a LEAF, mounted once in `App`'s tree — see
 *  `TaskStatusNotifier`'s own doc for why this shape (a subscribing hook at
 *  the app root re-renders everything on every poll; a leaf component holds
 *  the subscription alone and renders nothing). */
export function ModelConflictNotifier(): null {
  useModelConflictNotify();
  return null;
}

export function useModelConflictNotify(): void {
  const runtime = useAiRuntime();
  // Capabilities already notified for their CURRENT standoff — cleared the
  // moment that capability drops out of `conflicts` (resolved, released, or
  // cleared by a manual load/unload), so a later, genuinely new conflict on
  // the same capability notifies again.
  const notified = useRef<Set<string>>(new Set());

  useEffect(() => {
    // Matches `useTaskStatusNotify`'s own embed rule exactly (see its header
    // comment): a non-top embed pane forwards nothing of its own, a top
    // embed is a whole window with nobody to forward to.
    if (IS_EMBED && !IS_TOP_EMBED) return;
    const live = new Set<string>();
    for (const conflict of runtime.conflicts) {
      live.add(conflict.capability);
      if (notified.current.has(conflict.capability)) continue;
      // Always mark seen, held or not: a held entry is the user's own
      // action, not a fresh standoff to announce, but it still occupies
      // this capability's slot until cleared.
      notified.current.add(conflict.capability);
      if (conflict.held) continue;

      const label = capabilityLabel(conflict.capability);
      const other = conflict.models.find((m) => m !== conflict.resident) ?? conflict.models[0];
      const names = conflict.models.map(repoName).join(" and ");
      notify({
        title: `Two things are fighting over the ${label.toLowerCase()}: ${names}. Neither can finish loading.`,
        tone: "error",
        action: other
          ? {
              label: `Keep ${repoName(other)}`,
              onClick: () => {
                void resolveModelConflict(conflict.capability, other).then(refreshAiRuntime);
              },
            }
          : undefined,
        extraAction: {
          label: "Stop both",
          onClick: () => {
            void resolveModelConflict(conflict.capability, null).then(refreshAiRuntime);
          },
        },
      });
    }
    for (const capability of Array.from(notified.current)) {
      if (!live.has(capability)) notified.current.delete(capability);
    }
  }, [runtime.conflicts]);
}
