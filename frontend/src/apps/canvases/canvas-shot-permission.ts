// WHICH ROAD A CANVAS SCREENSHOT MAY TAKE (CanvasWorkspace `requestCanvasShot`).
//
// Two roads: the NATIVE still of the workbench frame off the screen
// (canvas-shot-native.ts, ~100 ms, touches nothing) and the SLOW embed
// capture (`fused-embed-capture`, the workbench's own html-to-image — seconds
// under a scrim, the canvas locked). Native is always tried first.
//
// What happens when native cannot is the platform's call (owner, 2026-10-28):
//   - macOS: native ONLY. The still is always available there and the one
//     thing that stops it is the Screen Recording grant, which is the user's
//     to give once. So a failed native shot is an ERROR that names the grant
//     — not a silent 20-second fallback. The slow road exists on a Mac only
//     behind a switch in Settings (`slowFallbackEnabled`), off by default.
//   - Windows / Linux: native when the backend has a still, else the slow
//     road, automatically — there is no grant to point at.
//
// Pure helpers here so the decision and the sentences are testable without
// the page.

import { getPrefsShared } from "@platform/lib/api";
import { isMac as detectedMac } from "@platform/lib/platform";
import { screenRecordingNeeded } from "@platform/lib/screen-permission";

export { screenRecordingNeeded };

/** Settings › Chats & tasks › Canvas screenshots › "Fall back to in-canvas
 *  capture" — the `canvas_shot_slow_fallback_enabled` preference
 *  (shell/prefs.py), read on every capture so a flip in Settings applies to
 *  the next screenshot. A preference, not localStorage: a fact the user
 *  would miss across a relaunch belongs behind the API (D1386). A read that
 *  fails is "off" — the default, and the refusal names the switch. */
export function slowFallbackEnabled(
  read: () => Promise<{ canvas_shot?: { slow_fallback?: boolean } }> = getPrefsShared,
): Promise<boolean> {
  return read().then(
    (p) => p.canvas_shot?.slow_fallback === true,
    () => false,
  );
}

/** Whether the slow embed road may run after native said no. A Mac WITHOUT
 *  a native still at all (macOS 11–12, `GET /api/capture` → `screenshot.
 *  available: false`) has no grant to point at, so it falls back like any
 *  other platform. */
export function slowRoadAllowed(g: { mac: boolean; fallbackOn: boolean; nativeAvailable?: boolean }): boolean {
  return !g.mac || g.fallbackOn || g.nativeAvailable === false;
}

export async function slowRoadAllowedHere(nativeAvailable?: boolean): Promise<boolean> {
  // Off a Mac the answer needs no round trip.
  if (!detectedMac) return true;
  if (nativeAvailable === false) return true;
  return slowRoadAllowed({ mac: true, fallbackOn: await slowFallbackEnabled(), nativeAvailable });
}

/** The error a Mac gets when native could not and the slow road is off. One
 *  sentence of cause, one of the fix. `permissionMissing` is the OS's own
 *  answer (`GET /api/capture` → `screenshot.granted === false`); without it
 *  the cause is situational (window behind another, a dialog over the
 *  frame, a frame off the display). `app` is the bundle's display name. */
export function nativeShotRefusal(g: { permissionMissing: boolean; app: string }): string {
  const fallback = `or turn on "Fall back to in-canvas capture" in Settings`;
  if (g.permissionMissing) {
    return `canvas screenshots need screen permission — allow ${g.app} in System Settings › Screen & System Audio Recording and relaunch, ${fallback}`;
  }
  return `could not photograph the canvas — bring ${g.app} to the front and try again, ${fallback}`;
}
