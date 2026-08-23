// What is left of the preview pane's row-mode machinery, now that
// `ListingPreviewPane` no longer previews a selected row's own templates at
// all (D443) — it shows the OPEN FOLDER's companions (`claude`/`git`/`mcp`)
// or a plain fallback hint instead, neither of which is a question this
// module ever answered.
//
// `paneModeList` SURVIVES, not as a relic but as a still-real, still-used
// computation: `mode-labels.test.ts` calls it to check that no registry key
// can offer two indistinguishably-named modes side by side (a question about
// the REGISTRY, which has nothing to do with whether any live component still
// calls the function that answers it). `activePaneMode`, `paneOpenTarget`/
// `PaneOpenTarget` and `paneOpenAction`/`PaneOpenAction` — the row-mode
// default and the pane's expand button, both genuinely dead once the row is
// gone — are deleted outright, with their `pane-modes.test.ts` coverage,
// rather than kept pinning behaviour nothing exercises any more.
//
// `paneChatOnly` is the one function still wired into the running app
// (ListingPreviewPane's companion-iframe branch).
import type { TemplateEntry } from "@platform/lib/api";
import { isModeVisible } from "@platform/lib/mode-visibility";

export interface PaneModeInput {
  // The target's resolved template list (already sentinel-filtered, PT-12).
  templates: TemplateEntry[];
  // Gate verdicts (CT-12); null while still resolving. Visibility is NOT this
  // module's call — it is `lib/mode-visibility`'s one policy for every mode
  // surface: an entry hides only on an explicit `false`.
  conditions: Record<string, boolean> | null;
  isDir: boolean;
}

// The pane's mode list, in pane priority order (first = default): the template
// system's own order, untouched — `_listing` stays exactly where the registry
// ranks it, and is dropped only for a FILE (no slot for a listing of a file).
//
// **EVERY folder reaches this module, and what it gets back leads with `claude`**
// — the registry's own order for the universal `/` key (D280). Two earlier answers
// are gone. This once hoisted an APP entry to the lead, a folder's own app being
// what that folder IS, with a pane-only `_app` sentinel standing in wherever the
// registry's `app` mode was absent or gate-denied; both went with the app concept
// (D264). Then D269 kept app folders away from here entirely, because the pane
// resolved such a folder to its entry PAGE before asking for a mode list, so what
// arrived was an html FILE like any other — **that retarget is deleted** (D280:
// selecting a row must not run the folder's app), so a folder arrives as a folder
// and this list is the FOLDER's modes.
//
// The pane does not render that lead directly for a folder row, and this module is
// not where that is decided: a selected folder has no `preview` side at all
// (pane-side's paneSideList, D281), so the pill lands on the chat. This list still
// answers for the `preview` FALLBACK a folder gets when neither companion is
// offered, where the gates have dropped `claude` and `_listing` is what remains.
//
// Every entry here is a REAL mode: the list says what the pane can show, never
// that it should show nothing. An EMPTY list means a target with nothing to
// offer at all, which the pane answers with its metadata card.
export function paneModeList(input: PaneModeInput): string[] {
  const { templates, conditions, isDir } = input;
  const visible = (e: TemplateEntry) => isModeVisible(e, conditions);
  const modes: string[] = [];
  for (const e of templates) {
    if (e.mode === "_listing") {
      if (isDir) modes.push("_listing");
      continue;
    }
    if (visible(e)) modes.push(e.mode);
  }
  return modes;
}

// Does this mode's template need `chat_only=1` — i.e. must the pane take away
// the template's OWN preview pane?
//
// One mode does: `claude`. In a column this narrow the chat template's copy of
// the target would be a second, differently run preview of the same thing beside
// the host's (see Preview's sideSrcFor, and CHAT_ONLY in
// templates/claude/template.html).
//
// **It used to matter for a second reason, now historical.** The chat template
// fills its own pane by resolving the folder's ENTRY PAGE and rendering it
// (templates/shared/app_entry.py), and while `ListingPreviewPane` still had a
// row MODE that could resolve to `claude` (a selected folder's default view,
// D280), the flag was what stopped that folder's app page reappearing nested
// one level deeper for the same mere selection D280 exists to refuse. D443
// deleted that row mode along with the rest of the selection-driven pane, so
// the ONE caller left is the `claude` COMPANION iframe (always about the open
// folder) — the flag still matters there for the plain layout reason above.
export function paneChatOnly(mode: string): boolean {
  return mode === "claude";
}

// `activePaneMode`, `paneOpenTarget`/`PaneOpenTarget` and `paneOpenAction`/
// `PaneOpenAction` used to live here: the ROW-mode default resolution and the
// pane's expand button, both about a SELECTED ROW's own template. D443
// deleted the selected-row branch of `ListingPreviewPane` entirely — the pane
// never previews a row any more, so there is no default to resolve and no
// expand button pointing at a row-mode target — and by the time of this pass
// none of the three had any caller left anywhere but their own tests
// (confirmed by grep: `mode-visibility.test.ts` names `activePaneMode` in a
// comment, never imports it). Deleted along with their `pane-modes.test.ts`
// describe blocks. `paneModeList` above stays: `mode-labels.test.ts` uses it
// as a real registry-collision guard, independent of whether the live
// component still calls it — see this module's header.

