// The "Chromium engine for windows" row (Optional features): the switch
// writes chromium_windows_enabled via a real PUT /api/prefs, and the status
// line follows the engine's download state — a percentage and a 1 s poll of
// GET /api/prefs/chromium while downloading, the error and a Retry button
// after a failure, the CEF version and a Remove button once ready.
//
// A stubbed `globalThis.fetch`, not `mock.module` — same reason as
// indexing.render.test.tsx: the api helpers are thin fetch wrappers, and
// replacing the whole module process-wide leaks into other test files.
import { afterEach, beforeEach, describe, expect, jest, test } from "bun:test";
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

function cwState(enabled: boolean, engine: Partial<ChromiumEngineStatus>): ChromiumWindowsState {
  return {
    enabled,
    available: true,
    engine: {
      state: "missing",
      progress: 0,
      error: null,
      engine_version: null,
      cef_version: null,
      path: null,
      ...engine,
    },
  };
}
function fakePrefs(cw: ChromiumWindowsState): Prefs {
  return { chromium_windows: cw } as unknown as Prefs;
}

const realFetch = globalThis.fetch;
let calls: { method: string; url: string; body: unknown }[] = [];
let putResponse: ChromiumWindowsState = cwState(true, { state: "downloading", progress: 0 });
let getResponse: ChromiumWindowsState = cwState(true, { state: "downloading", progress: 0.5 });
let postResponse: ChromiumWindowsState = cwState(false, { state: "missing" });

function fakeFetch(url: string | URL, init?: RequestInit): Promise<Response> {
  const u = String(url);
  const method = init?.method ?? "GET";
  calls.push({ method, url: u, body: init?.body ? JSON.parse(String(init.body)) : undefined });
  const ok = (v: unknown) => Promise.resolve(new Response(JSON.stringify(v), { status: 200 }));
  if (u === "/api/prefs" && method === "PUT") return ok(fakePrefs(putResponse));
  if (u === "/api/prefs/chromium" && method === "GET") return ok(getResponse);
  if (u.startsWith("/api/prefs/chromium/") && method === "POST") return ok(postResponse);
  throw new Error("chromium.render.test.tsx: unexpected fetch " + method + " " + u);
}

beforeEach(() => {
  calls = [];
  globalThis.fetch = fakeFetch as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  jest.useRealTimers();
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
  test("renders unchecked when off, with the download hint", async () => {
    const { box } = await mount(fakePrefs(cwState(false, { state: "missing" })));
    expect(sw(box).props.checked).toBe(false);
    expect(sw(box).props.disabled).toBe(false);
    expect(text(box)).toContain("Turning this on downloads the engine.");
    await act(async () => box.unmount());
  });

  test("toggling on PUTs chromium_windows_enabled=true and shows the download", async () => {
    jest.useFakeTimers();
    putResponse = cwState(true, { state: "downloading", progress: 0, phase: "download" });
    const { box } = await mount(fakePrefs(cwState(false, { state: "missing" })));
    await act(async () => press(box));
    expect(calls[0]).toEqual({ method: "PUT", url: "/api/prefs", body: { chromium_windows_enabled: true } });
    expect(sw(box).props.checked).toBe(true);
    expect(text(box)).toContain("Downloading engine… 0%");
    await act(async () => box.unmount());
  });

  test("the switch is disabled while the engine is unpublished", async () => {
    const { box } = await mount(fakePrefs(cwState(false, { state: "unpublished" })));
    expect(sw(box).props.disabled).toBe(true);
    expect(text(box)).toContain("Engine not published for this build yet.");
    await act(async () => box.unmount());
  });

  test("downloading renders a percentage and polls GET /api/prefs/chromium until it finishes", async () => {
    jest.useFakeTimers();
    getResponse = cwState(true, { state: "downloading", progress: 0.42, phase: "download" });
    const { box, prefs } = await mount(fakePrefs(cwState(true, { state: "downloading", progress: 0.1 })));
    expect(text(box)).toContain("Downloading engine… 10%");
    expect(calls).toHaveLength(0);

    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual(["GET /api/prefs/chromium"]);
    expect(text(box)).toContain("Downloading engine… 42%");

    getResponse = cwState(true, { state: "downloading", progress: 0.95, phase: "unpack" });
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(text(box)).toContain("Unpacking…");

    getResponse = cwState(true, { state: "ready", cef_version: "130.1.2" });
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(text(box)).toContain("Engine ready (CEF 130.1.2)");
    expect(prefs().chromium_windows?.engine.state).toBe("ready");

    // Left "downloading": no more polls.
    const n = calls.length;
    await act(async () => {
      jest.advanceTimersByTime(5000);
    });
    expect(calls).toHaveLength(n);
    await act(async () => box.unmount());
  });

  test("unmounting stops the poll", async () => {
    jest.useFakeTimers();
    const { box } = await mount(fakePrefs(cwState(true, { state: "downloading", progress: 0.1 })));
    await act(async () => box.unmount());
    await act(async () => {
      jest.advanceTimersByTime(5000);
    });
    expect(calls).toHaveLength(0);
  });

  test("failed renders the error and Retry POSTs /api/prefs/chromium/install", async () => {
    jest.useFakeTimers();
    postResponse = cwState(true, { state: "downloading", progress: 0 });
    getResponse = cwState(true, { state: "downloading", progress: 0.3 });
    const { box } = await mount(fakePrefs(cwState(true, { state: "failed", error: "checksum mismatch" })));
    expect(text(box)).toContain("checksum mismatch");
    const retry = button(box, "Retry download");
    expect(retry).toBeDefined();
    await act(async () => retry.props.onClick());
    expect(calls[0]).toMatchObject({ method: "POST", url: "/api/prefs/chromium/install" });
    expect(text(box)).toContain("Downloading engine… 0%");
    // And the poll starts again.
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(calls[1]).toMatchObject({ method: "GET", url: "/api/prefs/chromium" });
    expect(text(box)).toContain("Downloading engine… 30%");
    await act(async () => box.unmount());
  });

  test("ready renders the CEF version and Remove POSTs /api/prefs/chromium/remove", async () => {
    postResponse = cwState(false, { state: "missing" });
    const { box, prefs } = await mount(fakePrefs(cwState(true, { state: "ready", cef_version: "130.1.2" })));
    expect(text(box)).toContain("Engine ready (CEF 130.1.2)");
    expect(sw(box).props.checked).toBe(true);
    await act(async () => button(box, "Remove engine").props.onClick());
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ method: "POST", url: "/api/prefs/chromium/remove" });
    expect(prefs().chromium_windows?.enabled).toBe(false);
    expect(sw(box).props.checked).toBe(false);
    await act(async () => box.unmount());
  });

  test("a bundled engine is ready with nothing to remove", async () => {
    const { box } = await mount(fakePrefs(cwState(true, { state: "ready", cef_version: "130.1.2", bundled: true })));
    expect(text(box)).toContain("Engine ready (CEF 130.1.2)");
    expect(sw(box).props.checked).toBe(true);
    expect(button(box, "Remove engine")).toBeUndefined();
    expect(calls).toHaveLength(0);
    await act(async () => box.unmount());
  });
});
