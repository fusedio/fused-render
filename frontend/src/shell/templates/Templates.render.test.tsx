// Switching tabs on the Templates page is an in-place param sync: the page
// keeps its mount and its loaded tables. A navigation would remount it (App
// keys the route on the nav epoch), refetch the inventory and registry and
// flash the skeleton.
//
// A stubbed `globalThis.fetch`, as in Preferences.render.test.tsx.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { default: Templates } = await import("./Templates");

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}

const realFetch = globalThis.fetch;
const realLocation = globalThis.location;
const realHistory = globalThis.history;
let pushes = 0;
let fetched: string[] = [];

const BODIES: Record<string, unknown> = {
  "/api/templates/inventory": { sources: [], templates: [] },
  "/api/templates/registry": { sources: [], entries: [], builtin_registry: "", user_registry: "" },
};
function fakeFetch(url: unknown): Promise<Response> {
  const path = String(url).split("?")[0];
  fetched.push(path);
  const body = BODIES[path] ?? {};
  return Promise.resolve({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response);
}

function setLocation(search: string) {
  const loc = { pathname: "/view/_templates", search, href: "http://localhost/view/_templates" + search, origin: "http://localhost", assign() {}, reload() {} };
  globalThis.location = loc as unknown as Location;
  globalThis.history = {
    state: null,
    pushState() {
      pushes += 1;
    },
    replaceState(_s: unknown, _t: string, u: string) {
      const q = String(u).split("?")[1];
      loc.search = q ? "?" + q : "";
    },
  } as unknown as History;
}

beforeEach(() => {
  pushes = 0;
  fetched = [];
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
    box = create(createElement(Templates));
  });
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  return box;
}
const tabs = (box: ReactTestRenderer) => box.root.findByProps({ className: "templates-tabs" });
const tabButtons = (box: ReactTestRenderer) =>
  box.root.findAll((n) => n.type === "button" && typeof n.props.className === "string" && n.props.className.includes("templates-tab"));
const active = (box: ReactTestRenderer) => tabButtons(box).find((n) => n.props.className.includes("active"))!;

describe("switching tabs on the Templates page", () => {
  test("keeps the mounted tab bar, syncs ?tab= in place and does not refetch", async () => {
    const box = await mount();
    const before = tabs(box);
    const loads = fetched.length;
    expect(loads).toBeGreaterThan(0);
    const library = tabButtons(box).find((n) => textOf(n).includes("Library"))!;
    await act(async () => {
      library.props.onClick();
    });
    expect(tabs(box)).toBe(before);
    expect(textOf(active(box))).toContain("Library");
    expect(location.search).toContain("tab=library");
    expect(pushes).toBe(0);
    expect(fetched.length).toBe(loads);
  });

  test("the default tab keeps a clean URL", async () => {
    setLocation("?tab=library");
    const box = await mount();
    const bindings = tabButtons(box).find((n) => textOf(n).includes("File bindings"))!;
    await act(async () => {
      bindings.props.onClick();
    });
    expect(location.search).toBe("");
  });

  test("a deep link opens the Library tab", async () => {
    setLocation("?tab=library");
    const box = await mount();
    expect(textOf(active(box))).toContain("Library");
  });
});
