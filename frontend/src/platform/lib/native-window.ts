// Apps in their own native windows (macOS, `native_windows_enabled`).
//
// Inside one of the app's native windows (router.ts IS_NATIVE_WINDOW) an app
// click — a card, the app page's Open — does not navigate the window it happened
// in: the server's window manager focuses that app's own window, or opens
// one running the app's entry page as an embed, at the size and place the
// user last left it (POST /api/windows/open → mac_window.py
// `focus_or_open_app`). The window's title-bar Edit button is the way from
// there into the explorer.
import { postJson } from "./api";

// Ask for the app's window. `path` is the app folder (or a `.fused` file).
// Resolves false when the server has no window manager to ask (404: the app
// was relaunched without native windows, or this is `fused-render serve`) or
// the request failed — the caller then navigates in place, so a click never
// does nothing.
export async function openAppWindow(path: string): Promise<boolean> {
  try {
    await postJson<{ ok: boolean }>("/api/windows/open", { path });
    return true;
  } catch {
    return false;
  }
}
