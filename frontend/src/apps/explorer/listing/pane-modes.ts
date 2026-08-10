// The preview pane's mode list and its default, as pure functions — the one
// place that decides WHICH modes the listing's pane offers for a target and
// WHICH of them it lands on. Extracted from ListingPreviewPane so the ordering
// and default rules are testable without mounting the pane (the component
// still owns icons, fetching and rendering).
//
// One pane-only sentinel lives here (not a registry mode, so not in
// KNOWN_SENTINEL_MODES — the server never sees it):
//   `_app`   — a folder's lone top-level HTML app, rendered in place.
//
// Everything here describes a SELECTED row. The self target (nothing selected —
// the folder already open on the left) never reaches this module: it shows no
// mode menu at all and always renders the pane's neutral hint, so there is
// nothing to rank and nothing to default to. It used to be modelled here, with
// an elaborate no-default rule that existed only to stop a first-wins default
// from opening a chat on the folder merely because the pane was on; hiding the
// picker deletes the question instead of answering it.
import type { TemplateEntry } from "@platform/lib/api";
import { isModeVisible } from "@platform/lib/mode-visibility";

export const PANE_APP_MODE = "_app";

export interface PaneModeInput {
  // The target's resolved template list (already sentinel-filtered, PT-12).
  templates: TemplateEntry[];
  // Gate verdicts (CT-12); null while still resolving. Visibility is NOT this
  // module's call — it is `lib/mode-visibility`'s one policy for every mode
  // surface: an entry hides only on an explicit `false`.
  conditions: Record<string, boolean> | null;
  isDir: boolean;
  // A lone top-level HTML app was found in this folder (`_app` is offerable).
  hasApp: boolean;
}

// The pane's mode list, in pane priority order (first = default).
//
// A lone app LEADS pane priority — a folder's own app is what that folder IS,
// so it outranks the opt-in tools aimed at it. After it, the template system's
// own order untouched — `_listing` stays exactly where the registry ranks it.
// `_listing` is dropped only for a FILE (no slot for a listing of a file).
//
// Every entry here is a REAL mode: the list says what the pane can show, never
// that it should show nothing. An EMPTY list means a target with nothing to
// offer at all, which the pane answers with its metadata card.
//
// The lone app has TWO possible carriers and only ever gets ONE entry. The
// `_app` sentinel exists so a folder with no registry `app` binding still gets
// an app preview; when the registry does offer a visible `app` entry the
// sentinel is redundant, and since both read the same label (mode-name.ts
// names them alike on purpose — they are the same view from two surfaces) they
// would draw two identical menu entries. So the registry entry wins and is
// HOISTED to the lead (the registry ranks `_listing` ahead of it, and a
// child-row app folder must not default to the nested listing); the sentinel
// steps in only when `app` is absent or condition-denied. Either way, only
// when the lone-app probe is positive — a folder with an `app` binding but no
// lone HTML page keeps `_listing` first.
export function paneModeList(input: PaneModeInput): string[] {
  const { templates, conditions, isDir, hasApp } = input;
  const visible = (e: TemplateEntry) => isModeVisible(e, conditions);
  const hasRegistryApp = templates.some((e) => e.mode === "app" && visible(e));
  const modes: string[] = [];
  if (isDir && hasApp && !hasRegistryApp) modes.push(PANE_APP_MODE);
  for (const e of templates) {
    if (e.mode === "_listing") {
      if (isDir) modes.push("_listing");
      continue;
    }
    if (visible(e)) modes.push(e.mode);
  }
  if (isDir && hasApp && hasRegistryApp) {
    const i = modes.indexOf("app");
    if (i > 0) modes.unshift(modes.splice(i, 1)[0]);
  }
  return modes;
}

