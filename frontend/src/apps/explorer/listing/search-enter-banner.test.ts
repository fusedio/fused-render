// The Enter-to-open-and-search banner (Listing.tsx's `searching &&
// !showsSearchHits && (!isPathQuery || pathQueryRefused)` branch, the row
// built from `enterPrompt`/`pathNotFoundMessage`) is the one `.status-message`
// site that is an instruction OR a refusal report rather than a plain state
// report, so it carries a modifier class the other eleven `.status-message`
// sites do not. Same CSS-parsing pattern as search-bar-expand.test.ts: read
// explorer.css and Listing.tsx as text, no DOM in this suite.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const LISTING = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

function rulesFor(selectorExact: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim() === selectorExact) out.push(m[2]);
  }
  return out;
}

test("the gate that renders the row also carries the modifier class", () => {
  const gate = LISTING.indexOf(
    "if (searching && !showsSearchHits && (!isPathQuery || pathQueryRefused))",
  );
  expect(gate).toBeGreaterThan(-1);
  const enterCall = LISTING.indexOf("enterPrompt(", gate);
  expect(enterCall).toBeGreaterThan(gate);
  // The className attribute has to be on the same <td>, i.e. between the
  // gate and the enterPrompt call that fills it.
  const between = LISTING.slice(gate, enterCall);
  expect(between).toMatch(/className="status-message listing-enter-row"/);
});

test("the modifier washes the row in the accent, from the token not a literal", () => {
  const decls = rulesFor(".status-message.listing-enter-row");
  expect(decls.length).toBe(1);
  const decl = decls[0];
  expect(decl).toMatch(/background:\s*rgba\(var\(--accent-rgb\),\s*0\.13\)/);
  expect(decl).toMatch(/color:\s*var\(--accent-soft\)/);
  // Never the loud fill — that reads as an error/alert, not an instruction.
  expect(decl).not.toMatch(/--on-accent/);
  expect(decl).not.toMatch(/background:\s*var\(--accent\)/);
});

test("the bare .status-message rule is untouched by this — the wash lives only on the compound selector", () => {
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  let sawBareRule = false;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim() === ".status-message") {
      sawBareRule = true;
      expect(m[2]).not.toMatch(/accent/);
    }
    if (m[1].trim() === ".status-message.error") {
      expect(m[2]).not.toMatch(/accent/);
    }
  }
  // explorer.css itself carries no bare `.status-message` rule (it lives in
  // preview.css) — this just guards that IF one is ever added here, it is
  // not where this modifier's accent leaks in from.
  void sawBareRule;
});
