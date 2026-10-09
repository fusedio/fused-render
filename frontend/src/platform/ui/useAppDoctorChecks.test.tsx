// App Doctor's live facts ride the events bus's `apps.doctor {path}` topic:
// the header dot (`useAppDoctorChecks`) follows it for as long as it is
// mounted, and the Doctor panel (`useAppDoctorReport`) follows the SAME key
// while a check task is live — so a dot, a second dot and the panel are one
// subscription in the client's refcount, and no 4 s timer re-asks anything.
// Frames come from a scripted client (`setEventsClientForTests`); the panel's
// own GET/POST from a route-keyed `fetch` stub. Never `mock.module`, which is
// process-wide in bun.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { AppCheck, AppDoctorReport } from "@platform/lib/api";

const { useAppDoctorChecks } = await import("@platform/ui/useAppDoctorChecks");
const { useAppDoctorReport } = await import("@platform/ui/AppDoctorModal");
const { setEventsClientForTests, canonicalKey } = await import("@platform/lib/events");

type Frame = (snap: unknown, delta: unknown, meta: Record<string, unknown>) => void;
interface Sub {
  topic: string;
  params: Record<string, unknown>;
  cb: Frame;
  open: boolean;
}

function fakeEvents() {
  const subs: Sub[] = [];
  const resyncs: { topic: string; params: unknown }[] = [];
  setEventsClientForTests({
    subscribe: ((topic: string, params: Record<string, unknown>, cb: Frame) => {
      const sub: Sub = { topic, params, cb, open: true };
      subs.push(sub);
      return () => {
        sub.open = false;
      };
    }) as never,
    resync: ((topic: string, params: unknown) => {
      resyncs.push({ topic, params });
      return true;
    }) as never,
  });
  return {
    subs,
    resyncs,
    open: () => subs.filter((x) => x.open),
    push: (snap: unknown, meta: Record<string, unknown> = { gen: null }) =>
      act(async () => {
        for (const x of subs) if (x.open) x.cb(snap, null, meta);
      }),
  };
}

/** A real, tiny event registry on the shim's no-op `window` for one test: the
 *  dot hears `APP_DOCTOR_CHANGED_EVENT` through it. */
function liveWindowEvents(): () => void {
  const w = globalThis.window as unknown as Record<string, unknown>;
  const was = { add: w.addEventListener, remove: w.removeEventListener, fire: w.dispatchEvent };
  const bus = new Map<string, Set<(ev: Event) => void>>();
  w.addEventListener = (type: string, fn: (ev: Event) => void) => {
    const set = bus.get(type) ?? new Set();
    set.add(fn);
    bus.set(type, set);
  };
  w.removeEventListener = (type: string, fn: (ev: Event) => void) => bus.get(type)?.delete(fn);
  w.dispatchEvent = (ev: Event) => {
    for (const fn of [...(bus.get(ev.type) ?? [])]) fn(ev);
    return true;
  };
  return () => {
    w.addEventListener = was.add;
    w.removeEventListener = was.remove;
    w.dispatchEvent = was.fire;
  };
}

const DIR = "/apps/demo";

function row(id: string, extra: Partial<AppCheck> = {}): AppCheck {
  return {
    id,
    section: "share" as AppCheck["section"],
    severity: "warning" as AppCheck["severity"],
    kind: "fact" as AppCheck["kind"],
    label: id,
    state: "pass" as AppCheck["state"],
    detail: "",
    findings: [],
    task: null,
    ondemand: false,
    check_task: null,
    verdict_task: null,
    ...extra,
  };
}

function report(checks: AppCheck[]): AppDoctorReport {
  return { path: DIR, entry: "index.html", ok: true, checks, sections: [], severities: [] };
}

const CHECKING = report([row("readme"), row("cross-browser", { ondemand: true, state: "unrun" as AppCheck["state"], check_task: { id: "t1", state: "running", run_id: "r1" } })]);
const DONE = report([row("readme"), row("cross-browser", { ondemand: true, state: "fail" as AppCheck["state"] })]);

const realFetch = globalThis.fetch;
let hits: string[] = [];
function stubFetch(routes: Record<string, () => unknown>) {
  hits = [];
  globalThis.fetch = (async (url: string) => {
    const path = url.split("?")[0]!;
    hits.push(url);
    const route = routes[path] as (() => unknown) | undefined;
    return new Response(JSON.stringify(route ? route() : { error: "unmocked" }), {
      status: route ? 200 : 404,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
}

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  globalThis.fetch = realFetch;
  setEventsClientForTests(null);
});

const settle = () =>
  act(async () => {
    await new Promise((done) => setTimeout(done, 0));
  });

function Dot({ dir, onChecks }: { dir: string | null; onChecks: (c: AppCheck[] | null) => void }) {
  onChecks(useAppDoctorChecks(dir));
  return null;
}
function mountDot(dir: string | null) {
  const seen: (AppCheck[] | null)[] = [];
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Dot, { dir, onChecks: (c) => seen.push(c) }));
  });
  mounted.push(r);
  return { r, seen, last: () => seen[seen.length - 1] };
}

type Panel = ReturnType<typeof useAppDoctorReport>;
function PanelProbe({ onPanel }: { onPanel: (p: Panel) => void }) {
  onPanel(useAppDoctorReport(DIR));
  return null;
}

