// The /monitor page rendered against a fixed whole-machine payload (`load`
// injected, the poll parked on a huge interval). The one network call that
// matters — the kill — goes through the REAL `killSystemProcess` with
// `globalThis.fetch` stubbed, so the X-Fused guard header is what is asserted,
// not a mock's call record.
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";

import type { MachineSnapshot, MachineProc } from "@platform/lib/sysmon";
import MonitorPage from "@shell/monitor/MonitorPage";

const GB = 1024 ** 3;
const MB = 1024 ** 2;

function proc(over: Partial<MachineProc> & { pid: number }): MachineProc {
  return {
    ppid: 1,
    user: "me",
    command: `cmd${over.pid}`,
    args: `/usr/bin/cmd${over.pid} --flag`,
    cpuPct: 1,
    memBytes: 10 * MB,
    startedAt: 1000 + over.pid,
    fused: false,
    kind: "system",
    label: over.command ?? `cmd${over.pid}`,
    ...over,
  };
}

const PROCS: MachineProc[] = [
  proc({ pid: 100, command: "python", fused: true, kind: "app", label: "fused-render", cpuPct: 3, memBytes: GB }),
  proc({ pid: 201, command: "claude", fused: true, kind: "claude", label: "Claude: fix the bug", cpuPct: 12.34 }),
  proc({ pid: 300, command: "sleep", args: "sleep 300", cpuPct: 0, memBytes: 1 * MB }),
  proc({ pid: 400, command: "Safari", cpuPct: 64.2, memBytes: 2 * GB }),
  proc({ pid: 1, command: "launchd", user: "root", cpuPct: null, memBytes: null }),
  proc({ pid: 500, command: "WindowServer", user: "_windowserver", cpuPct: null, memBytes: null }),
];

const PAYLOAD: MachineSnapshot = {
  supported: true,
  host: {
    cpuPct: 34.2,
    cpuUser: 20,
    cpuSystem: 14.2,
    ncpu: 10,
    load: [2, 2, 2],
    memTotal: 16 * GB,
    memUsed: 12.6 * GB,
    memApp: 7 * GB,
    memWired: 3 * GB,
    memCompressed: 2.6 * GB,
  },
  procs: PROCS,
  history: [
    { t: 100, cpuPct: 30, memUsed: 12 * GB, appCpuPct: 10, appMemBytes: GB },
    { t: 101, cpuPct: 34, memUsed: 12.6 * GB, appCpuPct: 15, appMemBytes: GB },
  ],
  totals: { cpuPct: 15.3, memBytes: 1.01 * GB },
};

let renderer: ReactTestRenderer | null = null;
const realFetch = globalThis.fetch;

afterEach(() => {
  act(() => renderer?.unmount());
  renderer = null;
  globalThis.fetch = realFetch;
});

async function flush(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 6; i++) await Promise.resolve();
  });
}

// Mounts the page and WIDENS it to the whole machine: the page opens on
// fused-render's own processes (see "opens on fused-render only" below), and
// every test here but that one is about the whole-machine table.
async function mount(
  payload: MachineSnapshot | (() => MachineSnapshot) = PAYLOAD,
  confirmMs?: number,
  pollMs = 1e9,
): Promise<ReactTestRenderer> {
  const r = await mountNarrow(payload, confirmMs, pollMs);
  const toggle = r.root.find((n) => n.type === "button" && text(n) === "fused-render only");
  click(toggle);
  return r;
}

async function mountNarrow(
  payload: MachineSnapshot | (() => MachineSnapshot) = PAYLOAD,
  confirmMs?: number,
  pollMs = 1e9,
): Promise<ReactTestRenderer> {
  const read = typeof payload === "function" ? payload : () => payload;
  await act(async () => {
    renderer = create(
      <MonitorPage
        confirmMs={confirmMs}
        load={async () => read()}
        loadHistory={async (pids) =>
          Object.fromEntries(pids.map((p) => [String(p), [{ t: 101, cpuPct: 1, memBytes: MB }]]))
        }
        pollMs={pollMs}
      />,
    );
  });
  await flush();
  return renderer!;
}

function text(node: ReactTestInstance | string): string {
  if (typeof node === "string") return node;
  return node.children.map((c) => text(c as ReactTestInstance | string)).join("");
}

