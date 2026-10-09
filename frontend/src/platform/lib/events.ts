// The shell's door onto the events bus (fused_render/static/events-client.js,
// one socket per document, every live fact a subscription). This is a thin
// typed wrapper plus the `useTopic` React hook; the client itself is the
// plain-JS module runtime.js shares (D12), installed on `window.fusedEvents`
// by the side-effect import below.
//
// NO TIMER IN THE SHELL FETCHES ANY MORE (D3): a component that wants a live
// fact subscribes here and renders the snapshot the server pushes; a query
// with arguments (a search, a rank) stays a GET and is re-asked when the topic
// that would change its answer moves (D11).
import { useEffect, useRef, useState } from "react";
import "@static/events-client.js";
import type { FusedEvents, FusedEventsCallback, FusedEventsMeta, FusedEventsSubscribeOptions } from "@static/events-client.js";

export type { FusedEventsCallback, FusedEventsMeta, FusedEventsSubscribeOptions };

/** `fusedEvents.subscribe`'s shape, typed for one topic — what a module takes
 *  as its seam so a bun test can hand over a scripted one. */
export type SubscribeLike<S = unknown, D = unknown> = (
  topic: string,
  params: Record<string, unknown> | null | undefined,
  cb: FusedEventsCallback<S, D>,
  opts?: FusedEventsSubscribeOptions,
) => () => void;

/** A bun test's stand-in for the client: `setEventsClientForTests` installs
 *  one whose `subscribe` pushes scripted frames, so a module under test
 *  needs no module mock (`mock.module` is process-wide in bun). */
let override: FusedEvents | null = null;

export function setEventsClientForTests(client: Partial<FusedEvents> | null): void {
  override = client as FusedEvents | null;
}

/** The client, or null where there is none. */
export function eventsClient(): FusedEvents | null {
  if (override) return override;
  const g = globalThis as unknown as { fusedEvents?: FusedEvents };
  return g.fusedEvents ?? null;
}

/**
 * Follow a topic. Returns the unsubscribe. Without a client (tests) this is a
 * no-op that never calls back — a suite that wants frames hands its own
 * subscribe in through the module under test's env, never through here.
 */
export function subscribeTopic<S = unknown, D = unknown>(
  topic: string,
  params: Record<string, unknown> | null | undefined,
  cb: FusedEventsCallback<S, D>,
  opts?: FusedEventsSubscribeOptions,
): () => void {
  const client = eventsClient();
  if (!client) return () => {};
  return client.subscribe<S, D>(topic, params, cb, opts);
}

/** Ask the bus for a fresh snapshot of a subscribed key now — an event, not a
 *  timer: a local write just happened that the producer may take a beat to
 *  notice (a POST this document made, a storage stamp from a sibling). */
export function resyncTopic(topic: string, params?: Record<string, unknown> | null): boolean {
  return eventsClient()?.resync(topic, params) ?? false;
}

export interface UseTopicOptions<S, D = unknown> {
  /** Override the server's hidden policy (D7). */
  hiddenOk?: boolean;
  /** `false` subscribes to nothing (a hook cannot be called conditionally). */
  enabled?: boolean;
  /** Fold a delta into the held snapshot; without it deltas are ignored and
   *  only snapshots update the value. */
  reduce?: (held: S | null, delta: D) => S | null;
  /** Hear every frame as it lands (side effects: a notification, a poke). */
  onFrame?: FusedEventsCallback<S, D>;
}

/**
 * The live body of a topic, as the server pushes it: `null` until the first
 * snapshot lands (or while `enabled` is false). Re-keyed on the canonical
 * params, so a changed scope is a new subscription and a fresh snapshot.
 */
export function useTopic<S = unknown, D = unknown>(
  topic: string,
  params: Record<string, unknown> | null | undefined,
  opts: UseTopicOptions<S, D> = {},
): S | null {
  const [value, setValue] = useState<S | null>(null);
  const key = canonicalKey(params);
  const optsRef = useRef(opts);
  optsRef.current = opts;
  const enabled = opts.enabled !== false;
  useEffect(() => {
    if (!enabled) {
      setValue(null);
      return;
    }
    let held: S | null = null;
    const off = subscribeTopic<S, D>(
      topic,
      params,
      (snap, delta, meta) => {
        optsRef.current.onFrame?.(snap, delta, meta);
        if (snap !== null) {
          held = snap;
          setValue(snap);
        } else if (delta !== null && optsRef.current.reduce) {
          held = optsRef.current.reduce(held, delta);
          setValue(held);
        }
      },
      optsRef.current.hiddenOk === undefined ? undefined : { hiddenOk: optsRef.current.hiddenOk },
    );
    return off;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topic, key, enabled]);
  return value;
}

/** One string per distinct parameter set, whatever the key order — the same
 *  rule the client refcounts by. */
export function canonicalKey(params: Record<string, unknown> | null | undefined): string {
  if (!params) return "{}";
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(params).sort()) if (params[k] !== undefined) out[k] = params[k];
  return JSON.stringify(out);
}
