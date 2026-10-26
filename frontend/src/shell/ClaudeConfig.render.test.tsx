// Switching tabs on the Claude Code page is an in-place param sync: the page
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

// Every module answers with empty lists unless a test fills one in.
const PLUGIN = { id: "mine@mk", name: "mine", marketplace: "mk", description: "my plugin", version: "1", enabled: true, scope: "user", updateAvailable: false };
const AVAIL = (id: string, installed = false) => ({ id: `${id}@mk`, name: id, marketplace: "mk", description: `${id} does things`, keywords: [], installed, enabled: installed });
let pluginRows: unknown[] = [];
let availRows: unknown[] = [];
const EMPTY = { skills: [], plugins: [], marketplaces: [], servers: [], projects: [], files: [], skipped: 0 };
let fetched: string[] = [];
function fakeFetch(url: unknown, init?: RequestInit): Promise<Response> {
  fetched.push(String(url));
  let action = "";
  try { action = JSON.parse(String(init?.body ?? "{}")).action ?? ""; } catch { /* GET */ }
  const data =
    String(url).endsWith("/plugins") && action === "available"
      ? { plugins: availRows, skipped: [] }
      : String(url).endsWith("/plugins")
        ? { ...EMPTY, plugins: pluginRows }
        : EMPTY;
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
  pluginRows = [];
  availRows = [];
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
    await pick("Memory");
    const after = fetched.length;
    await pick("Plugins");
    await pick("Memory");
    await pick("Plugins");
    expect(fetched.length).toBeGreaterThanOrEqual(after);
    const again = fetched.length;
    await pick("Memory");
    await pick("Plugins");
    expect(fetched.length).toBe(again);
  });

  test("a deep link opens the tab it names", async () => {
    setLocation("?tab=memory");
    const box = await mount();
    const current = navItems(box).find((n) => n.props["aria-current"] === "page")!;
    expect(textOf(current)).toContain("Memory");
  });

  test("Plugins is the default tab and there is no Skills tab", async () => {
    const box = await mount();
    const current = navItems(box).find((n) => n.props["aria-current"] === "page")!;
    expect(textOf(current)).toContain("Plugins");
    expect(Array.from(new Set(navItems(box).map(textOf)))).toEqual(["Plugins", "MCP servers", "Memory"]);
  });

  test("a faint line points at the Skills page", async () => {
    const box = await mount();
    const link = box.root.findAllByType("a").find((a) => a.props.href === "/skills")!;
    expect(textOf(link)).toBe("Open Skills");
  });

  test("an empty query lists installed plugins and a Browse all row, with no Installed | Discover switch", async () => {
    pluginRows = [PLUGIN];
    availRows = [AVAIL("figma"), AVAIL("playwright"), AVAIL("mine", true)];
    const box = await mount();
    const text = textOf(box.root);
    expect(text).toContain("mine");
    expect(text).not.toContain("Discover");
    expect(text).toContain("list 2 more plugins. Type to search them.");
    expect(text).not.toContain("figma does things");
    const findmore = box.root.findByProps({ "data-testid": "plugins-findmore" });
    const browse = findmore.findAllByType("button").find((b) => textOf(b) === "Browse all")!;
    await act(async () => {
      browse.props.onClick();
    });
    expect(textOf(box.root)).toContain("figma does things");
    expect(textOf(box.root)).toContain("Available in your marketplaces");
  });

  test("a typed query lists Installed matches, then the catalog's", async () => {
    pluginRows = [PLUGIN];
    availRows = [AVAIL("figma"), AVAIL("design-kit"), AVAIL("mine", true)];
    const box = await mount();
    const input = box.root.findAllByType("input").find((i) => i.props.type === "search")!;
    await act(async () => {
      input.props.onChange({ currentTarget: { value: "design" }, target: { value: "design" }, nativeEvent: { defaultPrevented: false } });
    });
    const text = textOf(box.root);
    expect(text).toContain("Results for \"design\"");
    expect(text).toContain("Available in your marketplaces · 1");
    expect(text).toContain("design-kit");
    expect(text).not.toContain("figma does things");
    expect(text).toContain("Installed · 0");
  });

  test("a marketplace picked while searching stops filtering Installed once the search is cleared", async () => {
    pluginRows = [PLUGIN];
    availRows = [{ ...AVAIL("figma"), marketplace: "other" }];
    const box = await mount();
    const search = () => box.root.findAllByType("input").find((i) => i.props.type === "search")!;
    const type = async (value: string) =>
      act(async () => {
        search().props.onChange({ currentTarget: { value }, target: { value }, nativeEvent: { defaultPrevented: false } });
      });
    await type("e");
    const select = box.root.findByProps({ "aria-label": "Marketplace" });
    await act(async () => {
      select.props.onChange({ target: { value: "other" } });
    });
    await type("");
    const text = textOf(box.root);
    expect(box.root.findAllByProps({ "aria-label": "Marketplace" }).length).toBe(0);
    expect(text).toContain("mine");
    expect(text).not.toContain("No plugins installed");
  });

  test("the nav has no heading above the tabs", async () => {
    const box = await mount();
    expect(nav(box).findAll((n) => n.props.className === "settings-nav-heading")).toHaveLength(0);
  });
});
