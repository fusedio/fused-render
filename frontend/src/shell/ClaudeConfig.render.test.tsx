// Switching tabs on the Claude Config page is an in-place param sync: the page
// keeps its mount, so the nav stays put. A navigation would remount it (App
// keys the route on the nav epoch) and replay the fade-in on every click.
//
// A stubbed `globalThis.fetch`, as in Preferences.render.test.tsx: the api
// helpers are thin fetch wrappers and `mock.module` leaks across files.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { default: ClaudeConfig } = await import("./ClaudeConfig");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const realFetch = globalThis.fetch;
const realLocation = globalThis.location;
const realHistory = globalThis.history;
let pushes = 0;

// Every module answers with empty lists, so each tab mounts on an empty page.
const EMPTY = { skills: [], plugins: [], marketplaces: [], servers: [], projects: [], files: [], skipped: 0 };
function fakeFetch(): Promise<Response> {
  const body = JSON.stringify(EMPTY);
  return Promise.resolve({ ok: true, status: 200, json: async () => EMPTY, text: async () => body } as unknown as Response);
}

beforeEach(() => {
  pushes = 0;
  const loc = { pathname: "/claude-config", search: "", href: "http://localhost/claude-config", origin: "http://localhost", assign() {}, reload() {} };
  globalThis.location = loc as unknown as Location;
  globalThis.history = {
    state: null,
    pushState() {
      pushes += 1;
    },
    replaceState(_s: unknown, _t: string, url: string) {
      const q = String(url).split("?")[1];
      loc.search = q ? "?" + q : "";
    },
  } as unknown as History;
  globalThis.fetch = fakeFetch as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  globalThis.location = realLocation;
  globalThis.history = realHistory;
});

async function mount() {
  let box!: ReactTestRenderer;
  await act(async () => {
    box = create(createElement(ClaudeConfig));
  });
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  return box;
}
const nav = (box: ReactTestRenderer) => box.root.findByProps({ className: "settings-nav" });
const navItems = (box: ReactTestRenderer) =>
  box.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("settings-nav-item") && typeof n.type !== "string");

describe("switching tabs on the Claude Config page", () => {
  test("keeps the mounted nav and syncs ?tab= in place", async () => {
    const box = await mount();
    const before = nav(box);
    const target = navItems(box).find((n) => textOf(n).includes("Plugins"))!;
    await act(async () => {
      target.props.onClick();
    });
    expect(nav(box)).toBe(before);
    expect(location.search).toContain("tab=plugins");
    expect(pushes).toBe(0);
  });

  test("the nav has no heading above the tabs", async () => {
    const box = await mount();
    expect(nav(box).findAll((n) => n.props.className === "settings-nav-heading")).toHaveLength(0);
  });
});
