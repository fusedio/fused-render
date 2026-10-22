// The "Chromium engine for windows" row (Optional features): the switch
// writes chromium_windows_enabled via a real PUT /api/prefs; the status line
// names the shipped engine, or says the build has none (switch off, disabled).
//
// A stubbed `globalThis.fetch`, not `mock.module` — same reason as
// indexing.render.test.tsx: the api helpers are thin fetch wrappers, and
// replacing the whole module process-wide leaks into other test files.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import type { ChromiumEngineStatus, ChromiumWindowsState, Prefs } from "@platform/lib/api";
import { Switch } from "@platform/shadcn/ui/switch";
import { Button } from "@platform/shadcn/ui/button";
import { ChromiumWindowsControl } from "@shell/settings/general";

const sw = (box: ReactTestRenderer) => box.root.findByType(Switch);
const press = (box: ReactTestRenderer) => sw(box).props.onCheckedChange(!sw(box).props.checked);

function textOf(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map(textOf).join("");
}
const text = (box: ReactTestRenderer) => textOf(box.root);
const button = (box: ReactTestRenderer, label: string) =>
  box.root.findAll((n) => n.type === Button && textOf(n) === label)[0];

function cwState(enabled: boolean, engine: Partial<ChromiumEngineStatus>, stale = 0): ChromiumWindowsState {
  return {
    enabled,
    available: true,
    engine: { state: "ready", cef_version: "139.0.37+gb457b0b+chromium-139.0.7258.139", path: "/App", ...engine },
    stale_windows: stale,
  };
}
function fakePrefs(cw: ChromiumWindowsState): Prefs {
  return { chromium_windows: cw } as unknown as Prefs;
}

const realFetch = globalThis.fetch;
let calls: { method: string; url: string; body: unknown }[] = [];
let putResponse: ChromiumWindowsState = cwState(false, {}, 1);

function fakeFetch(url: string | URL, init?: RequestInit): Promise<Response> {
  const u = String(url);
  const method = init?.method ?? "GET";
  calls.push({ method, url: u, body: init?.body ? JSON.parse(String(init.body)) : undefined });
  const ok = (v: unknown) => Promise.resolve(new Response(JSON.stringify(v), { status: 200 }));
  if (u === "/api/prefs" && method === "PUT") return ok(fakePrefs(putResponse));
  throw new Error("chromium.render.test.tsx: unexpected fetch " + method + " " + u);
}

beforeEach(() => {
  calls = [];
  globalThis.fetch = fakeFetch as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

// A host that re-renders with whatever the row hands its parent, like the page.
async function mount(start: Prefs) {
  let current = start;
  let box!: ReactTestRenderer;
  const render = () =>
    createElement(ChromiumWindowsControl, {
      prefs: current,
      onChange: (p: Prefs) => {
        current = p;
        box.update(render());
      },
    });
  await act(async () => {
    box = create(render());
  });
  return { box, prefs: () => current };
}

describe("Chromium engine for windows", () => {
  test("ready renders checked with the CEF version, no download or remove", async () => {
    const { box } = await mount(fakePrefs(cwState(true, {})));
    expect(sw(box).props.checked).toBe(true);
    expect(sw(box).props.disabled).toBe(false);
    expect(text(box)).toContain("Chromium 139.0.37. New windows use it");
    expect(text(box)).not.toContain("ownload");
    expect(button(box, "Remove engine")).toBeUndefined();
    expect(calls).toHaveLength(0);
    await act(async () => box.unmount());
  });

  test("toggling off PUTs chromium_windows_enabled=false", async () => {
    // (The stale-windows dialog mounts a Modal, which needs a document the
    // test renderer has none of; the reopen path is the server's test.)
    putResponse = cwState(false, {}, 0);
    const { box, prefs } = await mount(fakePrefs(cwState(true, {})));
    await act(async () => press(box));
    expect(calls).toEqual([{ method: "PUT", url: "/api/prefs", body: { chromium_windows_enabled: false } }]);
    expect(prefs().chromium_windows?.enabled).toBe(false);
    expect(sw(box).props.checked).toBe(false);
    expect(text(box)).toContain("WebKit. New windows use it");
    await act(async () => box.unmount());
  });

  test("a build without the engine reads off and disabled, with a WebKit line", async () => {
    // The preference defaults on, yet with no engine to load the switch must
    // not promise Chromium under a line saying WebKit (an Intel Mac).
    const { box } = await mount(fakePrefs(cwState(true, { state: "missing", cef_version: null })));
    expect(sw(box).props.disabled).toBe(true);
    expect(sw(box).props.checked).toBe(false);
    expect(text(box)).toContain("This build ships no Chromium engine; windows use WebKit.");
    await act(async () => box.unmount());
  });
});
