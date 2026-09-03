// D664's retire-toast diffing — `retiredEngines`, exported from
// ActivityDock.tsx purely so this suite can exercise it directly (C9: D664
// shipped on this branch with no test at all, and C5 is exactly the defect
// that gap let through). No render, no poll, no `window`/`document`: this
// is the same pure-function-with-a-test split `jobs.ts`/`repo-updates-lib.ts`
// use for the parts of a dock that are wrong in ways a screenshot won't show.
import { expect, test } from "bun:test";

import { retiredEngines } from "@shell/ActivityDock";
import type { RunningEngine } from "@platform/lib/api";

function engine(over: Partial<RunningEngine> = {}): RunningEngine {
  return {
    engine_id: "e1",
    pid: 1,
    version: "1.0.0",
    folder: "/apps/thing",
    module: "",
    uptime_s: 120,
    idle_timeout_s: 900,
    idle_for_s: 0,
    busy: false,
    ...over,
  };
}

test("an engine missing from the next snapshot, with no stopping marker, is a genuine retirement", () => {
  const e = engine();
  const stopping = new Map<string, number>();
  const retired = retiredEngines([e], [], stopping, 1_000_000);
  expect(retired).toEqual([e]);
});

test("an engine still present in the next snapshot never retires, marker or not", () => {
  const e = engine();
  const stopping = new Map<string, number>([[e.engine_id, 999_000]]);
  const retired = retiredEngines([e], [e], stopping, 1_000_000);
  expect(retired).toEqual([]);
  // The marker is untouched — the engine never disappeared, so there is
  // nothing to consume it yet.
  expect(stopping.has(e.engine_id)).toBe(true);
});

test("a fresh stopping marker suppresses the toast for the engine it names", () => {
  const e = engine();
  const stopping = new Map<string, number>([[e.engine_id, 1_000_000]]);
  // Well within STOPPING_GRACE_MS (30s) of the marker.
  const retired = retiredEngines([e], [], stopping, 1_005_000);
  expect(retired).toEqual([]);
  // Consumed on the tick that checked it, whether or not it suppressed
  // anything — a marker is spent the moment its window is evaluated.
  expect(stopping.has(e.engine_id)).toBe(false);
});

test("C5: a stopping marker past its grace window no longer swallows a later, genuine retirement", () => {
  // `stopEngine()` rejected, or the engine is a `main =` app `restart()`
  // revived — either way nothing ever consumed the marker at the time, and
  // it sat in the map. Before the fix this permanently ate the id's next
  // real idle-retirement, however much later that happened. The grace
  // window bounds how long a click can plausibly still be resolving for.
  const e = engine();
  const stopping = new Map<string, number>([[e.engine_id, 0]]);
  // Long past STOPPING_GRACE_MS (30s) since the marker was set.
  const retired = retiredEngines([e], [], stopping, 60_000);
  expect(retired).toEqual([e]);
  expect(stopping.has(e.engine_id)).toBe(false);
});

test("only the engines actually missing are reported — a mixed snapshot", () => {
  const stays = engine({ engine_id: "stays" });
  const goesQuiet = engine({ engine_id: "goes-quiet" });
  const userStopped = engine({ engine_id: "user-stopped" });
  const stopping = new Map<string, number>([["user-stopped", 1_000_000]]);
  const retired = retiredEngines([stays, goesQuiet, userStopped], [stays], stopping, 1_001_000);
  expect(retired).toEqual([goesQuiet]);
});
