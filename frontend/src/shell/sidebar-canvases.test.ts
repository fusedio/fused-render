// The sidebar's Canvases entry: one destination, named the same in every place
// it appears, offered at all only once the feature is turned on (D427), shown
// as primary nav only once this machine is ALSO signed in, and never lit twice
// at once (D358, renamed in D372).
//
// Read out of the source, the way sidebar-tasks.test.ts reads its own wiring
// claims: the parts that matter here are WHICH gate each site is behind and
// WHAT it is called, and a DOM-less suite can hold those honestly.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { decideLoggedIn } from "@apps/canvases/logged-in";
import type { CanvasesStatus } from "@apps/canvases/api";

const SHELL = new URL(".", import.meta.url).pathname;
const SIDEBAR = readFileSync(join(SHELL, "GlobalSidebar.tsx"), "utf8");
const STORE = readFileSync(join(SHELL, "../apps/canvases/logged-in.ts"), "utf8");
const PAGE = readFileSync(join(SHELL, "../apps/canvases/Canvases.tsx"), "utf8");
const FLAG = readFileSync(join(SHELL, "../apps/canvases/feature-flag.ts"), "utf8");
const PREFS_PAGE = readFileSync(join(SHELL, "Preferences.tsx"), "utf8");
const APP = readFileSync(join(SHELL, "App.tsx"), "utf8");