function rows(r: ReactTestRenderer): ReactTestInstance[] {
  return r.root.findAll((n) => n.type === "tr" && n.props["data-pid"] !== undefined);
}

function row(r: ReactTestRenderer, pid: number): ReactTestInstance {
  return r.root.find((n) => n.type === "tr" && n.props["data-pid"] === pid);
}

const pidsOf = (r: ReactTestRenderer) => rows(r).map((n) => n.props["data-pid"] as number);

const click = (n: ReactTestInstance, mods: Record<string, boolean> = {}) =>
  act(() => {
    n.props.onClick({ stopPropagation() {}, preventDefault() {}, button: 0, ...mods });
  });

test("renders a row per process, CPU desc by default, unmeasured rows last with —", async () => {
  const r = await mount();
  expect(pidsOf(r)).toEqual([400, 201, 100, 300, 1, 500]);
  const launchd = text(row(r, 1));
  expect(launchd).toContain("launchd");
  expect(launchd).toContain("root");
  expect(launchd.match(/—/g)?.length).toBe(2); // CPU and Memory
  const claude = text(row(r, 201));
  expect(claude).toContain("Claude: fix the bug"); // our rows read by label…
  expect(claude).toContain("Claude"); // …with their kind chip
  expect(claude).toContain("12.3");
  expect(text(row(r, 400))).toContain("2.00 GB");
  expect(text(r.root)).toContain("6 processes");
  expect(text(r.root)).toContain("12.6 of 16 GB");
});

test("sorting by Memory keeps nulls last in both directions", async () => {
  const r = await mount();
  const memHeader = r.root.findAll((n) => n.type === "button" && n.props.className === "monitor-sort")[4];
  click(memHeader);
  expect(pidsOf(r)).toEqual([400, 100, 201, 300, 1, 500]);
  click(memHeader);
  expect(pidsOf(r).slice(0, 4)).toEqual([300, 201, 100, 400]);
  expect(pidsOf(r).slice(4)).toEqual([1, 500]);
});

test("search matches name, command line, user and pid", async () => {
  const r = await mount();
  const input = r.root.find((n) => n.type === "input" && n.props.type === "search");
  const search = (q: string) => act(() => input.props.onChange({ target: { value: q } }));
  search("saf");
  expect(pidsOf(r)).toEqual([400]);
  search("root");
  expect(pidsOf(r)).toEqual([1]);
  search("--flag");
  expect(pidsOf(r)).toHaveLength(5); // every row but sleep, whose args are "sleep 300"
  search("500");
  expect(pidsOf(r)).toEqual([500]);
  expect(text(r.root)).toContain("1 of 6 processes");
});

test("opens on fused-render only: our rows, the app's cost, and the toggle pressed", async () => {
  const r = await mountNarrow();
  const toggle = r.root.find((n) => n.type === "button" && text(n) === "fused-render only");
  expect(toggle.props["aria-pressed"]).toBe(true);
  expect(pidsOf(r)).toEqual([201, 100]);
  expect(text(r.root)).toContain("2 processes");
  expect(text(r.root)).toContain("fused-render and the processes it runs");
  click(toggle);
  expect(pidsOf(r)).toEqual([400, 201, 100, 300, 1, 500]);
  expect(text(r.root)).toContain("6 processes");
  expect(text(r.root)).toContain("Every process on");
});

test("fused-render only narrows the table and shows what the app costs", async () => {
  const r = await mount();
  const toggle = r.root.find((n) => n.type === "button" && text(n) === "fused-render only");
  click(toggle);
  expect(pidsOf(r)).toEqual([201, 100]);
  const stat = r.root.find((n) => n.props["aria-label"] === "fused-render's own cost");
  expect(text(stat)).toContain("15.3%"); // 12.34 + 3
  expect(text(stat)).toContain("Processes2");
});

const bar = (r: ReactTestRenderer) => r.root.find((n) => n.props.className === "monitor-selbar");
const bars = (r: ReactTestRenderer) => r.root.findAll((n) => n.props.className === "monitor-selbar");
const button = (n: ReactTestInstance, label: string) =>
  n.find((b) => b.type === "button" && text(b) === label);
const wait = (ms: number) => act(async () => new Promise((res) => setTimeout(res, ms)));

function captureFetch(reply: unknown = { ok: true, signal: "SIGTERM" }) {
  const calls: { url: string; init: RequestInit }[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return { ok: true, status: 200, json: async () => reply };
  }) as unknown as typeof fetch;
  return calls;
}