// ---- the dot -------------------------------------------------------------------

test("the dot follows apps.doctor for its folder and draws each snapshot", async () => {
  const bus = fakeEvents();
  const dot = mountDot(DIR);
  expect(bus.open().map((s) => [s.topic, s.params])).toEqual([["apps.doctor", { path: DIR }]]);
  expect(dot.last()).toBeNull();
  await bus.push(CHECKING);
  expect(dot.last()?.map((c) => c.id)).toEqual(["readme", "cross-browser"]);
  await bus.push(DONE);
  expect(dot.last()?.[1].state).toBe("fail");
});

test("the dot keeps what it had over a refused frame, and subscribes to nothing without a folder", async () => {
  const bus = fakeEvents();
  const dot = mountDot(DIR);
  await bus.push(DONE);
  await act(async () => {
    for (const x of bus.open()) x.cb(null, null, { error: "internal error", status: 500 });
  });
  expect(dot.last()?.[1].state).toBe("fail");
  const none = mountDot(null);
  expect(bus.subs).toHaveLength(1);
  expect(none.last()).toBeNull();
});

test("the dot's subscription goes with the unmount", () => {
  const bus = fakeEvents();
  const dot = mountDot(DIR);
  act(() => dot.r.unmount());
  mounted.length = 0;
  expect(bus.open()).toHaveLength(0);
});

test("an on-demand Check's announcement resyncs the folder's subscription", async () => {
  const restore = liveWindowEvents();
  try {
    const bus = fakeEvents();
    mountDot(DIR);
    const { announceAppDoctorChanged } = await import("@platform/lib/tasksChanged");
    act(() => announceAppDoctorChanged("/apps/other"));
    expect(bus.resyncs).toEqual([]);
    act(() => announceAppDoctorChanged(DIR));
    expect(bus.resyncs).toEqual([{ topic: "apps.doctor", params: { path: DIR } }]);
  } finally {
    restore();
  }
});

// ---- the panel -----------------------------------------------------------------

async function mountPanel(first: AppDoctorReport) {
  stubFetch({ "/api/apps/doctor": () => first });
  let panel!: Panel;
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(PanelProbe, { onPanel: (p: Panel) => (panel = p) }));
  });
  mounted.push(r);
  await settle();
  return { r, panel: () => panel };
}

test("the panel follows apps.doctor only while a check task is live, and leaves when it is gone", async () => {
  const bus = fakeEvents();
  const { panel } = await mountPanel(CHECKING);
  expect(panel().report?.checks[1].check_task?.id).toBe("t1");
  expect(bus.open().map((s) => [s.topic, s.params])).toEqual([["apps.doctor", { path: DIR }]]);

  // The dot's cached answer from before the check started: not news.
  await bus.push(DONE, { gen: null, replay: true });
  expect(panel().report?.checks[1].check_task?.id).toBe("t1");
  // A refused frame: the row keeps saying "Checking…".
  await act(async () => {
    for (const x of bus.open()) x.cb(null, null, { error: "internal error", status: 500 });
  });
  expect(panel().report?.checks[1].check_task?.id).toBe("t1");
  expect(bus.open()).toHaveLength(1);

  // The verdict lands: the report is replaced in place and the wait is over.
  await bus.push(DONE);
  expect(panel().report?.checks[1].state).toBe("fail");
  expect(panel().report?.checks[1].check_task).toBeNull();
  expect(bus.open()).toHaveLength(0);
  // Only the open's one GET — nothing re-asked on a timer.
  expect(hits).toHaveLength(1);
});

test("a settled panel subscribes to nothing; unmounting a waiting one closes its subscription", async () => {
  const bus = fakeEvents();
  await mountPanel(DONE);
  expect(bus.subs).toHaveLength(0);
  const waiting = await mountPanel(CHECKING);
  expect(bus.open()).toHaveLength(1);
  act(() => waiting.r.unmount());
  expect(bus.open()).toHaveLength(0);
});

test("dot, second dot and panel ask for ONE key — the client's refcount makes it one subscription", async () => {
  const bus = fakeEvents();
  mountDot(DIR);
  mountDot(DIR);
  await mountPanel(CHECKING);
  const keys = new Set(bus.open().map((s) => s.topic + " " + canonicalKey(s.params)));
  expect(bus.open()).toHaveLength(3);
  expect([...keys]).toEqual([`apps.doctor ${canonicalKey({ path: DIR })}`]);
});

test("no 4 s timer re-asks the report any more", () => {
  const hook = readFileSync(join(import.meta.dir, "useAppDoctorChecks.ts"), "utf8");
  const modal = readFileSync(join(import.meta.dir, "AppDoctorModal.tsx"), "utf8");
  for (const src of [hook, modal]) {
    expect(src).not.toMatch(/setInterval|CHECK_POLL_MS/);
    expect(src).toContain('subscribeTopic<AppDoctorReport>("apps.doctor", { path: dir }');
  }
  expect(hook).not.toContain("getAppDoctor");
  // The panel's remaining timer is the git row's single delayed re-ask.
  expect(modal.match(/setTimeout\(/g)?.length).toBe(1);
});
