// Two modes offered SIDE BY SIDE may never be indistinguishable.
//
// This is a test about registry BINDINGS, not about wording: which modes a key
// carries together is what decides whether a collision is even possible, and
// that is decided in `fused_render/templates/registry.json` and in the preview
// pane's own list (`listing/pane-modes.ts`). The display names themselves are
// `platform/lib/mode-name.ts`'s business and are unit-tested there — nothing
// here asserts a particular label.
//
// The bug that motivated the guard: two timeline templates — a sidecar-backed
// one and a git-backed one — both read "History", and `.parquet` and `.html`
// each carried BOTH modes, so the Open With menu (`lib/fs-actions.ts`, `label:
// modeTitle(t.mode)`) and every mode menu drew two identical entries pointing
// at different templates. Dispatch keys on `mode`, so nothing broke — the user
// simply could not tell which was which.
//
// That particular pair is gone rather than papered over: the sidecar one was
// deleted, and the git-backed survivor went too once the `git` view covered the
// same ground. The guard stays because the collision it catches is structural,
// not about that pair.
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
import { paneModeList } from "./pane-modes";

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
// pane's assembled list — the same entries with `_listing` dropped for a file —
// across both isDir permutations.
function offeredSets(): Array<{ where: string; modes: string[] }> {
  const sets: Array<{ where: string; modes: string[] }> = [];
  for (const [key, modes] of Object.entries(REGISTRY)) {
    if (!Array.isArray(modes)) continue; // a `null` binding disables the key (CT-2/D94)
    sets.push({ where: `registry key ${key}`, modes });
    const templates: TemplateEntry[] = modes.map(
      (m) => ({ mode: m, path: m.startsWith("_") ? null : `/t/${m}`, icon: null, conditional: false }) as TemplateEntry
    );
    for (const isDir of PERMUTATIONS) {
      sets.push({
        where: `preview pane, key ${key} (isDir=${isDir})`,
        modes: paneModeList({ templates, conditions: {}, isDir }),
      });
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