test("rows carry no buttons at all; there is no Actions column", async () => {
  const r = await mount();
  for (const tr of rows(r)) expect(tr.findAll((n) => n.type === "button")).toHaveLength(0);
  expect(text(r.root)).not.toContain("Actions");
  expect(bars(r)).toHaveLength(0); // nothing selected, no action bar
});

test("the app's own row cannot be selected; 'This app' is its checkbox tooltip", async () => {
  const r = await mount();
  const app = row(r, 100);
  const box = app.find((n) => n.type === "input");
  expect(box.props.disabled).toBe(true);
  expect(box.props.title).toBe("This app");
  click(app);
  expect(bars(r)).toHaveLength(0);
  expect(r.root.findAll((n) => n.props.className === "monitor-dock")).toHaveLength(0);
});

test("selecting puts the actions in the toolbar; Kill confirms there, then POSTs /kill with X-Fused", async () => {
  const calls = captureFetch();
  const r = await mount();
  click(row(r, 300));
  const toolbar = r.root.find((n) => n.props.className === "monitor-toolbar");
  expect(toolbar.findAll((n) => n.props.className === "monitor-selbar")).toHaveLength(1);
  expect(text(bar(r))).toContain("1 selected");
  click(button(bar(r), "Kill"));
  expect(text(bar(r))).toContain("Kill sleep (300)?");
  expect(calls).toHaveLength(0); // nothing sent before the confirm
  await act(async () => button(bar(r), "Kill").props.onClick());
  await flush();
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toBe("/api/system/activity/kill");
  expect((calls[0].init.headers as Record<string, string>)["X-Fused"]).toBe("1");
  expect(JSON.parse(calls[0].init.body as string)).toEqual({ pid: 300, startedAt: 1300, force: false });
});

test("Cancel closes the confirm without a request", async () => {
  const calls = captureFetch({});
  const r = await mount();
  click(row(r, 400));
  click(button(bar(r), "Kill"));
  click(button(bar(r), "Cancel"));
  expect(text(bar(r))).toContain("1 selected");
  expect(calls).toHaveLength(0);
});

test("a selection of only our own Claude row reads Stop and uses the owner-aware /stop", async () => {
  const calls = captureFetch({ ok: true, via: "claude" });
  const r = await mount();
  click(row(r, 201));
  click(button(bar(r), "Stop"));
  await act(async () => button(bar(r), "Stop").props.onClick());
  await flush();
  expect(calls.map((c) => c.url)).toEqual(["/api/system/activity/stop"]);
  // Mixed with someone else's process, the verb is Kill again.
  click(row(r, 400), { metaKey: true });
  expect(bar(r).findAll((n) => n.type === "button" && text(n) === "Kill")).toHaveLength(1);
});

test("permission denied is tagged on the row, not thrown", async () => {
  captureFetch({ ok: false, error: "permission denied" });
  const r = await mount();
  click(row(r, 500));
  click(button(bar(r), "Kill"));
  await act(async () => button(bar(r), "Kill").props.onClick());
  await flush();
  expect(text(row(r, 500))).toContain("Permission denied");
});

test("click, Cmd-click and Shift-click select; the dock below holds the graphs", async () => {
  const r = await mount();
  click(row(r, 400));
  click(row(r, 300), { metaKey: true });
  expect(text(r.root.find((n) => n.props.className === "monitor-selcount"))).toBe("2 selected");
  click(row(r, 400));
  click(row(r, 300), { shiftKey: true }); // 400, 201, (100 is the app: skipped), 300
  expect(text(bar(r))).toContain("3 selected");
  await flush();
  const dock = r.root.find((n) => n.props.className === "monitor-dock");
  expect(text(dock)).toContain("% CPU · last minute");
  expect(text(dock)).toContain("Claude: fix the bug (201)");
  click(button(bar(r), "Clear"));
  expect(bars(r)).toHaveLength(0);
  expect(r.root.findAll((n) => n.props.className === "monitor-dock")).toHaveLength(0);
});

