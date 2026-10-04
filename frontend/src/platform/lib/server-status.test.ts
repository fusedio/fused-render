// The server-status decision table. One probe result at a time goes through
// reduceProbe; the returned state drives the banner and `reload` asks the
// component to location.reload(). The two update cases are distinct: a served
// version newer than the bundled one is fixed by a page refresh, while an
// installed-on-disk version newer than the served one needs an app restart —
// prompting "refresh" there would be a lie.
import { expect, test } from "bun:test";

import {
  bannerSurface,
  FAIL_THRESHOLD,
  initialStatus,
  probeOnTick,
  probeWhileHidden,
  reduceProbe,
  updateDialogMode,
  updateDialogPreview,
  pendingOutage,
  type ProbeFailKind,
  type ProbeResult,
  type StatusState,
  type SurfaceInput,
} from "@platform/lib/server-status";
import { restartInFlight, RESTART_STAGES } from "@platform/lib/restart-flow";

const BUILD = "0.4.8";

const ok = (version = BUILD, installedVersion: string | null = null): ProbeResult => ({
  ok: true,
  version,
  installedVersion,
});
const fail = (kind: ProbeFailKind = "refused"): ProbeResult => ({ ok: false, kind });
/** Enough consecutive failures to reach "down". */
const down = (kind: ProbeFailKind = "refused") =>
  Array.from({ length: FAIL_THRESHOLD }, () => fail(kind));

function run(state: StatusState, probes: ProbeResult[]) {
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
  const { state, reload } = run(initialStatus(), [...down(), ok()]);
  expect(state.banner).toBe("reconnected");
  expect(reload).toBe(false);
});

test("served version differs from bundle: refresh banner", () => {
  const { state, reload } = run(initialStatus(), [ok("0.4.9")]);
  expect(state.banner).toBe("update-refresh");
  expect(reload).toBe(false);
});

test("recovery onto a new version auto-reloads", () => {
  const { reload } = run(initialStatus(), [...down(), ok("0.4.9")]);
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
  const { state } = run(initialStatus(), [...down(), ok(), ok()]);
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
  const { state, reload } = run(initialStatus(), [...down(), ok("0.4.9", "0.5.0")]);
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
  const { state } = run(initialStatus(), [ok("0.4.9"), ...down()]);
  expect(state.banner).toBe("down");
});

test("probe body without versions is treated as healthy, not an update", () => {
  const { state, reload } = run(initialStatus(), [{ ok: true }]);
  expect(state.banner).toBe("hidden");
  expect(reload).toBe(false);
});

// ---- failure kinds, the slow line and the outage record (SPEC §50) --------

test("a timeout below the threshold is slow, not down", () => {
  const { state } = run(initialStatus(), [fail("timeout")]);
  expect(state.banner).toBe("slow");
  expect(state.failKinds).toEqual(["timeout"]);
});

test("a refused probe below the threshold leaves the banner alone", () => {
  const { state } = run(initialStatus(), [fail("refused")]);
  expect(state.banner).toBe("hidden");
});

test("slow becomes down at the threshold, and clears quietly on recovery", () => {
  let { state } = run(initialStatus(), down("timeout"));
  expect(state.banner).toBe("down");
  ({ state } = run(initialStatus(), [fail("timeout"), ok()]));
  expect(state.banner).toBe("hidden");
});

test("recovery emits one outage record with the streak's kinds and boot ids", () => {
  let state = initialStatus();
  ({ state } = reduceProbe(state, { ...ok(), bootId: "a", latencyMs: 12 }, BUILD, 1_000));
  ({ state } = reduceProbe(state, fail("timeout"), BUILD, 6_000));
  ({ state } = reduceProbe(state, fail("refused"), BUILD, 11_000));
  const r = reduceProbe(state, { ...ok(), bootId: "b", latencyMs: 30 }, BUILD, 16_000);
  expect(r.outage).toEqual({
    t_down: 6,
    t_up: 16,
    strikes: 2,
    kinds: ["timeout", "refused"],
    boot_id_before: "a",
    boot_id_after: "b",
    recovered: true,
  });
  expect(r.state.fails).toBe(0);
  expect(r.state.failKinds).toEqual([]);
  expect(r.state.firstFailAt).toBeUndefined();
  expect(r.state.bootId).toBe("b");
  expect(r.state.latencies).toEqual([12, 30]);
});

