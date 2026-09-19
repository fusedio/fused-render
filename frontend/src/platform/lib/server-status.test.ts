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
  previewInstalledVersion,
  previewRequestedAt,
  probeOnTick,
  probeWhileHidden,
  reduceProbe,
  updateDialogMode,
  updateDialogPreview,
  type StatusState,
  type SurfaceInput,
} from "@platform/lib/server-status";
import { restartIsSlow, RESTART_SLOW_MS, RESTART_STAGES } from "@platform/lib/restart-flow";

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

test("the disk being ahead raises the DIALOG, not a card", () => {
  // It used to be `.server-status-update`, a card in the notification stack
  // with a bare relaunch link on it that sat there forever (D1).
  expect(surface({ banner: "update-restart" })).toBe("restart-dialog");
});

test("the dialog pops on its own the moment the install lands", () => {
  // D2: the shared update store says "installed" on its 2 s busy cadence, two
  // ticks before the banner's own 5 s probe notices the disk moved. Neither the
  // banner state nor a press is needed.
  expect(surface({ updateState: "installed" })).toBe("restart-dialog");
  // …and only that state. An install still running is the badge's story.
  expect(surface({ updateState: "installing" })).toBe("none");
  expect(surface({ updateState: "available" })).toBe("none");
  expect(surface({ updateState: "error" })).toBe("none");
});

test("the down card is suppressed for every in-flight stage", () => {
  // Step 5. The app being gone IS the restart working; two surfaces telling
  // opposite stories about one outage is the bug.
  for (const stage of ["quitting", "restarting", "reconnecting", "back"] as const) {
    expect(surface({ banner: "down", stage })).toBe("restart-dialog");
  }
});

test("the cap gives the down card back while the server is still gone", () => {
  // D4, and the case the cap exists for. Both doors into the dialog stay open
  // through an outage — the update store keeps its last value when its poll
  // fails, and `update-restart` is the last thing the banner knew — so
  // `gave-up` has to outrank both or the modal outlives the promise it made.
  expect(surface({ banner: "down", stage: "gave-up" })).toBe("down");
  expect(surface({ banner: "down", updateState: "installed", stage: "gave-up" })).toBe("down");
});

test("the cap does NOT show the down card over a server that is answering", () => {
  // bugbot, PR #1214. A restart that did not take leaves the app demonstrably
  // running with the disk still ahead: "fused-render isn't running" would be a
  // lie, and with the dialog gone the only way back to the button is a page
  // reload. The ordinary doors take it instead — and `UpdateDialog` draws the
  // button for `gave-up` exactly as it does for `ready`.
  expect(surface({ banner: "update-restart", stage: "gave-up" })).toBe("restart-dialog");
  expect(surface({ updateState: "installed", stage: "gave-up" })).toBe("restart-dialog");
  // Nothing to say at all is still nothing to say.
  expect(surface({ banner: "hidden", stage: "gave-up" })).toBe("none");
  expect(surface({ banner: "reconnected", stage: "gave-up" })).toBe("reconnected");
});

test("a restart in flight outranks every other card too", () => {
  expect(surface({ banner: "update-refresh", stage: "reconnecting" })).toBe("restart-dialog");
  expect(surface({ banner: "reconnected", stage: "back" })).toBe("restart-dialog");
  expect(surface({ banner: "hidden", stage: "quitting" })).toBe("restart-dialog");
});

test("a stage the dialog is not on screen for leaves the banner alone", () => {
  // Only `ready` and `gave-up`; every other stage is covered above. Stated as a
  // sweep so a stage added to the machine cannot quietly acquire a surface.
  const holds = RESTART_STAGES.filter((stage) => surface({ stage }) === "restart-dialog");
  expect(holds).toEqual(["quitting", "restarting", "reconnecting", "back"]);
});

// ---- the restart preview (dev only) ---------------------------------------

