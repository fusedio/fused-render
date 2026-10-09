// What this machine is holding in memory, shared by the AI Models page and the
// sidebar entry's dot (SPEC §40).
//
// Two readers, one subscription. The sidebar needs a single bit — is anything
// loaded — and the page needs the whole table; asking twice would be two
// subscriptions for one in-memory answer, and worse, they would disagree for a
// beat after a load or an unload. So the subscription lives here and both
// read it: one module-level record, a listener set, and a `publish` that
// writes both — which is also what lets a load STARTED on the page reach a
// sidebar that never asked for it.
//
// SINCE 2026-10-09 NOTHING HERE POLLS (D3). The record arrives over the
// document's events-bus socket (`ai.runtime`, platform/lib/events): the server
// pushes a snapshot — GET /api/ai/runtime's body, exactly — on subscribe and
// whenever the runtime moves, every second while something is loading or
// downloading (the old active cadence) and otherwise only on change. The 1 s /
// 10 s timer, its in-flight guard and the hidden-window gate are gone: a
// hidden document's subscription is dropped by the client and resubscribed on
// return, and the snapshot that answers is the catch-up.
import { useEffect, useState } from "react";
import type { AiRuntime } from "@platform/lib/api";
import { resyncTopic, subscribeTopic } from "@platform/lib/events";

const TOPIC = "ai.runtime";

const EMPTY: AiRuntime = {
  runners: [],
  loaded: [],
  downloading: [],
  totalResidentBytes: null,
  // Null, not a guess: `settled` (below) is what distinguishes
  // "not asked yet" from "this machine has no readable ceiling".
  memoryCeilingBytes: null,
};

let current: AiRuntime = EMPTY;
// Has a real response ever landed? `EMPTY` is indistinguishable from a machine
// holding nothing, and `useAutoExpandOnNew` needs that distinction so a page
// load onto already-resident models is not read as a wave of arrivals (D574
// bug 2, autoExpand.ts's `ready`). Module-level like `current` itself, so a
// remount inherits it rather than re-announcing what is already loaded.
let settled = false;
/** This module's one subscription to `ai.runtime`, or null while nobody reads
 *  the runtime. */
let lane: (() => void) | null = null;
const listeners = new Set<(runtime: AiRuntime) => void>();

/** Anything mid-flight — a venv build, a download, weights going into memory.
 *
 *  `downloading` is not an afterthought in this predicate: a weights-only pull
 *  holds no memory and appears in no worker row, so a runtime that only looked
 *  at `loaded` called an 8GB download an idle machine — which, for the readers
 *  that gate their own work on it (useCacheScan, the playground), would leave
 *  the page's job rows unread. */
export function isBusy(runtime: AiRuntime): boolean {
  return (
    runtime.downloading.length > 0 ||
    runtime.loaded.some((m) => m.state !== "ready" && m.state !== "error")
  );
}

function publish(next: AiRuntime) {
  current = next;
  settled = true;
  for (const listener of listeners) listener(next);
}

// One frame from the bus. A refusal (`meta.error`) is not news: the page keeps
// the last answer it had rather than blanking a table because one read lost a
// race with a restart.
function onFrame(snap: AiRuntime | null) {
  if (snap !== null) publish(snap);
}

/** Keep the lane in step with the readers: open while anyone reads the
 *  runtime, closed when nobody does — nothing watches a machine whose AI page
 *  nobody is looking at. */
function syncLane() {
  const wanted = listeners.size > 0;
  if (!wanted) {
    if (lane) {
      const stop = lane;
      lane = null;
      stop();
    }
    return;
  }
  if (lane) return;
  lane = subscribeTopic<AiRuntime>(TOPIC, {}, onFrame);
}

/** Subscribe to the runtime. The subscription opens with the first reader and
 *  closes with the last. A page that has just mounted does not show "nothing
 *  loaded" for a second first: the client replays the cached snapshot
 *  synchronously to a late reader, and a first reader's subscribe is answered
 *  with a snapshot before anything else. */
export function useAiRuntime(): AiRuntime {
  const [runtime, setRuntime] = useState<AiRuntime>(current);
  useEffect(() => {
    listeners.add(setRuntime);
    syncLane();
    // The lane may have opened before this reader (another reader's mount):
    // the record it already holds is the one to show.
    setRuntime(current);
    return () => {
      listeners.delete(setRuntime);
      syncLane();
    };
  }, []);
  return runtime;
}

/** Whether `useAiRuntime` has ever seen a real answer — see `settled`. Read
 *  during render: whatever makes it flip also publishes new state, so the
 *  re-render that observes the data observes this with it. */
export function aiRuntimeSettled(): boolean {
  return settled;
}

/** Push a known-fresh answer — what a load or unload replies with — so the UI
 *  updates on the action rather than on the server's next push. */
export function publishAiRuntime(runtime: AiRuntime) {
  publish(runtime);
}

/** Ask for a fresh snapshot now: after starting a load, when waiting for the
 *  server to notice would read as the button having done nothing. An event,
 *  never a timer (D3). */
export function refreshAiRuntime() {
  resyncTopic(TOPIC, {});
}