test("a healthy streak emits no outage record", () => {
  let state = initialStatus();
  ({ state } = reduceProbe(state, { ...ok(), bootId: "a" }, BUILD, 1_000));
  const r = reduceProbe(state, { ...ok(), bootId: "a" }, BUILD, 6_000);
  expect(r.outage).toBeUndefined();
  expect(r.state.banner).toBe("hidden");
});

test("a same-version restart no probe saw fail still reconnects and records", () => {
  let state = initialStatus();
  ({ state } = reduceProbe(state, { ...ok(), bootId: "a" }, BUILD, 1_000));
  const r = reduceProbe(state, { ...ok(), bootId: "b" }, BUILD, 6_000);
  expect(r.state.banner).toBe("reconnected");
  expect(r.reload).toBe(false);
  expect(r.outage).toMatchObject({ t_down: 1, t_up: 6, strikes: 0, kinds: [], recovered: true });
});

test("the latency ring keeps only the newest entries", () => {
  let state = initialStatus();
  for (let i = 0; i < 60; i++) ({ state } = reduceProbe(state, { ...ok(), latencyMs: i }, BUILD));
  expect(state.latencies.length).toBe(50);
  expect(state.latencies[49]).toBe(59);
});

test("a tab leaving mid-outage has a partial, unrecovered record", () => {
  expect(pendingOutage(initialStatus())).toBeNull();
  let state = initialStatus();
  ({ state } = reduceProbe(state, { ...ok(), bootId: "a" }, BUILD, 1_000));
  ({ state } = reduceProbe(state, fail("http-5xx"), BUILD, 2_000));
  expect(pendingOutage(state)).toEqual({
    t_down: 2,
    t_up: null,
    strikes: 1,
    kinds: ["http-5xx"],
    boot_id_before: "a",
    boot_id_after: null,
    recovered: false,
  });
});

// ---- the refresh case's dialog, and its three modes -----------------------
// The state machine is unchanged by dev (a stale bundle IS a stale bundle); it
// is the PROMPT that is wrong on a dev.sh server, where the served version is
// the checkout's while the bundle in the tab was rebuilt by the vite watch
// seconds ago. So the suppression lives in the render, and here — as does the
// preview that puts the dialog back with no mismatch to reveal.

test("a packaged server always gets the real dialog", () => {
  expect(updateDialogMode(false, "", null)).toBe("real");
  // …including with the flag set: prod is never suppressed, so there is
  // nothing for it to lift and nothing to preview.
  expect(updateDialogMode(false, "?update_modal=1", "1")).toBe("real");
  expect(updateDialogMode(false, "?foo=1", null)).toBe("real");
});

test("a dev server gets no dialog", () => {
  expect(updateDialogMode(true, "", null)).toBe("off");
  expect(updateDialogMode(true, "?update_modal=0", "0")).toBe("off");
});

test("the dev flag previews it, from the URL or from storage", () => {
  // PREVIEW, not merely un-suppressed: a dev server and the bundle it just
  // built agree on the version, so lifting the suppression alone left the
  // developer looking at nothing (Akshil, 2026-09-14).
  expect(updateDialogMode(true, "?update_modal=1", null)).toBe("preview");
  expect(updateDialogMode(true, "?tab=list&update_modal=1", null)).toBe("preview");
  expect(updateDialogMode(true, "", "1")).toBe("preview");
});

test("a dev probe still reduces to the refresh state", () => {
  // Suppressing the dialog must not rewrite what the banner KNOWS — an
  // auto-reload on the next transition still depends on `served` being right.
  const { state } = reduceProbe(
    initialStatus(),
    { ok: true, version: "0.4.9", dev: true },
    BUILD
  );
  expect(state.banner).toBe("update-refresh");
  expect(state.served).toBe("0.4.9");
});