test("the preview flag says WHICH dialog, and the restart one says which stage", () => {
  expect(updateDialogPreview("?update_modal=1", null)).toEqual({ kind: "refresh" });
  expect(updateDialogPreview("?update_modal=restart&stage=reconnecting", null)).toEqual({
    kind: "restart",
    stage: "reconnecting",
    slow: false,
  });
  for (const stage of RESTART_STAGES) {
    expect(updateDialogPreview(`?update_modal=restart&stage=${stage}`, null)).toEqual({
      kind: "restart",
      stage,
      slow: false,
    });
  }
});

test("an unknown or missing stage previews the face a real restart starts on", () => {
  expect(updateDialogPreview("?update_modal=restart", null)).toEqual({
    kind: "restart",
    stage: "ready",
    slow: false,
  });
  expect(updateDialogPreview("?update_modal=restart&stage=melting", null)).toEqual({
    kind: "restart",
    stage: "ready",
    slow: false,
  });
});

test("no flag, no preview — and the restart flag is a preview like the refresh one", () => {
  expect(updateDialogPreview("", null)).toBeNull();
  expect(updateDialogPreview("?update_modal=0", "0")).toBeNull();
  expect(updateDialogMode(true, "?update_modal=restart", null)).toBe("preview");
  // Storage carries it across page loads, exactly as "1" always has.
  expect(updateDialogPreview("", "restart")).toEqual({ kind: "restart", stage: "ready", slow: false });
});

test("`slow=1` previews the overlong wait, which is not a stage and cannot be named as one", () => {
  // The machine is still in `restarting` past 25s — the sentence changes, the
  // stage does not — so the seventh face has to be reachable by a flag or not
  // at all.
  expect(updateDialogPreview("?update_modal=restart&stage=restarting&slow=1", null)).toEqual({
    kind: "restart",
    stage: "restarting",
    slow: true,
  });
  // Anything but "1" is off, the way the dialog flag itself reads its own value.
  expect(updateDialogPreview("?update_modal=restart&stage=restarting&slow=yes", null)).toEqual({
    kind: "restart",
    stage: "restarting",
    slow: false,
  });
  expect(updateDialogPreview("?update_modal=restart&stage=restarting", null)).toEqual({
    kind: "restart",
    stage: "restarting",
    slow: false,
  });
});

test("the pretend press is placed past the sentence's own estimate, or at the instant itself", () => {
  const now = 1_700_000_000_000;
  // Far enough back that the dialog reads the wait as overlong on the FIRST
  // paint — a preview you have to wait 25 seconds for is not a preview.
  expect(previewRequestedAt(true, now)).toBeLessThanOrEqual(now - RESTART_SLOW_MS);
  expect(restartIsSlow(previewRequestedAt(true, now), now)).toBe(true);
  // And a plain preview freezes the ordinary sentence: the press is NOW, so
  // nothing crosses the mark while the page is being looked at.
  expect(previewRequestedAt(false, now)).toBe(now);
  expect(restartIsSlow(previewRequestedAt(false, now), now)).toBe(false);
});

test("the preview invents the one disagreement a dev server cannot supply", () => {
  // Nothing newer on disk — the dev case — so the sentence would otherwise read
  // "Closing v0.5.53 and starting v0.5.53", which no reader will ever see.
  expect(previewInstalledVersion("0.5.53", "0.5.53")).toBe("0.5.54");
  expect(previewInstalledVersion("0.5.53", "")).toBe("0.5.54");
  // A REAL pending update wins: the preview shows the number actually waiting
  // rather than a made-up one.
  expect(previewInstalledVersion("0.5.53", "0.6.0")).toBe("0.6.0");
  // A version it cannot parse is left exactly as it is — inventing a successor
  // for it would put a string on screen that is not a version at all.
  expect(previewInstalledVersion("nightly", "nightly")).toBe("nightly");
  expect(previewInstalledVersion("0.5.x", "0.5.x")).toBe("0.5.x");
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
