import { expect, test } from "bun:test";

import { SCREEN_RECORDING_SETTINGS_URL, openScreenRecordingPane } from "@platform/lib/screen-permission";

import {
  nativeShotRefusal,
  screenRecordingNeeded,
  slowFallbackEnabled,
  slowRoadAllowed,
} from "./canvas-shot-permission";

test("the grant is 'missing' only on a platform WITH a still that the OS says is not granted", () => {
  expect(screenRecordingNeeded({ available: true, granted: false })).toBe(true);
  expect(screenRecordingNeeded({ available: true, granted: true })).toBe(false);
  // Unknown (no preflight on this backend): not a missing grant.
  expect(screenRecordingNeeded({ available: true, granted: null })).toBe(false);
  expect(screenRecordingNeeded({ available: false, granted: false })).toBe(false);
  expect(screenRecordingNeeded(null)).toBe(false);
  expect(screenRecordingNeeded(undefined)).toBe(false);
});

test("the slow road is automatic off a Mac, and on a Mac only behind the switch", () => {
  expect(slowRoadAllowed({ mac: false, fallbackOn: false })).toBe(true);
  expect(slowRoadAllowed({ mac: false, fallbackOn: true })).toBe(true);
  expect(slowRoadAllowed({ mac: true, fallbackOn: false })).toBe(false);
  expect(slowRoadAllowed({ mac: true, fallbackOn: true })).toBe(true);
});

test("the switch is the preference, only a stored true is on, and a failed read is off", async () => {
  expect(await slowFallbackEnabled(async () => ({ canvas_shot: { slow_fallback: true } }))).toBe(true);
  expect(await slowFallbackEnabled(async () => ({ canvas_shot: { slow_fallback: false } }))).toBe(false);
  // An older server without the key.
  expect(await slowFallbackEnabled(async () => ({}))).toBe(false);
  expect(
    await slowFallbackEnabled(async () => {
      throw new Error("HTTP 500");
    }),
  ).toBe(false);
});

test("the refusal names the grant when the OS says it is missing, the situation otherwise — and the switch both times", () => {
  const perm = nativeShotRefusal({ permissionMissing: true, app: "Fused Render" });
  expect(perm).toContain("need screen permission");
  expect(perm).toContain("allow Fused Render in System Settings");
  expect(perm).toContain("relaunch");
  expect(perm).toContain("Fall back to in-canvas capture");
  const situ = nativeShotRefusal({ permissionMissing: false, app: "Fused Render" });
  expect(situ).toContain("could not photograph the canvas");
  expect(situ).toContain("bring Fused Render to the front");
  expect(situ).toContain("Fall back to in-canvas capture");
  expect(situ.length).toBeLessThan(140);
});

test("the pane opens through the server, and by deep link when that door is missing", async () => {
  const went: string[] = [];
  const nav = (u: string) => void went.push(u);
  expect(await openScreenRecordingPane(async () => ({ ok: true }), nav)).toBe("server");
  expect(went).toEqual([]);
  expect(
    await openScreenRecordingPane(async () => {
      throw new Error("HTTP 404");
    }, nav),
  ).toBe("link");
  expect(went).toEqual([SCREEN_RECORDING_SETTINGS_URL]);
});
