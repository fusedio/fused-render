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
import { dismissNotification, notify } from "@platform/lib/notifications";
import { repoName, joinNames } from "@platform/lib/format";
import { resolveModelConflict, actionErrorMessage } from "@platform/lib/api";
import { publishAiRuntime, useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { IS_EMBED, IS_TOP_EMBED } from "@platform/lib/router";
import { requestOpenSection } from "@platform/lib/statusChip";

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
  // Capabilities already notified for their CURRENT standoff — the toast's
  // own notification id (`null` for a held entry, which marks itself seen
  // WITHOUT ever raising a toast — see below) plus the exact contender set
  // the toast was raised for. Cleared the moment that capability drops out
  // of `conflicts` (resolved, released, or cleared by a manual load/unload),
  // so a later, genuinely new conflict on the same capability notifies
  // again. A THIRD (or later) caller joining a standoff already announced
  // changes `models` without ever touching `held`, so it is tracked
  // separately from the id rather than folded into the `already !== undefined`
  // short-circuit below.
  const notified = useRef<Map<string, { id: number | null; models: string }>>(new Map());

  useEffect(() => {
    // Matches `useTaskStatusNotify`'s own embed rule exactly (see its header
    // comment): a non-top embed pane forwards nothing of its own, a top
    // embed is a whole window with nobody to forward to.
    if (IS_EMBED && !IS_TOP_EMBED) return;
    const live = new Set<string>();
    for (const conflict of runtime.conflicts) {
      live.add(conflict.capability);
      const modelsKey = conflict.models.join("\u0000");
      const already = notified.current.get(conflict.capability);
      if (already !== undefined) {
        // A standing toast must not survive the capability turning held —
        // a resolve (this tab's own, or another one's) can land without the
        // capability ever leaving `conflicts` at all, just flipping `held`
        // and its resident. Withdraw the stale toast (if any) and re-mark
        // this capability seen as held, exactly like a FRESH held entry
        // does below — no new toast for it either.
        if (conflict.held) {
          if (already.id != null) dismissNotification(already.id);
          notified.current.set(conflict.capability, { id: null, models: modelsKey });
          continue;
        }
        // The contender set grew (or shrank) since this toast was raised —
        // withdraw it and fall through to raise a fresh one naming the
        // current set, rather than leaving a stale "A and B" toast up while
        // a third caller is also now being refused.
        if (already.models === modelsKey) continue;
        if (already.id != null) dismissNotification(already.id);
      } else if (conflict.held) {
        // Always mark seen, held or not: a held entry is the user's own
        // action, not a fresh standoff to announce, but it still occupies
        // this capability's slot until cleared.
        notified.current.set(conflict.capability, { id: null, models: modelsKey });
        continue;
      }

      const label = capabilityLabel(conflict.capability);
      const requesterOf = (m: string) => conflict.requesters?.[m] || repoName(m);
      const other = conflict.models.find((m) => m !== conflict.resident) ?? conflict.models[0];
      const names = joinNames(conflict.models.map(requesterOf));
      // A refused choice must say so rather than look like the click did
      // nothing — the same non-silent-failure contract `ModelsDock.tsx`'s
      // `ConflictRow` already keeps for the identical actions.
      const onFailure = (label_: string) => (e: unknown) => {
        notify({
          title: actionErrorMessage(e, `Could not ${label_} — check your connection and retry.`),
          tone: "error",
        });
      };
      // The resident keeps answering normally throughout — only the OTHER
      // contenders' loads are refused — so the title says who stays loaded
      // instead of claiming nobody is making progress (B7).
      const residentName = conflict.resident ? repoName(conflict.resident) : null;
      const title = residentName
        ? `${names} are fighting over the ${label.toLowerCase()}: ${residentName} stays loaded, the rest can't take over.`
        : `${names} are fighting over the ${label.toLowerCase()}.`;
      // Only two action slots exist here (see this file's own header
      // comment) — fine for exactly two contenders ("Keep <other>"/"Stop
      // both"), but a third makes "Keep <other>" an arbitrary, unexplained
      // pick among several. Three or more instead gets "Choose…" (opens the
      // dock, which has a Keep action per contender) and "Stop all".
      const multiway = conflict.models.length > 2;
      const id = notify({
        title,
        tone: "error",
        action: multiway
          ? { label: "Choose…", onClick: () => requestOpenSection("models") }
          : other
            ? {
                label: `Keep ${repoName(other)}`,
                onClick: () => {
                  resolveModelConflict(conflict.capability, other)
                    .then(publishAiRuntime)
                    .catch(onFailure(`keep ${repoName(other)}`));
                },
              }
            : undefined,
        extraAction: {
          label: multiway ? "Stop all" : "Stop both",
          onClick: () => {
            resolveModelConflict(conflict.capability, null)
              .then(publishAiRuntime)
              .catch(onFailure(`stop ${label.toLowerCase()}`));
          },
        },
      });
      notified.current.set(conflict.capability, { id, models: modelsKey });
    }
    for (const [capability, entry] of Array.from(notified.current)) {
      if (live.has(capability)) continue;
      // Withdraw the live toast along with its bookkeeping — a conflict
      // resolved, released, or cleared by a manual load/unload must not
      // leave its "Keep"/"Stop both" card sitting on screen offering
      // choices that no longer apply.
      if (entry.id != null) dismissNotification(entry.id);
      notified.current.delete(capability);
    }
  }, [runtime.conflicts]);
}