test("bulk Force kill confirms in the toolbar and signals every pid", async () => {
  const bodies: unknown[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    bodies.push([url, JSON.parse(init.body as string)]);
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }) as unknown as typeof fetch;
  const r = await mount();
  click(row(r, 400));
  click(row(r, 300), { metaKey: true });
  click(button(bar(r), "Force kill"));
  expect(text(bar(r))).toContain("Force kill Safari (400) and sleep (300)?");
  await act(async () => button(bar(r), "Force kill").props.onClick());
  await flush();
  expect(bodies).toEqual([
    ["/api/system/activity/kill", { pid: 400, startedAt: 1400, force: true }],
    ["/api/system/activity/kill", { pid: 300, startedAt: 1300, force: true }],
  ]);
});

test("a re-opened confirm is not closed by the first one's pending auto-cancel", async () => {
  const r = await mount(PAYLOAD, 80);
  click(row(r, 400));
  click(button(bar(r), "Kill"));
  click(button(bar(r), "Cancel"));
  await wait(50);
  click(button(bar(r), "Kill")); // a fresh confirm, 50 ms into the first one's 80 ms timer
  await wait(45);
  expect(text(bar(r))).toContain("Kill Safari (400)?"); // first timer fired: still open
  await wait(60);
  expect(text(bar(r))).toContain("1 selected"); // its own timer closes it
});

// ---- the held order -------------------------------------------------------------

/** PAYLOAD with Safari (top CPU) dropped to idle and sleep (idle) shot up. */
const RESORTED: MachineSnapshot = {
  ...PAYLOAD,
  procs: PAYLOAD.procs.map((p) =>
    p.pid === 400 ? { ...p, cpuPct: 0.1 } : p.pid === 300 ? { ...p, cpuPct: 90 } : p,
  ),
};
const NEWCOMER = proc({ pid: 777, command: "newcomer", cpuPct: 99 });

test("while the pointer is over the table the order holds; values update in place", async () => {
  let current = PAYLOAD;
  const r = await mount(() => current, undefined, 25);
  expect(pidsOf(r)).toEqual([400, 201, 100, 300, 1, 500]);
  const body = r.root.find((n) => n.type === "tbody");
  act(() => body.props.onMouseEnter());
  expect(text(r.root)).toContain("order held");
  current = { ...RESORTED, procs: [...RESORTED.procs, NEWCOMER] };
  await wait(80);
  // Same order, new values, the newcomer appended at the bottom.
  expect(pidsOf(r)).toEqual([400, 201, 100, 300, 1, 500, 777]);
  expect(text(row(r, 300))).toContain("90.0");
  // The pointer leaves with nothing selected: it re-sorts.
  act(() => body.props.onMouseLeave());
  expect(pidsOf(r)).toEqual([777, 300, 201, 100, 400, 1, 500]);
  expect(text(r.root)).not.toContain("order held");
});

test("a selection holds the order too; a header click re-sorts under it", async () => {
  let current = PAYLOAD;
  const r = await mount(() => current, undefined, 25);
  click(row(r, 500));
  current = RESORTED;
  await wait(80);
  expect(pidsOf(r)).toEqual([400, 201, 100, 300, 1, 500]);
  const cpuHeader = r.root.findAll((n) => n.type === "button" && n.props.className === "monitor-sort")[3];
  click(cpuHeader); // % CPU ascending, applied now despite the hold
  expect(pidsOf(r)).toEqual([400, 100, 201, 300, 1, 500]);
});

test("an armed confirm is disarmed by any selection change", async () => {
  const r = await mount();
  click(row(r, 400));
  click(button(bar(r), "Kill"));
  expect(text(bar(r))).toContain("Kill Safari (400)?");
  click(row(r, 300), { metaKey: true });
  expect(text(bar(r))).toContain("2 selected");
  click(button(bar(r), "Kill"));
  const input = r.root.find((n) => n.type === "input" && n.props.type === "search");
  act(() => input.props.onChange({ target: { value: "s" } }));
  expect(text(bar(r))).not.toContain("?");
});

test("a selected pid that comes back as a different process drops out", async () => {
  let current = PAYLOAD;
  const r = await mount(() => current, undefined, 25);
  click(row(r, 300));
  click(row(r, 400), { metaKey: true });
  expect(text(bar(r))).toContain("2 selected");
  current = {
    ...PAYLOAD,
    procs: PAYLOAD.procs.map((p) => (p.pid === 300 ? { ...p, startedAt: 9999 } : p)),
  };
  await wait(80);
  expect(text(bar(r))).toContain("1 selected");
  expect(row(r, 300).props.className).not.toContain("is-selected");
});
