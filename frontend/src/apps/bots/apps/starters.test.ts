import { describe, expect, test } from "bun:test";
import type { Starter } from "../lib/api";
import { busyLabel, needsStatus, starterBadge, starterOf, starterRows, toInstall, updateButton, updateConfirm, withStatus } from "./starters";

const S = (key: string, over: Partial<Starter> = {}): Starter => ({
  key, name: key.toUpperCase(), desc: "", version: "1.2.0", tools: 3, icon: "", setup_tool: "", ready_key: "connected",
  installed: false, dir: "", installed_version: "", update: false, ...over,
});

describe("starterBadge", () => {
  test("nothing until installed", () => {
    expect(starterBadge({ installed: false, ready: null })).toBeNull();
    expect(starterBadge({ installed: false, ready: true })).toBeNull();
  });
  test("Needs setup / Ready / Installed", () => {
    expect(starterBadge({ installed: true, ready: false })).toEqual({ text: "Needs setup", cls: "badge setup", title: "Open the app once and finish its setup (the Google apps need a service-account key)" });
    expect(starterBadge({ installed: true, ready: true })).toEqual({ text: "Ready", cls: "badge ready" });
    expect(starterBadge({ installed: true, ready: null })).toEqual({ text: "Installed", cls: "badge" });
  });
});

describe("strip vs gallery", () => {
  test("the strip keeps only what is not installed; an installed starter is a gallery card", () => {
    const rows = starterRows([S("a"), S("b", { installed: true, dir: "/x/b" }), S("c")]);
    expect(toInstall(rows).map((r) => r.key)).toEqual(["a", "c"]);
    expect(starterOf(rows, "/x/b")?.key).toBe("b");
    expect(starterOf(rows, "/x/a")).toBeNull();
    expect(starterOf(rows, "")).toBeNull();  // a not-installed row has dir "" and must never match a card
  });
  test("nothing left to install", () => {
    expect(toInstall(starterRows([S("a", { installed: true, dir: "/x/a" })]))).toEqual([]);
  });
});

describe("updateButton", () => {
  test("only on an installed starter with a newer shipped version", () => {
    expect(updateButton({ installed: false, update: false, version: "1" })).toBeNull();
    expect(updateButton({ installed: true, update: false, version: "1" })).toBeNull();
    expect(updateButton({ installed: false, update: true, version: "1" })).toBeNull();
    expect(updateButton({ installed: true, update: true, version: "1.3.0" })).toEqual({ label: "Update", title: "Replace the installed files with the version that ships with Fused Render (v1.3.0)" });
  });
  test("busy labels", () => {
    expect(busyLabel("install")).toBe("Installing…");
    expect(busyLabel("update")).toBe("Updating…");
  });
});

describe("status", () => {
  test("only asked for when an installed starter has a setup tool", () => {
    expect(needsStatus(starterRows([S("a"), S("b", { installed: true })]))).toBe(false);
    expect(needsStatus(starterRows([S("a", { setup_tool: "docs_status" })]))).toBe(false);
    expect(needsStatus(starterRows([S("a", { installed: true, setup_tool: "docs_status" })]))).toBe(true);
  });
  test("rows start at ready: null; the reply only changes the keys it reports", () => {
    const rows = starterRows([S("a", { installed: true }), S("b", { installed: true })]);
    expect(rows.map((r) => r.ready)).toEqual([null, null]);
    expect(withStatus(rows, { a: false }).map((r) => r.ready)).toEqual([false, null]);
    expect(withStatus(rows, null)).toEqual(rows);
    expect(starterRows(undefined)).toEqual([]);
  });
});

test("update confirm text", () => {
  expect(updateConfirm({ name: "Docs", dir: "/x/docs", version: "2" })).toEqual([
    "Update Docs?",
    "This replaces the app's files under /x/docs with the ones that ship with Fused Render (v2). Edits a build made to that copy are lost; its saved key and library under .fused/ stay.",
  ]);
});
