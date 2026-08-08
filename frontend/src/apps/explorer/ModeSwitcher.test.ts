// Mode LABELS have to be distinguishable wherever two of them are offered side
// by side, and nothing enforced that.
//
// The bug this suite exists for: MODE_TITLES mapped `versions` → "History", and
// `history` already read "History" through the capitalize fallback. Two registry
// keys carry both modes (`.parquet` and `.html`), so opening either put two
// visually identical "History" entries in the Open With menu (fs-actions.ts uses
// `label: modeTitle(t.mode)`), pointing at different templates — one is a git
// commit timeline for that path, the other is the `<file>.json` sidecar's
// activity timeline (sessions, bookmarks, comments). Dispatch keys on `t.mode`
// internally, so it was a labelling collision only: nothing broke, the user just
// could not tell which was which.
//
// The guard is general on purpose. A collision is created by an EDIT SOMEWHERE
// ELSE — adding a mode to a key, or renaming an unrelated template into a name
// whose capitalize fallback happens to match — so pinning the one pair would not
// have caught the next one.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { modeTitle } from "@apps/explorer/ModeSwitcher";
import { PANE_APP_MODE, PANE_NONE_MODE, paneModeList } from "@apps/explorer/listing/pane-modes";
import type { TemplateEntry } from "@platform/lib/api";

// The built-in registry, read from the repo rather than duplicated here — the
// point is to track whatever the bindings actually are (PT-7/CT-3). A user
// override (§16) can add modes to a key at runtime and is out of reach from here;
// what this can guarantee is that the shipped set is unambiguous.
const REGISTRY: Record<string, string[]> = JSON.parse(
  readFileSync(join(import.meta.dir, "../../../../fused_render/templates/registry.json"), "utf8")
);

// Every set of modes a user can see in ONE list. Keys are the registry's, plus
// the `.html` pair the shell hardcodes (PT-12) — which the registry already
// carries, so it needs no special case.
function offeredSets(): Array<{ where: string; modes: string[] }> {
  const sets: Array<{ where: string; modes: string[] }> = [];
  for (const [key, modes] of Object.entries(REGISTRY)) {
    sets.push({ where: `registry key ${key}`, modes });
    // The listing's preview pane assembles its own list from the same entries
    // plus two pane-only sentinels (listing/pane-modes.ts), and renders them
    // through the same modeTitle — so it is a second place two labels meet.
    const templates: TemplateEntry[] = modes.map(
      (m) => ({ mode: m, path: m.startsWith("_") ? null : `/t/${m}`, icon: null, conditional: false }) as TemplateEntry
    );
    for (const self of [false, true]) {
      for (const hasApp of [false, true]) {
        for (const isDir of [false, true]) {
          sets.push({
            where: `preview pane, key ${key} (isDir=${isDir} self=${self} hasApp=${hasApp})`,
            modes: paneModeList({ templates, conditions: {}, isDir, self, hasApp }),
          });
        }
      }
    }
  }
  return sets;
}

test("no two modes offered together resolve to the same label", () => {
  const clashes: string[] = [];
  for (const { where, modes } of offeredSets()) {
    const byLabel = new Map<string, string[]>();
    for (const m of new Set(modes)) {
      const label = modeTitle(m);
      byLabel.set(label, [...(byLabel.get(label) ?? []), m]);
    }
    for (const [label, ms] of byLabel) {
      if (ms.length > 1) clashes.push(`${where}: ${ms.join(" + ")} both read "${label}"`);
    }
  }
  expect(clashes).toEqual([]);
});

// The two pairs the guard above was written for, named so a rename that merely
// swaps one collision for another is not silently "still passing".
test("the two timelines are named for what each actually shows", () => {
  // A git commit timeline for one path.
  expect(modeTitle("versions")).toBe("Revisions");
  // The `<file>.json` sidecar: chat sessions, bookmarks, review comments (§24).
  expect(modeTitle("history")).toBe("Activity");
});

test("the folder's lone page is not labelled the same as the app template", () => {
  // `app` is the registry template of the app-builder trio; `_app` is the pane's
  // own in-place render of a folder's single top-level HTML. Both were "App", and
  // a workspace app folder offers both at once (its gate passes AND its
  // index.html is the folder's lone page).
  expect(modeTitle("app")).toBe("App");
  expect(modeTitle(PANE_APP_MODE)).toBe("Folder app");
  expect(modeTitle(PANE_NONE_MODE)).toBe("No preview");
});

test("a mode with no display name still reads as a capitalized folder name", () => {
  expect(modeTitle("code")).toBe("Code");
  expect(modeTitle("_render")).toBe("Rendered");
  expect(modeTitle("_listing")).toBe("Listing");
});
