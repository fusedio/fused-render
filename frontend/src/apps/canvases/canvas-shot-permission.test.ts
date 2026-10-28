import { expect, test } from "bun:test";

import { SCREEN_RECORDING_SETTINGS_URL, openScreenRecordingPane } from "@platform/lib/screen-permission";

import {
  SLOW_FALLBACK_KEY,
  nativeShotRefusal,
  screenRecordingNeeded,
  setSlowFallbackEnabled,
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

test("the switch is remembered on this machine, and a broken storage reads off", () => {
  const store = new Map<string, string>();
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  };
  expect(slowFallbackEnabled(storage)).toBe(false);
  setSlowFallbackEnabled(true, storage);
  expect(store.get(SLOW_FALLBACK_KEY)).toBe("1");
  expect(slowFallbackEnabled(storage)).toBe(true);
  setSlowFallbackEnabled(false, storage);
  expect(store.has(SLOW_FALLBACK_KEY)).toBe(false);
  const broken = {
    getItem: () => {
      throw new Error("private mode");
    },
    setItem: () => {
      throw new Error("quota");
    },
    removeItem: () => {
      throw new Error("quota");
    },
  };
  expect(slowFallbackEnabled(broken)).toBe(false);
  expect(() => setSlowFallbackEnabled(true, broken)).not.toThrow();
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
