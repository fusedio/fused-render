// Switching groups in Preferences is an in-place param sync: the page keeps
// its mount, so the nav stays put and nothing is fetched again. A navigation
// would remount it (App keys the route on the nav epoch), replaying the
// fade-in and the skeleton on every click.
//
// A stubbed `globalThis.fetch`, as in indexing.render.test.tsx: the api
// helpers are thin fetch wrappers and `mock.module` leaks across files.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { default: Preferences } = await import("./Preferences");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const realFetch = globalThis.fetch;
const realLocation = globalThis.location;
const realHistory = globalThis.history;
let hits: Record<string, number> = {};
let pushes = 0;
let scrolled: string[] = [];
const doc = globalThis.document as unknown as { getElementById?: unknown };
const realGetById = doc.getElementById;

// Every settings control reads its own corner of the prefs (`prefs.reader.
// enabled`, …); a page mounted for its navigation needs none of the values, so
// the prefs answer is a stand-in that has every corner and is off in all.
function emptyPrefs(): unknown {
  return new Proxy({}, { get: (_t, k) => (k === "then" || typeof k === "symbol" ? undefined : "") });
}

function fakeFetch(url: string | URL): Promise<Response> {
  const u = String(url);
  const key = u.split("?")[0];
  hits[key] = (hits[key] ?? 0) + 1;
  const json = (b: unknown) => Promise.resolve(new Response(JSON.stringify(b), { status: 200 }));
  if (key === "/api/prefs") return Promise.resolve({ ok: true, status: 200, json: async () => emptyPrefs(), text: async () => "{}" } as unknown as Response);
  if (key === "/api/claude-config/status") return json({ available: true });
  if (key === "/api/claude-config/preferences")
    return json({
      schema: [{ key: "model", label: "Model", group: "Model", control: "text" }],
      prefs: {},
    });
  return json({});
}

beforeEach(() => {
  hits = {};
  pushes = 0;
  scrolled = [];
  doc.getElementById = (id: string) => ({ scrollIntoView: () => scrolled.push(id) });
  const loc = { pathname: "/preferences", search: "", href: "http://localhost/preferences", origin: "http://localhost", assign() {}, reload() {} };
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
  doc.getElementById = realGetById;
  globalThis.fetch = realFetch;
  globalThis.location = realLocation;
  globalThis.history = realHistory;
});

async function mount() {
  let box!: ReactTestRenderer;
  await act(async () => {
    box = create(createElement(Preferences));
  });
  for (let i = 0; i < 5; i++) await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  return box;
}
const nav = (box: ReactTestRenderer) => box.root.findByProps({ className: "settings-nav" });
const navItems = (box: ReactTestRenderer) =>
  box.root.findAll((n) => typeof n.props.className === "string" && n.props.className.includes("settings-nav-item") && typeof n.type !== "string");
const title = (box: ReactTestRenderer) => textOf(box.root.findByType("h2"));

describe("switching groups in Preferences", () => {
  test("keeps the mounted page: one fetch each, same nav, no skeleton", async () => {
    const box = await mount();
    const before = nav(box);
    const first = title(box);
    const others = navItems(box).filter((n) => n.props["aria-current"] !== "page");
    expect(others.length).toBeGreaterThan(1);
    const target = others[0];
    const label = textOf(target).replace(/\d+$/, "");
    await act(async () => {
      target.props.onClick();
    });
    expect(nav(box)).toBe(before);
    expect(title(box)).not.toBe(first);
    expect(title(box)).toContain(label);
    expect(JSON.stringify(box.toJSON())).not.toContain("Loading preferences");
    expect(hits["/api/prefs"]).toBe(1);
    expect(hits["/api/claude-config/preferences"]).toBe(1);
    expect(pushes).toBe(0);
    expect(location.search).toContain("tab=");
  });

  test("a jump from a search hit selects the group and flashes the row", async () => {
    const box = await mount();
    const before = nav(box);
    await act(async () => {
      box.root.find((n) => typeof n.type !== "string" && n.props["aria-label"] === "Search settings").props.onChange({ target: { value: "model" } });
    });
    const crumb = box.root.findAll((n) => n.type === "button" && n.props.className === "settings-crumb" && textOf(n).includes("Claude"))[0];
    expect(crumb).toBeDefined();
    await act(async () => {
      crumb.props.onClick();
    });
    expect(nav(box)).toBe(before);
    expect(box.root.findAll((n) => n.props.className === "settings-crumb")).toHaveLength(0);
    expect(location.search).toContain("tab=claude-cli");
    expect(location.search).toContain("s=claude-pref%3Amodel");
    expect(location.search).not.toContain("q=");
    const lit = box.root.findAll(
      (n) => n.type === "div" && String(n.props.className).includes("settings-row-flash"),
    );
    expect(lit).toHaveLength(1);
    expect(textOf(lit[0])).toContain("Model");
    expect(scrolled).toEqual(["setting-claude-pref:model"]);
    expect(hits["/api/prefs"]).toBe(1);
  });
});
