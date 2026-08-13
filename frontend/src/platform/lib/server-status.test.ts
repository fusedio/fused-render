// The server-status decision table. One probe result at a time goes through
// reduceProbe; the returned state drives the banner and `reload` asks the
// component to location.reload(). The two update cases are distinct: a served
// version newer than the bundled one is fixed by a page refresh, while an
// installed-on-disk version newer than the served one needs an app restart —
// prompting "refresh" there would be a lie.
import { expect, test } from "bun:test";

import {
  FAIL_THRESHOLD,
  initialStatus,
  reduceProbe,
  type StatusState,
} from "@platform/lib/server-status";

const BUILD = "0.4.8";

const ok = (version = BUILD, installedVersion: string | null = null) => ({
  ok: true,
  version,
  installedVersion,
});
const fail = () => ({ ok: false });

function run(state: StatusState, probes: Array<ReturnType<typeof ok | typeof fail>>) {
  let reload = false;
  for (const probe of probes) ({ state, reload } = reduceProbe(state, probe, BUILD));
  return { state, reload };
}

test("healthy probe with matching versions stays hidden", () => {
  const { state, reload } = run(initialStatus(), [ok()]);
  expect(state.banner).toBe("hidden");
  expect(reload).toBe(false);
});

test("goes down only after consecutive failures reach the threshold", () => {
  let state = initialStatus();
  for (let i = 1; i < FAIL_THRESHOLD; i++) {
    ({ state } = reduceProbe(state, fail(), BUILD));
    expect(state.banner).toBe("hidden");
  }
  ({ state } = reduceProbe(state, fail(), BUILD));
  expect(state.banner).toBe("down");
});

test("a success between failures resets the streak", () => {
  const { state } = run(initialStatus(), [fail(), ok(), fail()]);
  expect(state.banner).toBe("hidden");
});

test("recovery on the same version shows reconnected, no reload", () => {
  const { state, reload } = run(initialStatus(), [fail(), fail(), ok()]);
  expect(state.banner).toBe("reconnected");
  expect(reload).toBe(false);
});

test("served version differs from bundle: refresh banner", () => {
  const { state, reload } = run(initialStatus(), [ok("0.4.9")]);
  expect(state.banner).toBe("update-refresh");
  expect(reload).toBe(false);
});

test("recovery onto a new version auto-reloads", () => {
  const { reload } = run(initialStatus(), [fail(), fail(), ok("0.4.9")]);
  expect(reload).toBe(true);
});

test("served version changing between healthy probes auto-reloads", () => {
  // A restart can be quicker than the down threshold (one missed poll, or
  // none if the tab was hidden) — the version transition itself is proof the
  // server swapped under this tab.
  const { reload } = run(initialStatus(), [ok(), ok("0.4.9")]);
  expect(reload).toBe(true);
});

test("a version transition never auto-reloads while the disk is still ahead", () => {
  const { state, reload } = run(initialStatus(), [ok(), ok("0.4.9", "0.5.0")]);
  expect(reload).toBe(false);
  expect(state.banner).toBe("update-restart");
});

test("reconnected clears on the following healthy probe", () => {
  // Backstop for the dismiss-timer race: an in-flight probe can write a stale
  // "reconnected" back AFTER the timer fired (and no new timer arms, wasDown
  // being false). The reducer therefore never holds "reconnected" past the
  // next probe — worst case the card shows for two poll ticks, never forever.
  const { state } = run(initialStatus(), [fail(), fail(), ok(), ok()]);
  expect(state.banner).toBe("hidden");
});

test("installed version differs from running server: restart banner", () => {
  const { state } = run(initialStatus(), [ok(BUILD, "0.4.9")]);
  expect(state.banner).toBe("update-restart");
});

test("restart wins over refresh when both versions drift", () => {
  // Disk has 0.5.0, running server 0.4.9, this bundle 0.4.8 — a refresh
  // still leaves a stale server, so ask for the restart.
  const { state } = run(initialStatus(), [ok("0.4.9", "0.5.0")]);
  expect(state.banner).toBe("update-restart");
});

test("no auto-reload on recovery while the disk is still ahead", () => {
  const { state, reload } = run(initialStatus(), [fail(), fail(), ok("0.4.9", "0.5.0")]);
  expect(reload).toBe(false);
  expect(state.banner).toBe("update-restart");
});

test("update banners survive later healthy probes", () => {
  const { state } = run(initialStatus(), [ok("0.4.9"), ok("0.4.9")]);
  expect(state.banner).toBe("update-refresh");
});

test("update banner clears if versions re-align", () => {
  const { state } = run(initialStatus(), [ok(BUILD, "0.4.9"), ok()]);
  expect(state.banner).toBe("hidden");
});

test("down interrupts an update banner once the threshold is hit", () => {
  const { state } = run(initialStatus(), [ok("0.4.9"), fail(), fail()]);
  expect(state.banner).toBe("down");
});

test("probe body without versions is treated as healthy, not an update", () => {
  const { state, reload } = run(initialStatus(), [{ ok: true }]);
  expect(state.banner).toBe("hidden");
  expect(reload).toBe(false);
});
