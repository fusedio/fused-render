// The tab shell: which tab opens, and that a click writes the URL once, in the
// handler (the effect-free rule).
import { afterEach, expect, mock, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import MonitorPage from "@shell/monitor/MonitorPage";
import type { MonitorParams } from "@shell/monitor/monitor-net-lib";

let renderer: ReactTestRenderer | null = null;
const realFetch = globalThis.fetch;
afterEach(() => {
  act(() => renderer?.unmount());
  renderer = null;
  globalThis.fetch = realFetch;
});
const text = (n: ReactTestInstance | string): string =>
  typeof n === "string" ? n : n.children.map((c) => text(c as ReactTestInstance | string)).join("");

async function mount(initial?: MonitorParams) {
  // Every tab's poll fails fast: this file is about the shell, not the tabs.
  globalThis.fetch = mock(async () => new Response("{}", { status: 500 })) as never;
  await act(async () => {
    renderer = create(<MonitorPage initial={initial} pollMs={1e9} />);
  });
  return renderer!;
}
const tabs = (r: ReactTestRenderer) => r.root.findAll((n) => n.props.role === "tab");

test("opens on Overview by default with three tabs", async () => {
  const r = await mount();
  expect(tabs(r).map((t) => text(t))).toEqual(["Overview", "Network", "Processes"]);
  expect(tabs(r).map((t) => t.props["aria-selected"])).toEqual([true, false, false]);
  expect(r.root.findAll((n) => n.props.className?.includes?.("monitor-overview"))).not.toHaveLength(0);
});

test("a deep link opens its tab; a click switches and writes the URL in the handler", async () => {
  const r = await mount({ tab: "network", source: "page-a", route: "/api/x", pid: null });
  expect(tabs(r).map((t) => t.props["aria-selected"])).toEqual([false, true, false]);
  const writes: string[] = [];
  const realHistory = (globalThis as { history?: unknown }).history;
  (globalThis as { history?: unknown }).history = {
    state: null,
    replaceState: (_s: unknown, _t: string, url: string) => writes.push(url),
  };
  try {
    await act(async () => tabs(r)[2].props.onClick({ button: 0, preventDefault() {} }));
    expect(writes).toEqual(["/monitor?tab=processes"]);
    expect(tabs(r).map((t) => t.props["aria-selected"])).toEqual([false, false, true]);
  } finally {
    (globalThis as { history?: unknown }).history = realHistory;
  }
});

test("Overview and Network scroll as a page; Processes keeps the fixed page", async () => {
  const root = (r: ReactTestRenderer) => r.root.findAll((n) => typeof n.type === "string" && /\bmonitor-page\b/.test(n.props.className ?? ""))[0];
  const o = await mount();
  expect(root(o).props.className).toContain("is-page-scroll");
  act(() => o.unmount());
  const p = await mount({ tab: "processes" } as MonitorParams);
  expect(root(p).props.className).not.toContain("is-page-scroll");
});
