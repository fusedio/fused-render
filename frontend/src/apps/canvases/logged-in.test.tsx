// The sign-in hub behind the sidebar's Canvases row: one `canvases.status`
// subscription shared by every reader, opened by the first and closed by the
// last; a page's own `publishLoggedIn` lands at once and asks the bus for a
// fresh snapshot; a remembered refusal (`deniedStamp`) outranks a cheerful
// snapshot for the same credentials store. Driven through a scripted events
// client (`setEventsClientForTests`) — the real one never calls back under
// bun, and `mock.module` is process-wide.
import { afterEach, expect, test } from "bun:test";
import { act, create } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";
import { setEventsClientForTests } from "@platform/lib/events";
import type { CanvasesStatus } from "./api";

installDomShim();
const { publishLoggedIn, useCanvasesLoggedIn } = await import("./logged-in");

type Frame = (snap: unknown, delta: unknown, meta: Record<string, unknown>) => void;

function status(over: Partial<CanvasesStatus> = {}): CanvasesStatus {
  return {
    cli_found: true,
    logged_in: false,
    creds_stamp: null,
    login_in_flight: false,
    workbench_base_url: "",
    ...over,
  } as CanvasesStatus;
}

function fakeBus(first: CanvasesStatus) {
  const subs = new Set<Frame>();
  const resyncs: string[] = [];
  setEventsClientForTests({
    subscribe: ((topic: string, _params: unknown, cb: Frame) => {
      if (topic !== "canvases.status") throw new Error(`unscripted topic: ${topic}`);
      subs.add(cb);
      cb(first, null, { gen: null });
      return () => {
        subs.delete(cb);
      };
    }) as never,
    resync: (topic: string) => {
      resyncs.push(topic);
      return true;
    },
  });
  return {
    subs,
    resyncs,
    push: (snap: CanvasesStatus) => {
      for (const cb of subs) cb(snap, null, { gen: null });
    },
    pushError: () => {
      for (const cb of subs) cb(null, null, { error: "down", status: 500 });
    },
  };
}

const last = (xs: boolean[]) => xs[xs.length - 1];

function Reader({ onValue }: { onValue: (v: boolean) => void }) {
  onValue(useCanvasesLoggedIn());
  return null;
}

afterEach(() => setEventsClientForTests(null));

test("two readers share one canvases.status subscription; it opens with the first and closes with the last", async () => {
  const bus = fakeBus(status({ logged_in: true, creds_stamp: 1 }));
  const seen: boolean[][] = [[], []];
  let a!: ReturnType<typeof create>;
  let b!: ReturnType<typeof create>;
  await act(async () => {
    a = create(<Reader onValue={(v) => seen[0].push(v)} />);
  });
  await act(async () => {
    b = create(<Reader onValue={(v) => seen[1].push(v)} />);
  });
  expect(bus.subs.size).toBe(1);
  expect(last(seen[0])).toBe(true);
  expect(last(seen[1])).toBe(true);

  // A later snapshot moves both readers; a refused frame moves neither.
  await act(async () => {
    bus.push(status({ logged_in: false }));
  });
  expect(last(seen[0])).toBe(false);
  expect(last(seen[1])).toBe(false);
  await act(async () => {
    bus.pushError();
  });
  expect(last(seen[0])).toBe(false);

  await act(async () => {
    a.unmount();
  });
  expect(bus.subs.size).toBe(1);
  await act(async () => {
    b.unmount();
  });
  expect(bus.subs.size).toBe(0);
});

test("publishLoggedIn lands at once, resyncs the lane, and a refused store's stamp outranks a later snapshot", async () => {
  const bus = fakeBus(status({ logged_in: true, creds_stamp: 7 }));
  const seen: boolean[] = [];
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(<Reader onValue={(v) => seen.push(v)} />);
  });
  expect(last(seen)).toBe(true);

  // The page's guarded call came back 401: it publishes the refusal.
  await act(async () => {
    publishLoggedIn(status({ logged_in: false, creds_stamp: 7 }));
  });
  expect(last(seen)).toBe(false);
  expect(bus.resyncs).toEqual(["canvases.status"]);

  // The bus cheerfully reports the SAME store as signed in — still refused.
  await act(async () => {
    bus.push(status({ logged_in: true, creds_stamp: 7 }));
  });
  expect(last(seen)).toBe(false);

  // A NEW stamp is what a completed re-login looks like.
  await act(async () => {
    bus.push(status({ logged_in: true, creds_stamp: 8 }));
  });
  expect(last(seen)).toBe(true);

  await act(async () => {
    r.unmount();
  });
  expect(bus.subs.size).toBe(0);
});