test("a dev probe with matching versions stays hidden — what preview renders over", () => {
  // The ordinary dev case, and the reason the preview cannot be gated on the
  // banner: there is no mismatch here at all, so the state machine has nothing
  // to say and the component has to force the dialog up itself.
  const { state } = reduceProbe(initialStatus(), { ok: true, version: BUILD, dev: true }, BUILD);
  expect(state.banner).toBe("hidden");
  expect(updateDialogMode(true, "?update_modal=1", null)).toBe("preview");
});

// ---- what the banner PUTS ON SCREEN ---------------------------------------
// `bannerSurface` is the second decision: given what the probe meant, the dev
// suppression, the shared update store and whether a restart is in flight, what
// does the reader actually see. It exists as a pure function because two
// surfaces have to agree — a "down" card under a dialog promising the app is
// coming back is the contradiction this whole flow was built to remove.

const surface = (over: Partial<SurfaceInput> = {}) =>
  bannerSurface({ banner: "hidden", mode: "real", stage: "ready", ...over });

test("nothing to say draws nothing", () => {
  expect(surface()).toBe("none");
});

test("today's cards are unchanged when no restart is anywhere near", () => {
  expect(surface({ banner: "down" })).toBe("down");
  expect(surface({ banner: "reconnected" })).toBe("reconnected");
  expect(surface({ banner: "update-refresh" })).toBe("refresh-dialog");
  expect(surface({ banner: "update-refresh", mode: "off" })).toBe("none");
});

test("slow draws its own line, and a restart in flight suppresses it like down", () => {
  expect(surface({ banner: "slow" })).toBe("slow");
  for (const stage of RESTART_STAGES.filter(restartInFlight)) {
    expect(surface({ banner: "slow", stage })).toBe("none");
  }
});

test("the disk being ahead suppresses the down card, but draws no dialog", () => {
  // Used to raise a blocking dialog (D1, then `UpdateDialog`'s "restart"
  // mode). Both are gone: the decision is a status-bar notification now
  // (`UpdateNotifier`), which this function knows nothing about — it only
  // still owns whether the down card shows.
  expect(surface({ banner: "update-restart" })).toBe("none");
});

test("the install landing draws no dialog either, same as the disk-ahead case", () => {
  // None of these ever draw anything HERE (`UpdateDialog`'s "restart" mode is
  // deleted) — with the default `banner: "hidden"` from `surface()` there is
  // nothing to suppress in the first place, so this only pins that no state
  // in the update store makes this function invent a card on its own. The
  // case that actually exercises suppression — `updateState` overriding a
  // real `banner: "down"` — is below, since it used to (wrongly) matter and
  // no longer does; see the "genuine outage" test.
  expect(surface({ updateState: "installed" })).toBe("none");
  expect(surface({ updateState: "installing" })).toBe("none");
  expect(surface({ updateState: "available" })).toBe("none");
  expect(surface({ updateState: "error" })).toBe("none");
});

test("a genuine crash still shows the down card even with an install sitting installed (finding #5, code review)", () => {
  // Regression test. `installedReady` used to be
  // `banner === "update-restart" || updateState === "installed"` — and
  // `updateState` never reverts from "installed" until an actual restart
  // happens, so once ANY update installed during a session, this OR stayed
  // true for the rest of it. A later, wholly unrelated server crash (no
  // restart ever requested, `stage` still "ready") flips `banner` to "down"
  // on consecutive failed probes exactly as it always did — but the OR used
  // to swallow that "down" and keep showing "none" forever. The fix narrows
  // the door to `banner === "update-restart"` alone, which `reduceProbe`
  // itself clears the instant probes start failing, so a real outage is no
  // longer hidden behind old news from the update store.
  expect(surface({ banner: "down", updateState: "installed", stage: "ready" })).toBe("down");
});

test("the down card is suppressed for every in-flight stage", () => {
  // Step 5. The app being gone IS the restart working; a down card during it
  // would tell the opposite story from the restart notification.
  for (const stage of ["quitting", "restarting", "reconnecting", "back"] as const) {
    expect(surface({ banner: "down", stage })).toBe("none");
  }
});