describe("the sidebar's Canvases entry", () => {
  it("is 'Canvases' in the sidebar and 'Workbench Canvases' on the page", () => {
    // The menu item, the expanded row and the collapsed rail's tooltip: three
    // sites, one destination, so a reader who found it in one place recognises
    // it in the others. Anchored on the `label:`/`label=` forms because the bare
    // word also appears as a route, a CSS class and a component name.
    const named = SIDEBAR.match(/label(?::|=)\s*"Canvases"/g) ?? [];
    expect(named.length).toBe(3);
    // Nav says the short name — it sits under Home/Tasks, where "Workbench" was
    // the longest label in the rail for the least product meaning. The PAGE
    // carries the qualified name instead, so the destination still says which
    // canvases these are once you are on it (D372).
    expect(SIDEBAR).not.toMatch(/Workbench canvases/i);
    expect(PAGE).toMatch(/<h1 className="canvases-title">Workbench Canvases<\/h1>/);
  });

  it("shows the row and the rail icon behind the SAME gate", () => {
    // A row that exists until you collapse the sidebar is a destination people
    // lose — both halves are gated, or neither should be. The gate is the
    // conjunction (D427): the feature is offered AND there is an account.
    expect(SIDEBAR).toMatch(/const canvasesLoggedIn = useCanvasesLoggedIn\(\)/);
    expect(SIDEBAR).toMatch(/const canvasesEnabled = useCanvasesFeature\(\)/);
    expect(SIDEBAR).toMatch(
      /const canvasesInNav = canvasesEnabled && canvasesLoggedIn/
    );
    // The rail entry, spread in conditionally.
    expect(SIDEBAR).toMatch(/\.\.\.\(canvasesInNav\s*\n?\s*\?/);
    // The expanded row.
    expect(SIDEBAR).toMatch(/\{canvasesInNav && \(\s*\n\s*<NavItem/);
  });

  it("hides BOTH entry points when the feature is off, without touching the routes", () => {
    // The preference is over the entry points, not the pages: /canvases and
    // /canvases/<name> keep answering a deep link or a bookmark exactly as they
    // do signed out, so the flag never strands an open workspace. Nothing in
    // App.tsx's routing consults it.
    expect(APP).not.toMatch(/canvasesEnabled|useCanvasesFeature/);
    expect(APP).toMatch(/const isCanvases = pathname === "\/canvases"/);
    // The menu entry is gated on the FEATURE ALONE — it has always been ungated
    // on sign-in (the page explains a signed-out state itself), and gating it on
    // the account would delete the only affordance for setting Canvases up.
    expect(SIDEBAR).toMatch(
      /\.\.\.\(canvasesEnabled\s*\n?\s*\?\s*\n?\s*\[\{ href: "\/canvases", label: "Canvases"/
    );
  });

  it("reads the feature flag from prefs, one fetch plus a publish", () => {
    // No poll, unlike sign-in: this pref can only change on the Preferences
    // page of this app, and that page hands the new value over so the row
    // appears with the checkbox instead of on the next navigation.
    expect(FLAG).toMatch(/export function publishCanvasesEnabled/);
    expect(FLAG).toMatch(/getPrefs\(\)/);
    expect(FLAG).not.toMatch(/setTimeout|setInterval/);
    expect(PREFS_PAGE).toMatch(/publishCanvasesEnabled\(next\.canvases\.enabled\)/);
    // And a publish OUTRANKS a read already in flight — the same generation
    // counter logged-in.ts keeps, for the same reason: a reader who lands on
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

  it("lights on the list page only, not inside a canvas", () => {
    // /canvases/<name> is a workspace you opened, not the list page — Home's
    // reasoning, and the reason this is equality rather than a prefix test.
    expect(SIDEBAR).toMatch(/const canvasesActive = pathname === "\/canvases"/);
    expect(SIDEBAR).not.toMatch(/pathname\.startsWith\("\/canvases/);
  });

  it("keeps the menu entry but stops it lighting the Preferences trigger too", () => {
    // Both asks stay true: the menu still lists the destination by name, and
    // the trigger does not light beside an already-lit primary row.
    expect(SIDEBAR).toMatch(/\{ href: "\/canvases", label: "Canvases"/);
    expect(SIDEBAR).toMatch(/!\(canvasesInNav && e\.href === "\/canvases"\)/);
  });

  it("probes sign-in through the standalone module, not the app barrel", () => {
    // index.ts re-exports Canvases/CanvasWorkspace: importing the probe through
    // it would pull the whole sub-app into the shell's main bundle for one
    // boolean (the reason claude_config/available.ts is split out too).
    expect(SIDEBAR).toMatch(/from "@apps\/canvases\/logged-in"/);
    expect(SIDEBAR).not.toMatch(/from "@apps\/canvases"/);
  });

  it("treats sign-in as a fact that MOVES, unlike claude-config availability", () => {
    // Signing in and out happens mid-session, so there is no one-shot cache:
    // the page publishes what its own status poll returned, and the store's own
    // slow poll only exists to catch a login that happened elsewhere.
    expect(STORE).toMatch(/export function publishLoggedIn/);
    expect(PAGE).toMatch(/publishLoggedIn\(status\)/);
    expect(STORE).toMatch(/window\.setTimeout\(poll, POLL_MS\)/);
    // A failed read is not a sign-out — the row survives a server restart.
    expect(STORE).toMatch(/catch \{\n\s*\/\/ A failed read is not a sign-out/);
  });

  it("does not un-hide the row for credentials the server already refused", () => {
    // /api/canvases/status answers `logged_in` from the file EXISTING, so a
    // present-but-unrefreshable store reads as signed in forever. The page
    // learns otherwise from a guarded 401 and replaces itself with the sign-in
    // wall; before this the store's own poll wrote that verdict back a minute
    // later and the row returned over a wall (bugbot, 2026-08-19).
    const dead = status({ logged_in: true, creds_stamp: 100 });
    expect(decideLoggedIn(dead, null)).toBe(true);
    expect(decideLoggedIn(dead, 100)).toBe(false);
    // A re-login over a stale-but-present store never flips `logged_in` — the
    // mtime changing is the whole signal, which is why the refusal is
    // remembered as a stamp and not a boolean.
    expect(decideLoggedIn(status({ logged_in: true, creds_stamp: 101 }), 100)).toBe(true);
    // No store at all is signed out whatever was refused before.
    expect(decideLoggedIn(status({ logged_in: false, creds_stamp: null }), 100)).toBe(false);
    // And a stampless read is never mistaken for the refused one.
    expect(decideLoggedIn(status({ logged_in: true, creds_stamp: null }), null)).toBe(true);
  });
});

function status(over: Partial<CanvasesStatus>): CanvasesStatus {
  return {
    cli_found: true,
    logged_in: true,
    creds_stamp: null,
    login_in_flight: false,
    workbench_base_url: "https://www.fused.io",
    canvases_dir: "/Users/x/.fused-render/canvases",
    ...over,
  };
}
