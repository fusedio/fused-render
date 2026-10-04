// The store around the stage machine: the ONE press handler, and the
// cross-window latch that makes every open window tell the same story (D3).
// `restart-flow.test.ts` owns the transitions; what is tested here is the
// wiring the reducer cannot see — the order a press does things in, that a
// latched press is another window's instant rather than this one's, and that
// no surface anywhere still ships a relaunch link of its own.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const {
  RELAUNCH_HREF,
  noteRestartProbe,
  receiveRestartBroadcastForTests,
  requestRestart,
  resetRestartForTests,
} = await import("@platform/lib/restart-store");
const { RESTART_GIVE_UP_MS } = await import("@platform/lib/restart-flow");

const nav: string[] = [];
const loc = globalThis.location as unknown as { assign: (href: string) => void };
loc.assign = (href: string) => {
  nav.push(href);
};

afterEach(() => {
  nav.length = 0;
  resetRestartForTests();
});

// The store's state is read here the way React reads it, through the public
// hook's own snapshot getter — imported lazily so the module under test is the
// same instance the helpers above hold.
const { useRestartFlow } = await import("@platform/lib/restart-store");
const React = await import("react");
const { act, create } = await import("react-test-renderer");

let seen: { stage: string; requestedAt: number | null } = { stage: "ready", requestedAt: null };
function Probe() {
  const state = useRestartFlow();
  seen = { stage: state.stage, requestedAt: state.requestedAt };
  return null;
}
async function watch() {
  let r!: ReturnType<typeof create>;
  await act(async () => {
    r = create(React.createElement(Probe));
  });
  return r;
}

test("the press latches BEFORE it navigates", async () => {
  const r = await watch();
  noteRestartProbe({ ok: true, version: "0.5.50" });
  await act(async () => {
    requestRestart();
  });
  // `location.assign` hands the main thread to the OS; anything left after it
  // is a race, so the latch has to already have happened by the time the
  // navigation is recorded.
  expect(seen.stage).toBe("quitting");
  expect(nav).toEqual([RELAUNCH_HREF]);
  expect(typeof seen.requestedAt).toBe("number");
  act(() => r.unmount());
});

test("the deep link is the bare relaunch, not the FDA one", () => {
  // `fda.ts`'s RELAUNCH_HREF carries `?reason=fda` and respawns the SAME
  // version to pick up a Full Disk Access grant — a different action with a
  // different story on screen (fused_render/deeplink.py tells them apart).
  expect(RELAUNCH_HREF).toBe("fused-render://relaunch");
});

test("a second window latches the broadcast and runs the same clock", async () => {
  const r = await watch();
  // This window never pressed anything: the press arrives over the channel,
  // carrying the OTHER window's instant.
  const at = Date.now() - 30_000;
  await act(async () => {
    receiveRestartBroadcastForTests({ at, served: "0.5.50" });
  });
  expect(seen.stage).toBe("quitting");
  expect(seen.requestedAt).toBe(at);
  // …and its own probes drive its own stages from there.
  await act(async () => {
    noteRestartProbe({ ok: false });
  });
  expect(seen.stage).toBe("restarting");
  await act(async () => {
    noteRestartProbe({ ok: false });
  });
  expect(seen.stage).toBe("reconnecting");
  await act(async () => {
    noteRestartProbe({ ok: true, version: "0.5.51" });
  });
  expect(seen.stage).toBe("back");
  act(() => r.unmount());
});

test("a latched press expires on the ORIGINAL instant, not on this window's", async () => {
  const r = await watch();
  await act(async () => {
    receiveRestartBroadcastForTests({ at: Date.now() - (RESTART_GIVE_UP_MS + 5_000), served: "0.5.50" });
  });
  // Already past the cap when it arrived: one probe is enough to say so, and
  // the page falls through to the ordinary down card.
  await act(async () => {
    noteRestartProbe({ ok: false });
  });
  expect(seen.stage).toBe("gave-up");
  act(() => r.unmount());
});

test("the same broadcast twice does not restart the clock", async () => {
  const r = await watch();
  const at = Date.now() - 10_000;
  await act(async () => {
    receiveRestartBroadcastForTests({ at, served: "0.5.50" });
    noteRestartProbe({ ok: false });
    // Channel and storage both heard, or a re-post: latching is idempotent on
    // the instant, so the cap is not pushed out by an echo.
    receiveRestartBroadcastForTests({ at, served: "0.5.50" });
  });
  expect(seen.requestedAt).toBe(at);
  expect(seen.stage).toBe("restarting");
  act(() => r.unmount());
});

test("a malformed broadcast is ignored", async () => {
  const r = await watch();
  await act(async () => {
    receiveRestartBroadcastForTests(null);
    receiveRestartBroadcastForTests({ at: "soon" });
    receiveRestartBroadcastForTests({});
    receiveRestartBroadcastForTests("restart");
  });
  expect(seen.stage).toBe("ready");
  act(() => r.unmount());
});

test("the press remembers the version the last healthy probe reported", async () => {
  const r = await watch();
  noteRestartProbe({ ok: true, version: "0.5.50" });
  await act(async () => {
    requestRestart();
  });
  // Proof it recorded the right `before`: a probe answering on that SAME
  // version is not a restart, so the stage must not jump to "back".
  await act(async () => {
    noteRestartProbe({ ok: true, version: "0.5.50" });
  });
  expect(seen.stage).toBe("quitting");
  await act(async () => {
    noteRestartProbe({ ok: true, version: "0.5.51" });
  });
  expect(seen.stage).toBe("back");
  act(() => r.unmount());
});

// ---- ONE ENTRY POINT ------------------------------------------------------
//
// A grep, not a render assertion, because the failure mode is a new surface
// growing its own `<a href="fused-render://relaunch">` next month — which is a
// restart nothing remembers, and therefore a dialog that never lights up.

/** Line and block comments out: a note EXPLAINING that the link used to live
 *  here is exactly what should survive. */
function code(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

test("no surface but the store ships a relaunch link of its own", () => {
  const root = join(import.meta.dir, "..", "..");
  const allowed = new Set([
    // The one handler.
    join(root, "platform", "lib", "restart-store.ts"),
    // A DIFFERENT action: `?reason=fda` respawns the same version for a Full
    // Disk Access grant and has nothing to do with an update.
    join(root, "platform", "lib", "fda.ts"),
  ]);
  const bad: string[] = [];
  for (const file of walk(root)) {
    if (allowed.has(file)) continue;
    code(readFileSync(file, "utf8"))
      .split("\n")
      .forEach((line, i) => {
        if (line.includes("fused-render://relaunch")) {
          bad.push(file.slice(root.length + 1) + ":" + (i + 1) + " " + line.trim());
        }
      });
  }
  expect(bad).toEqual([]);
});