test("the cap gives the down card back while the server is still gone", () => {
  // D4, and the case the cap exists for. Both suppression doors stay open
  // through an outage — the update store keeps its last value when its poll
  // fails, and `update-restart` is the last thing the banner knew — so
  // `gave-up` has to outrank both or the down card never comes back.
  expect(surface({ banner: "down", stage: "gave-up" })).toBe("down");
  expect(surface({ banner: "down", updateState: "installed", stage: "gave-up" })).toBe("down");
});

test("the cap does NOT show the down card over a server that is answering", () => {
  // bugbot, PR #1214. A restart that did not take leaves the app demonstrably
  // running with the disk still ahead: "fused-render isn't running" would be a
  // lie. The restart notification (`UpdateNotifier`), not this function, is
  // what offers the retry on `gave-up`.
  expect(surface({ banner: "update-restart", stage: "gave-up" })).toBe("none");
  expect(surface({ updateState: "installed", stage: "gave-up" })).toBe("none");
  // Nothing to say at all is still nothing to say.
  expect(surface({ banner: "hidden", stage: "gave-up" })).toBe("none");
  expect(surface({ banner: "reconnected", stage: "gave-up" })).toBe("reconnected");
});

test("a restart in flight suppresses every other card too", () => {
  expect(surface({ banner: "update-refresh", stage: "reconnecting" })).toBe("none");
  expect(surface({ banner: "reconnected", stage: "back" })).toBe("none");
  expect(surface({ banner: "hidden", stage: "quitting" })).toBe("none");
});

test("bannerSurface never returns the down card for any in-flight restart stage", () => {
  // New verification named in SPEC-update-notifications.md, stated as a sweep
  // over `restartInFlight`'s own predicate so a stage added to that set
  // cannot quietly reopen the down card during a restart. `ready` (no restart
  // under way) and `gave-up` (the cap's D4 fall-through, which is SUPPOSED to
  // give the down card back) are correctly excluded — see the tests above.
  for (const stage of RESTART_STAGES.filter(restartInFlight)) {
    expect(surface({ banner: "down", stage })).toBe("none");
  }
});

// ---- the refresh preview (dev only) ---------------------------------------
// `updateDialogPreview` used to also answer for `UpdateDialog`'s "restart"
// mode (`?update_modal=restart&stage=...`); that mode is deleted along with
// the dialog it previewed (SPEC-update-notifications.md), so only the refresh
// preview is left to test.

test("the preview flag previews the refresh dialog, from the URL or from storage", () => {
  expect(updateDialogPreview("?update_modal=1", null)).toEqual({ kind: "refresh" });
  expect(updateDialogPreview("", "1")).toEqual({ kind: "refresh" });
});

test("no flag, no preview", () => {
  expect(updateDialogPreview("", null)).toBeNull();
  expect(updateDialogPreview("?update_modal=0", "0")).toBeNull();
  // The restart preview's old value is gone with the mode it previewed — a
  // stale bookmark or localStorage entry from before this change previews
  // nothing rather than reaching for a component that no longer exists.
  expect(updateDialogPreview("?update_modal=restart", null)).toBeNull();
  expect(updateDialogPreview("", "restart")).toBeNull();
});

test("a hidden tab keeps probing only while a restart is in flight", () => {
  // The reader presses Restart, sees the app back in the Dock, clicks it — the
  // tab is hidden at the exact moment the new server starts answering. A tab
  // that stops probing there never sees the version move and never reloads.
  for (const stage of ["quitting", "restarting", "reconnecting", "back"] as const) {
    expect(probeWhileHidden(stage)).toBe(true);
  }
  for (const stage of ["ready", "gave-up"] as const) {
    expect(probeWhileHidden(stage)).toBe(false);
  }
});

test("the poll tick probes a visible tab always, and a hidden one only mid-restart", () => {
  expect(probeOnTick("visible", "ready")).toBe(true);
  expect(probeOnTick("hidden", "ready")).toBe(false);
  expect(probeOnTick("hidden", "gave-up")).toBe(false);
  expect(probeOnTick("hidden", "reconnecting")).toBe(true);
  expect(probeOnTick("hidden", "quitting")).toBe(true);
  // A DOM with no visibilityState at all (the test shim) is not a hidden tab.
  expect(probeOnTick(undefined, "ready")).toBe(true);
});
