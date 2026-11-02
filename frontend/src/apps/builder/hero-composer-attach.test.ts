// The New app composer's attachments, pinned to the source: what a paste
// intercepts and what the create sends are wiring no pure function holds
// (the same habit as shell/new-task-images.test.ts).
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const HERO = readFileSync(join(import.meta.dir, "HomeHero.tsx"), "utf8");
const API = readFileSync(join(import.meta.dir, "../../platform/lib/api.ts"), "utf8");

describe("HeroComposer attachments", () => {
  it("the prompt textarea takes clipboard FILES only, and uploads them as task shots", () => {
    const area = HERO.slice(HERO.indexOf("<TextArea"), HERO.indexOf("/>", HERO.indexOf("<TextArea")));
    expect(area).toContain("onPaste=");
    expect(area).toMatch(/\.filter\(\(i\) => i\.kind === "file"\)/);
    expect(area).toMatch(/if \(files\.length\) \{\s*e\.preventDefault\(\);/);
    expect(HERO).toContain("uploadTaskShot(file)");
  });

  it("drops on the box attach too", () => {
    expect(HERO).toMatch(/onDrop=\{\(e\) => \{[^}]*attachFiles\(e\.dataTransfer\.files\)/);
  });

  it("the create waits for in-flight uploads, then sends the uploaded paths", () => {
    const create = HERO.slice(HERO.indexOf("const createNamed"));
    const wait = create.indexOf("await Promise.all([...pendingRef.current])");
    const call = create.indexOf("createAppUnderFreeName(");
    expect(wait).toBeGreaterThan(-1);
    expect(call).toBeGreaterThan(wait);
  });

  it("createApp posts `images` to /api/apps/new", () => {
    const fn = API.slice(API.indexOf("export function createApp("));
    expect(fn.slice(0, fn.indexOf("\n}"))).toMatch(
      /postJson<NewAppResult>\("\/api\/apps\/new", \{[^}]*\bimages\b[^}]*\}\)/,
    );
  });
});

describe("HeroComposer model / effort", () => {
  it("offers no Auto option and a pick stays this composer's own", () => {
    expect(HERO).not.toMatch(/label: "Auto"/);
    // The Claude settings page is the ONE editor of the global pair: a pick
    // here rides on createApp into the new app's task and is never written back.
    expect(HERO).not.toContain("setClaudeDefaults");
    expect(HERO).toContain("picked.current.model = true");
    expect(HERO).toContain("picked.current.effort = true");
    // The picks reach the create calls: the pick lands in state, and both the
    // direct and the free-name create carry model + effort.
    expect(HERO).toContain("setModel(next)");
    expect(HERO).toContain("await createApp(name, fullPrompt(), model, effort, images, agent)");
    expect(HERO).toMatch(
      /createAppUnderFreeName\(\s*name,\s*fullPrompt\(\),\s*model,\s*effort,\s*images,\s*agent,?\s*\)/,
    );
  });
});
