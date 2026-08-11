// What this machine is holding in memory, shared by the AI Models page and the
// sidebar entry's dot (SPEC §40).
//
// Two readers, one poll. The sidebar needs a single bit — is anything loaded —
// and the page needs the whole table; polling twice would be two requests a
// second for one in-memory answer, and worse, they would disagree for a beat
// after a load or an unload. So the poll lives here and both subscribe, the same
// publish/subscribe shape `useAiModelsAvailable` already uses for the cache gate.
//
// The cadence follows the state, not the clock: while something is loading or
// downloading the numbers move every second, and while nothing is happening
// there is nothing to see. An idle machine costs one cheap in-memory read every
// 10 seconds.
import { useEffect, useState } from "react";
import { getAiRuntime, type AiRuntime } from "@platform/lib/api";

const ACTIVE_MS = 1000;
const IDLE_MS = 10_000;

const EMPTY: AiRuntime = { runners: [], loaded: [], totalResidentBytes: null };

let current: AiRuntime = EMPTY;
let timer: number | null = null;
let inFlight = false;
const listeners = new Set<(runtime: AiRuntime) => void>();

/** Anything mid-flight — a venv build, a download, weights going into memory. */
export function isBusy(runtime: AiRuntime): boolean {
  return runtime.loaded.some((m) => m.state !== "ready" && m.state !== "error");
}

function publish(next: AiRuntime) {
  current = next;
  for (const listener of listeners) listener(next);
}

async function poll() {
  if (inFlight) return;
  inFlight = true;
  try {
    publish(await getAiRuntime());
  } catch {
    // A failed read is not news: the page keeps the last answer it had rather
    // than blanking a table because one poll lost a race with a restart.
  } finally {
    inFlight = false;
    schedule();
  }
}

function schedule() {
  if (timer !== null) window.clearTimeout(timer);
  if (listeners.size === 0) {
    timer = null;
    return;
  }
  timer = window.setTimeout(poll, isBusy(current) ? ACTIVE_MS : IDLE_MS);
}

/** Subscribe to the runtime. Polling starts with the first reader and stops
 *  with the last — nothing polls a machine whose AI page nobody is looking at. */
export function useAiRuntime(): AiRuntime {
  const [runtime, setRuntime] = useState<AiRuntime>(current);
  useEffect(() => {
    listeners.add(setRuntime);
    // Read immediately rather than waiting out an interval: a page that has
    // just mounted should not show "nothing loaded" for a second first.
    void poll();
    return () => {
      listeners.delete(setRuntime);
      schedule();
    };
  }, []);
  return runtime;
}

/** Push a known-fresh answer — what a load or unload replies with — so the UI
 *  updates on the action rather than on the next tick. */
export function publishAiRuntime(runtime: AiRuntime) {
  publish(runtime);
  schedule();
}

/** Ask for a read now: after starting a load, when waiting a full interval to
 *  see the row appear would read as the button having done nothing. */
export function refreshAiRuntime() {
  void poll();
}
