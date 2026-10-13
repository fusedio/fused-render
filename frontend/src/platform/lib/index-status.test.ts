// useIndexStatus over the events bus: one `index.status` subscription per
// reader while `active`, the snapshot as the hook's value, a nonce bump as a
// resync (never a timer), and the completion stamp observed once for the
// lifecycle. The client is a scripted one (`setEventsClientForTests`): the
// real client in bun has no socket and never calls back.
import { afterEach, describe, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { setEventsClientForTests } = await import("@platform/lib/events");
const { useIndexStatus, INDEX_STATUS_TOPIC } = await import("@platform/lib/index-status");
type IndexStatus = import("@platform/lib/api").IndexStatus;

const status = (over: Partial<IndexStatus> = {}): IndexStatus =>
  ({
    scanning: false,
    has_index: true,
    files_indexed: 10,
    last_completed_at: 1,
    running: false,
    ...over,
  }) as IndexStatus;

/** The scripted bus: every subscribe is recorded, and `push` fans a snapshot
 *  out to every open subscription the way one server frame does. */
function rig() {
  const cbs = new Set<(s: unknown, d: unknown, m: Record<string, unknown>) => void>();
  const topics: string[] = [];
  let resyncs = 0;
  setEventsClientForTests({
    subscribe: ((
      topic: string,
      _params: unknown,
      cb: (s: unknown, d: unknown, m: Record<string, unknown>) => void,
    ) => {
      topics.push(topic);
      cbs.add(cb);
      return () => {
        cbs.delete(cb);
      };
    }) as never,
    resync: () => {
      resyncs += 1;
      return true;
    },
  });
  return {
    topics,
    open: () => cbs.size,
    resyncs: () => resyncs,
    push: (s: IndexStatus | null, meta: Record<string, unknown> = { gen: null }) => {
      for (const cb of [...cbs]) cb(s, null, meta);
    },
  };
}

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  setEventsClientForTests(null);
});

function mountReaders(n: number, active = true, nonce = 0) {
  const seen: (IndexStatus | null)[] = new Array(n).fill(null);
  const Probe = (p: { i: number; active: boolean; nonce: number }) => {
    seen[p.i] = useIndexStatus(p.active, p.nonce);
    return null;
  };
  const trees: ReactTestRenderer[] = [];
  act(() => {
    for (let i = 0; i < n; i++) trees.push(create(createElement(Probe, { i, active, nonce })));
  });
  mounted.push(...trees);
  return {
    seen,
    update: (i: number, p: { active?: boolean; nonce?: number }) =>
      act(() => {
        trees[i].update(createElement(Probe, { i, active: p.active ?? active, nonce: p.nonce ?? nonce }));
      }),
  };
}

describe("useIndexStatus", () => {
  test("null until the first snapshot, then the snapshot; an err frame is skipped", () => {
    const bus = rig();
    const r = mountReaders(1);
    expect(bus.topics).toEqual([INDEX_STATUS_TOPIC]);
    expect(r.seen[0]).toBeNull();
    act(() => bus.push(status({ scanning: true })));
    expect(r.seen[0]?.scanning).toBe(true);
    act(() => bus.push(null, { error: "boom", status: 500 }));
    expect(r.seen[0]?.scanning).toBe(true);
    act(() => bus.push(status({ scanning: false })));
    expect(r.seen[0]?.scanning).toBe(false);
  });

  test("inactive subscribes to nothing; going active opens it, inactive closes it", () => {
    const bus = rig();
    const r = mountReaders(1, false);
    expect(bus.open()).toBe(0);
    r.update(0, { active: true });
    expect(bus.open()).toBe(1);
    r.update(0, { active: false });
    expect(bus.open()).toBe(0);
  });

  test("N readers are N subscribers of the one topic, and one frame reaches them all", () => {
    // The refcount to ONE server subscription is the client's job; the hook's
    // contract is that it never opens a second topic or a second key.
    const bus = rig();
    const r = mountReaders(3);
    expect(bus.topics).toEqual([INDEX_STATUS_TOPIC, INDEX_STATUS_TOPIC, INDEX_STATUS_TOPIC]);
    act(() => bus.push(status({ files_indexed: 42 })));
    expect(r.seen.map((s) => s?.files_indexed)).toEqual([42, 42, 42]);
  });

  test("a nonce bump is one resync of the subscription, never a timer or a GET", () => {
    const bus = rig();
    const r = mountReaders(1, true, 0);
    expect(bus.resyncs()).toBe(0);
    r.update(0, { nonce: 1 });
    expect(bus.resyncs()).toBe(1);
    r.update(0, { nonce: 1 });
    expect(bus.resyncs()).toBe(1);
    // Mounting with a nonce already set is not a bump.
    const bus2 = rig();
    mountReaders(1, true, 7);
    expect(bus2.resyncs()).toBe(0);
  });
});
