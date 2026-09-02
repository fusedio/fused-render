// The URL that lands on an app's entry page with the Claude pane open on a
// task's live run: the file's ordinary explorer URL, `_side=claude` for the
// pane (the same hop a task row makes, schedule-lib.explorerUrl), and `run` so
// the pane's boot re-attaches to the run instead of showing its landing page —
// with no session id yet there is nothing else it could adopt. `model`/`effort`
// ride along when a composer's pickers were used, so the pane's own pills open
// showing what the turn actually ran with and the NEXT turn keeps it; omitted
// when empty, since an empty param would beat the template's own detection.
//
// Platform-level because three surfaces make this hop — the Home hero after
// scaffolding, the app page's Migrate button, and the explorer topbar's — and
// an app may not import another app (check-boundaries).
import { urlForFsPath } from "@platform/lib/router";
import type { DefaultModel, SessionEffort } from "@platform/lib/api";

export function appLandingUrl(
  entryHtml: string,
  runId: string,
  model: DefaultModel = "",
  effort: SessionEffort = "",
): string {
  const params = new URLSearchParams({ _side: "claude", run: runId });
  if (model) params.set("model", model);
  if (effort) params.set("effort", effort);
  return urlForFsPath(entryHtml, "?" + params.toString());
}
