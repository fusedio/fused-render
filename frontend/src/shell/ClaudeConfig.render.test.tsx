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
const DEMO = { slug: "demo", name: "demo", description: "", source: "user", linked: false, path: "/x", location: "user", paths: [], readers: [] };
let skillRows: unknown[] = [];
const EMPTY = { skills: [], plugins: [], marketplaces: [], servers: [], projects: [], files: [], skipped: 0 };
let fetched: string[] = [];
function fakeFetch(url: unknown): Promise<Response> {
  fetched.push(String(url));
  const data = { ...EMPTY, skills: skillRows };
  const body = JSON.stringify(data);
  return Promise.resolve({ ok: true, status: 200, json: async () => data, text: async () => body } as unknown as Response);
}

function setLocation(search: string) {
  const loc = { pathname: "/claude-config", search, href: "http://localhost/claude-config", origin: "http://localhost", assign() {}, reload() {} };
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
}
beforeEach(() => {
  pushes = 0;
  fetched = [];
  skillRows = [];
  setLocation("");
  globalThis.fetch = fakeFetch as unknown as typeof fetch;
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

  test("a tab already visited is not refetched when it is switched back to", async () => {
    const box = await mount();
    const pick = async (label: string) => {
      const target = navItems(box).find((n) => textOf(n).includes(label))!;
      await act(async () => {
        target.props.onClick();
      });
      for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
    };
    await pick("Plugins");
    const after = fetched.length;
    await pick("Skills");
    await pick("Plugins");
    expect(fetched.length).toBe(after);
  });

  test("a deep link opens the tab it names", async () => {
    setLocation("?tab=plugins");
    const box = await mount();
    const current = navItems(box).find((n) => n.props["aria-current"] === "page")!;
    expect(textOf(current)).toContain("Plugins");
  });

  test("picking another tab drops a skill deep link from the URL", async () => {
    setLocation("?tab=skills&skill=user%2Fdemo");
    const box = await mount();
    const target = navItems(box).find((n) => textOf(n).includes("Memory"))!;
    await act(async () => {
      target.props.onClick();
    });
    expect(location.search).toContain("tab=memory");
    expect(location.search).not.toContain("skill=");
  });

  test("leaving an open skill editor and coming back shows the Skills list", async () => {
    skillRows = [DEMO];
    setLocation("?tab=skills&skill=user%2Fdemo");
    const box = await mount();
    expect(textOf(box.root)).not.toContain("Your skills");
    const pick = async (label: string) => {
      const target = navItems(box).find((n) => textOf(n).includes(label))!;
      await act(async () => {
        target.props.onClick();
      });
      for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
    };
    await pick("Plugins");
    await pick("Skills");
    expect(location.search).not.toContain("skill=");
    expect(textOf(box.root)).toContain("Your skills");
  });

  test("the nav has no heading above the tabs", async () => {
    const box = await mount();
    expect(nav(box).findAll((n) => n.props.className === "settings-nav-heading")).toHaveLength(0);
  });
});
