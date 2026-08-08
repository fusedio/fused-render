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
// "Nothing previewed" is a STATE, not a mode. A SELF target (nothing selected,
// i.e. the folder already open on the left) resolves to NO active mode, and the
// pane renders its neutral hint for that. There used to be a `_none` entry in
// the list instead, so "No preview" appeared in the mode menu as something to
// pick — which it never was: closing the pane is that gesture. What the entry
// was really solving stays solved by `activePaneMode` below: dropping the self
// row's `_listing` (redundant — that listing is the left half) left `claude` as
// modes[0], so with a plain first-wins default merely opening the pane opened a
// chat on the user's folder.
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
  // True when the target is the listing's OWN folder (nothing selected).
  self: boolean;
  // A lone top-level HTML app was found in this folder (`_app` is offerable).
  hasApp: boolean;
}

// The pane's mode list, in pane priority order (first = default).
//
// A lone app LEADS pane priority — a folder's own app is what that folder IS,
// so it stays the self target's default too (it is a preview, not an opt-in
// tool). After it, the template system's own order untouched — `_listing` stays
// exactly where the registry ranks it. `_listing` is dropped for a FILE (no
// slot for a listing of a file) and for a SELF target (that listing is the left
// half of the split).
//
// Every entry here is a REAL mode: the list says what the pane can show, never
// that it should show nothing (see `activePaneMode` for the self target's
// no-default rule). So a self target whose every peer is gate-denied gets an
// EMPTY list, which the pane reads as "bare hint, no header".
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
  const { templates, conditions, isDir, self, hasApp } = input;
  const visible = (e: TemplateEntry) => isModeVisible(e, conditions);
  const hasRegistryApp = templates.some((e) => e.mode === "app" && visible(e));
  const modes: string[] = [];
  if (isDir && hasApp && !hasRegistryApp) modes.push(PANE_APP_MODE);
  for (const e of templates) {
    if (e.mode === "_listing") {
      if (isDir && !self) modes.push("_listing");
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
// mode in pane priority order — and `null`, meaning "nothing previewed", for a
// SELF target that has no app of its own.
//
// The self rule is the whole point of this function. Previewing the folder you
// are already looking at has no obvious subject, and the modes left after
// `_listing` is dropped are the `/` key's heavyweight opt-ins (the chat, git,
// versions), so a first-wins default would open a chat merely because the pane
// was toggled on. The one exception is the folder's OWN app — `_app`, or the
// registry `app` entry hoisted in its place — which is a preview of the folder
// rather than a tool aimed at it, so `hasApp` (the lone-app probe, the same
// input `paneModeList` takes) is what says the lead is that app and may default.
// A selected row always has a subject, so it keeps the plain first-wins rule.
//
// Deliberately `modes[0]` and not `mode-visibility`'s `defaultMode` (first
// UNCONDITIONAL entry, PT-8/PT-9): the pane's own ordering above already
// front-loads what it wants to land on (`_app`/`app`), and that lead is exactly
// the entry a first-unconditional rule would skip past. The pane also never
// reaches here with verdicts in flight — the component holds a skeleton until
// they land — so "unconditional" carries no render-now-don't-wait meaning here
// the way it does on the preview route.
export function activePaneMode(
  modes: string[],
  modeOverride: string | null,
  target: { self?: boolean; hasApp?: boolean } = {}
): string | null {
  if (modeOverride !== null && modes.includes(modeOverride)) return modeOverride;
  if (target.self && !target.hasApp) return null;
  return modes[0] ?? null;
}