// The mode the pane shows: the user's override (from the switcher, seeded from
// the `_panelMode` URL param) while that mode is still offered, else the first
// mode in pane priority order. `null` means the target offers nothing at all
// (an empty list — a file that maps to no template, or one whose every
// template is gate-denied), which the pane answers with its metadata card.
//
// Deliberately `modes[0]` and not `mode-visibility`'s `defaultMode` (first
// UNCONDITIONAL entry, PT-8/PT-9): the pane's own ordering above already
// front-loads what it wants to land on (`_app`/`app`), and that lead is exactly
// the entry a first-unconditional rule would skip past. The pane also never
// reaches here with verdicts in flight — the component holds a skeleton until
// they land — so "unconditional" carries no render-now-don't-wait meaning here
// the way it does on the preview route.
export function activePaneMode(modes: string[], modeOverride: string | null): string | null {
  if (modeOverride !== null && modes.includes(modeOverride)) return modeOverride;
  return modes[0] ?? null;
}

// Where the pane's expand button goes: the previewed row, opened full-screen IN
// THE MODE THE PANE IS SHOWING. Without the mode the expand icon silently threw
// away the template the user had switched to and reopened the target in its
// default — the one thing "make this the whole view" must not do.
//
// It takes the RESOLVED active mode, never the raw `_panelMode` param: when the
// param names a mode this selection does not offer, the pane has already fallen
// back to its default, and the open has to match what is on screen.
//
// Three modes are not `_mode` values and are translated rather than passed on:
//
//   `_app`      — a pane-only sentinel; the server has never heard of it. What
//                 the pane frames is the folder's lone app FILE, so that file
//                 is what opens full-screen (in its own default view, which is
//                 that app). With no app resolved there is nothing to translate
//                 and the folder itself opens.
//   `_listing`  — a folder full-screen IS its listing; `_mode=_listing` would
//                 be the destination's own default written out longhand, and
//                 Preview strips it again the moment the user switches modes.
//   null        — the target offers nothing at all (the pane is showing its
//                 metadata card), so there is no mode to carry.
export interface PaneOpenTarget {
  path: string;
  isDir: boolean;
  // Absent = open the destination's own default view.
  mode?: string;
}

export function paneOpenTarget(
  row: { path: string; isDir: boolean },
  activeMode: string | null,
  app: { path: string } | null | undefined
): PaneOpenTarget {
  if (activeMode === PANE_APP_MODE) {
    return app ? { path: app.path, isDir: false } : { path: row.path, isDir: row.isDir };
  }
  if (activeMode === null || activeMode === "_listing") {
    return { path: row.path, isDir: row.isDir };
  }
  return { path: row.path, isDir: row.isDir, mode: activeMode };
}

// WHICH control the pane's header offers for the previewed row — the half
// paneOpenTarget deliberately does not answer, because for a folder the honest
// answer is "none of them".
//
//   file            → `expand`. "Make this preview the whole view", in the mode
//                     the pane is showing (paneOpenTarget above).
//   folder with an
//   app of its own  → `app`. The folder's real primary: open the app. Not
//                     conditioned on the mode the pane happens to be showing —
//                     a folder switched to its embedded listing or to a chat is
//                     still an app, and the button still means the app. The
//                     target is the lone app PAGE, the same translation the
//                     `_app` sentinel gets and the same thing Preview's own
//                     "Open as app" falls back to.
//   plain folder    → `none`. Expanding a folder means opening its listing —
//                     and its listing is what the LEFT HALF of this very split
//                     already is. The button offered to replace a two-pane view
//                     of a folder with a one-pane view of the same folder,
//                     which is not an action so much as a step backwards. Its
//                     one honest use, "get into this folder", is what
//                     double-click and Enter on the row already do.
//
// An `undefined` app (the folder's lone-app probe still in flight) counts as no
// app. The pane holds a skeleton until the probe settles, so this is belt and
// braces — but a button that appears and then changes meaning is exactly the
// outcome worth ruling out.
export type PaneOpenAction =
  | { kind: "expand"; target: PaneOpenTarget }
  | { kind: "app"; target: PaneOpenTarget }
  | { kind: "none" };

export function paneOpenAction(
  row: { path: string; isDir: boolean },
  activeMode: string | null,
  app: { path: string } | null | undefined
): PaneOpenAction {
  if (!row.isDir) return { kind: "expand", target: paneOpenTarget(row, activeMode, app) };
  if (!app) return { kind: "none" };
  return { kind: "app", target: paneOpenTarget(row, PANE_APP_MODE, app) };
}
