// Two modes offered SIDE BY SIDE may never be indistinguishable.
//
// This is a test about registry BINDINGS, not about wording: which modes a key
// carries together is what decides whether a collision is even possible, and
// that is decided in `fused_render/templates/registry.json` and in the preview
// pane's own list (`listing/pane-modes.ts`). The display names themselves are
// `platform/lib/mode-name.ts`'s business and are unit-tested there — nothing
// here asserts a particular label.
//
// The bug that motivated the guard: a rename made `versions` read the same
// string as `history` through the humanizer fallback, and `.parquet` and
// `.html` each carry BOTH modes, so the Open With menu (`lib/fs-actions.ts`,
// `label: modeTitle(t.mode)`) and every mode menu drew two identical entries
// pointing at different templates. Dispatch keys on `mode`, so nothing broke —
// the user simply could not tell which was which.
//
// The guard is general on purpose: a collision is created by an edit SOMEWHERE
// ELSE — adding a mode to a key, or naming a new template folder into a string
// the humanizer already produces — so pinning the one pair would not catch the
// next.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { modeTitle } from "@platform/lib/mode-name";
import type { TemplateEntry } from "@platform/lib/api";
import { PANE_APP_MODE, paneModeList } from "./pane-modes";

// The built-in registry, read from the repo rather than duplicated here — the
// point is to track whatever the bindings actually are (PT-7/CT-3). A user
// override (§16) can add modes to a key at runtime and is out of reach from
// here; what this can guarantee is that the shipped set is unambiguous.
const REGISTRY: Record<string, string[]> = JSON.parse(
  readFileSync(join(import.meta.dir, "../../../../../fused_render/templates/registry.json"), "utf8")
);

const PERMUTATIONS = [false, true];

// Every set of modes a user can see in ONE list: the registry key's own list
// (the Open With menu and the preview route render it as-is), plus the preview
// pane's assembled list — same entries, one pane-only sentinel added and some
// dropped — across the isDir/self/hasApp permutations the pane can be in.
function offeredSets(): Array<{ where: string; modes: string[] }> {
  const sets: Array<{ where: string; modes: string[] }> = [];
  for (const [key, modes] of Object.entries(REGISTRY)) {
    if (!Array.isArray(modes)) continue; // a `null` binding disables the key (CT-2/D94)
    sets.push({ where: `registry key ${key}`, modes });
    const templates: TemplateEntry[] = modes.map(
      (m) => ({ mode: m, path: m.startsWith("_") ? null : `/t/${m}`, icon: null, conditional: false }) as TemplateEntry
    );
    for (const hasApp of PERMUTATIONS) {
      for (const isDir of PERMUTATIONS) {
        sets.push({
          where: `preview pane, key ${key} (isDir=${isDir} hasApp=${hasApp})`,
          modes: paneModeList({ templates, conditions: {}, isDir, hasApp }),
        });
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

// `app` (the registry template) and `_app` (the pane's in-place render of a
// folder's lone top-level page) are the SAME view from two surfaces, and
// mode-name.ts names them alike deliberately. The reason the guard above passes
// is therefore not an exemption but a list rule: the pane offers exactly one of
// the two carriers, never both. Asserting it here keeps the guard honest — if
// this ever regresses, the collision test must trip rather than be excused.
test("the pane never co-offers `app` and the `_app` sentinel", () => {
  const both: string[] = [];
  for (const { where, modes } of offeredSets()) {
    if (modes.includes("app") && modes.includes(PANE_APP_MODE)) both.push(where);
  }
  expect(both).toEqual([]);
  // …and the pair really does read alike, so the rule above is load-bearing
  // rather than incidentally true.
  expect(modeTitle(PANE_APP_MODE)).toBe(modeTitle("app"));
});
