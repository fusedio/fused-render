// The sidebar's Canvases entry: one destination, named the same in every place
// it appears, offered at all only once the feature is turned on (D427) — the
// feature flag ALONE, with no sign-in half since 2026-10-22 (owner: on a new
// machine the section has to appear the moment the flag is flipped) — and
// never lit twice at once (D358, renamed in D372).
//
// Read out of the source, the way sidebar-tasks.test.ts reads its own wiring
// claims: the parts that matter here are WHICH gate each site is behind and
// WHAT it is called, and a DOM-less suite can hold those honestly.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SHELL = new URL(".", import.meta.url).pathname;
const SIDEBAR = readFileSync(join(SHELL, "GlobalSidebar.tsx"), "utf8");
const PAGE = readFileSync(join(SHELL, "../apps/canvases/Canvases.tsx"), "utf8");
const FLAG = readFileSync(join(SHELL, "../apps/canvases/feature-flag.ts"), "utf8");
const PREFS_PAGE = readFileSync(join(SHELL, "settings/toggles.tsx"), "utf8");
const APP = readFileSync(join(SHELL, "App.tsx"), "utf8");

describe("the sidebar's Canvases entry", () => {
  it("is 'Canvases' in the sidebar and 'Workbench Canvases' on the page", () => {
    // The settings-menu item is the ONE nav site left (owner, 2026-10-20: the
    // primary row and the rail icon are gone — the Canvases SECTION lists the
    // canvases and its "+ Canvas" row opens the catalog). Anchored on the
    // `label:` form because the bare word also appears as a route, a CSS
    // class and a component name.
    const named = SIDEBAR.match(/label(?::|=)\s*"Canvases"/g) ?? [];
    expect(named.length).toBe(1);
    // Nav says the short name — it sits under Home/Tasks, where "Workbench" was
    // the longest label in the rail for the least product meaning. The PAGE
    // carries the qualified name instead, so the destination still says which
    // canvases these are once you are on it (D372).
    expect(SIDEBAR).not.toMatch(/Workbench canvases/i);
    expect(PAGE).toMatch(/<h1 className="canvases-title">Workbench Canvases<\/h1>/);
  });

  it("shows the Canvases section behind the feature flag alone, and no nav row", () => {
    // ONE gate (owner, 2026-10-22): the feature is offered. It was the
    // conjunction with "there is an account" (D427 over D358) until a new
    // machine turned the flag on and saw nothing — the section the reader had
    // just enabled waited on a sign-in that happens INSIDE it (the "+ Canvas"
    // row opens the catalog, which hosts the sign-in). It guards the Canvases
    // SECTION (the desk's canvas half, CurrentAppsSection); there is no
    // primary row and no rail icon any more.
    expect(SIDEBAR).not.toMatch(/useCanvasesLoggedIn|canvasesLoggedIn/);
    expect(SIDEBAR).toMatch(/const canvasesEnabled = useCanvasesFeature\(\)/);
    expect(SIDEBAR).toMatch(/const canvasesInNav = canvasesEnabled;/);
    expect(SIDEBAR).toMatch(/\{canvasesInNav && <CurrentAppsSection section="canvases" \/>\}/);
    expect(SIDEBAR).not.toMatch(/id="canvases-link"/);
    expect(SIDEBAR).not.toMatch(/key: "canvases"/);
  });

  it("hides BOTH entry points when the feature is off, without touching the routes", () => {
    // The preference is over the entry points, not the pages: /canvases and
    // /canvases/<env>/<name> keep answering a deep link or a bookmark exactly as they
    // do signed out, so the flag never strands an open workspace. Nothing in
    // App.tsx's routing consults it.
    expect(APP).not.toMatch(/canvasesEnabled|useCanvasesFeature/);
    expect(APP).toMatch(/const isCanvases = pathname === "\/canvases"/);
    // The menu entry is gated on the feature too — it has always been ungated
    // on sign-in (the page explains a signed-out state itself), and since
    // 2026-10-22 the section is gated the same way.
    expect(SIDEBAR).toMatch(
      /\.\.\.\(canvasesEnabled\s*\n?\s*\?\s*\n?\s*\[\{ href: "\/canvases", label: "Canvases"/
    );
  });

  it("reads the feature flag from prefs, one fetch plus a publish", () => {
    // No poll, unlike sign-in: this pref can only change on the Preferences
    // page of this app, and that page hands the new value over so the row
    // appears with the switch instead of on the next navigation.
    expect(FLAG).toMatch(/export function publishCanvasesEnabled/);
    expect(FLAG).toMatch(/getPrefs(Shared)?\(\)/);
    expect(FLAG).not.toMatch(/setTimeout|setInterval/);
    expect(PREFS_PAGE).toMatch(/publishCanvasesEnabled\(next\.canvases\.enabled\)/);
    // And a publish OUTRANKS a read already in flight — a generation counter,
    // because a reader who lands on
    // /preferences and flips the checkbox before that first GET resolves would
    // otherwise watch the row they just enabled vanish when the pre-toggle
    // payload arrives.
    expect(FLAG).toMatch(/const departed = generation/);
    expect(FLAG).toMatch(/if \(generation === departed\) set\(p\.canvases\.enabled\)/);
    expect(FLAG).toMatch(/generation \+= 1;\n\s*reading = Promise\.resolve\(\);\n\s*set\(next\)/);
    // Default off means an unanswered read renders as hidden — assuming ON
    // would flash an entry the reader turned off on every load.
    expect(FLAG).toMatch(/useState\(enabled === true\)/);
  });

  it("keeps the menu entry, which lights the Preferences trigger on the catalog", () => {
    // The menu still lists the destination by name; with no primary row to
    // clash with, the trigger lights on /canvases like any other menu page.
    expect(SIDEBAR).toMatch(/\{ href: "\/canvases", label: "Canvases"/);
    expect(SIDEBAR).not.toMatch(/canvasesActive/);
  });

  it("reads the flag through the standalone module, not the app barrel", () => {
    // index.ts re-exports Canvases/CanvasWorkspace: importing the flag through
    // it would pull the whole sub-app into the shell's main bundle for one
    // boolean (the reason claude_config/available.ts is split out too). And
    // the sign-in store is gone with its last reader: nothing probes it.
    expect(SIDEBAR).toMatch(/from "@apps\/canvases\/feature-flag"/);
    expect(SIDEBAR).not.toMatch(/from "@apps\/canvases"/);
    expect(SIDEBAR).not.toMatch(/@apps\/canvases\/logged-in/);
    expect(PAGE).not.toMatch(/publishLoggedIn|logged-in/);
  });
});
