// SCREEN RECORDING FOR THE FAST CANVAS SCREENSHOT — the two facts the canvas
// page (apps/canvases/canvas-shot-permission.ts) and the Settings page
// (shell/settings/screenshots.tsx) both need: whether the grant is what stands
// between this Mac and the native still, and how to open the pane it is
// granted on. Lives in platform so neither side imports the other.

/** `GET /api/capture` → `sources.screenshot`, as `captureSources` returns it. */
export interface ScreenshotSource {
  available?: boolean | null;
  granted?: boolean | null;
  reason?: string | null;
}

/** Whether the grant is what stands between this page and the fast road:
 *  the platform HAS a still (`available`) and the OS says it is not granted.
 *  `granted: null` is "unknown" (a backend with no preflight) and is not a
 *  reason to nag. */
export function screenRecordingNeeded(source: ScreenshotSource | null | undefined): boolean {
  return !!source && source.available === true && source.granted === false;
}

/** System Settings › Privacy & Security › Screen & System Audio Recording —
 *  the same deep link the server opens (`capture/_darwin.py`
 *  `SHOT_SETTINGS_URL`). */
export const SCREEN_RECORDING_SETTINGS_URL =
  "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";

/** Open the pane: the server's door first (it runs `open`, which works from
 *  the app's own window), and when that door is not there — a dev shell
 *  proxied to an older server (404), a platform without one (409) — the deep
 *  link straight from the page, which a browser hands to the OS. */
export async function openScreenRecordingPane(
  server: () => Promise<unknown>,
  navigate: (url: string) => void = (url) => {
    window.location.assign(url);
  },
): Promise<"server" | "link"> {
  try {
    await server();
    return "server";
  } catch {
    navigate(SCREEN_RECORDING_SETTINGS_URL);
    return "link";
  }
}
