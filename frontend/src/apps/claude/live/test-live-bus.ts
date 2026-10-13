// TEST-ONLY: `claude.live` as the standing-watch suite scripts it.
//
// `createLiveWatch` subscribes to `claude.live` and laps on every frame; the
// suite was written against a watch that asked `deps.liveness` itself on a
// timer. This adapter keeps the scripts meaning what they meant: every frame
// it pushes is built by calling the suite's `answer` ONCE — the rig's own
// `liveness` (so `livenessCalls` still counts the stats a lap read) and its
// idea of the live run — on subscribe (the snapshot the bus always sends
// first) and on each `resync` of an open key (the event triggers' ask).
// Nothing in production imports this file.
import { canonicalKey, type SubscribeLike } from "@platform/lib/events";
import type { ClaudeLiveBody } from "../protocol/run-controller";

export interface LiveSub {
  topic: string;
  params: Record<string, unknown>;
  closed: boolean;
  push(): Promise<void>;
}

export function liveBus(answer: (params: Record<string, unknown>) => ClaudeLiveBody | Promise<ClaudeLiveBody>) {
  const subs: LiveSub[] = [];
  const resyncs: Record<string, unknown>[] = [];
  const subscribe = ((topic, params, cb) => {
    const p = { ...(params || {}) } as Record<string, unknown>;
    const sub: LiveSub = {
      topic,
      params: p,
      closed: false,
      async push() {
        const body = await answer(p);
        if (!sub.closed) (cb as (s: unknown, d: unknown, m: { gen: null }) => void)(body, null, { gen: null });
      },
    };
    subs.push(sub);
    void sub.push();
    return () => {
      sub.closed = true;
    };
  }) as SubscribeLike;
  const resync = (topic: string, params: Record<string, unknown>) => {
    resyncs.push(params);
    const key = canonicalKey(params);
    for (const s of subs) if (!s.closed && s.topic === topic && canonicalKey(s.params) === key) void s.push();
  };
  return { subscribe, resync, subs, resyncs, open: () => subs.filter((s) => !s.closed) };
}
