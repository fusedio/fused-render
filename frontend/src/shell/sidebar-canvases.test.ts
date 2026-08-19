// The sidebar's Workbench canvases entry: one destination, named the same in
// both places it appears, shown as primary nav only once this machine is signed
// in, and never lit twice at once (D358).
//
// Read out of the source, the way sidebar-tasks.test.ts reads its own wiring
// claims: the parts that matter here are WHICH gate each site is behind and
// WHAT it is called, and a DOM-less suite can hold those honestly.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SHELL = new URL(".", import.meta.url).pathname;
const SIDEBAR = readFileSync(join(SHELL, "GlobalSidebar.tsx"), "utf8");
const STORE = readFileSync(join(SHELL, "../apps/canvases/logged-in.ts"), "utf8");
const PAGE = readFileSync(join(SHELL, "../apps/canvases/Canvases.tsx"), "utf8");

describe("the sidebar's Workbench canvases entry", () => {
  it("is called the same thing everywhere, and never plain 'Canvases'", () => {
    // The menu item, the expanded row and the collapsed rail's tooltip: three
    // sites, one destination, so a reader who found it in one place recognises
    // it in the others.
    const named = SIDEBAR.match(/"Workbench canvases"/g) ?? [];
    expect(named.length).toBe(3);
    expect(SIDEBAR).not.toMatch(/label: "Canvases"/);
    expect(SIDEBAR).not.toMatch(/label="Canvases"/);
  });

  it("shows the row and the rail icon behind the SAME sign-in gate", () => {
    // A row that exists until you collapse the sidebar is a destination people
    // lose — both halves are gated, or neither should be.
    expect(SIDEBAR).toMatch(/const canvasesLoggedIn = useCanvasesLoggedIn\(\)/);
    // The rail entry, spread in conditionally.
    expect(SIDEBAR).toMatch(/\.\.\.\(canvasesLoggedIn\s*\n?\s*\?/);
    // The expanded row.
    expect(SIDEBAR).toMatch(/\{canvasesLoggedIn && \(\s*\n\s*<NavItem/);
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
    expect(SIDEBAR).toMatch(/\{ href: "\/canvases", label: "Workbench canvases"/);
    expect(SIDEBAR).toMatch(/!\(canvasesLoggedIn && e\.href === "\/canvases"\)/);
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
    expect(PAGE).toMatch(/publishLoggedIn\(status\.logged_in\)/);
    expect(STORE).toMatch(/window\.setTimeout\(poll, POLL_MS\)/);
    // A failed read is not a sign-out — the row survives a server restart.
    expect(STORE).toMatch(/catch \{\n\s*\/\/ A failed read is not a sign-out/);
  });
});
