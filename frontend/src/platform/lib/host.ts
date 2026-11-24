// WHICH HOST PAGE THIS CODE RUNS IN. The desktop shell (index.html) and the
// phone page (lan.html, served by fused_render/lan.py to a paired iPhone) mount
// the SAME React apps — `@apps/bots`, the shell's Tasks page, `@apps/claude`'s
// ChatMount (D1468). What differs is the door: the LAN listener forwards an
// allow-list of the desktop's routes (lan.py `_DOORS`) and 404s the rest
// (take-over, pop-out, worktrees, erase, the Mac's browsers and iMessage …).
// A component whose control would only ever 404 there hides it by asking
// here, so the phone never shows a button that cannot work. lan.html stamps
// `data-host="lan"` on <html>; nothing is inferred from the hostname or the
// user agent, which both lie (a Mac browser on the LAN port, a phone on
// loopback through a tunnel).
export function isLanHost(): boolean {
  // `documentElement` can be missing under a test's bare document shim —
  // then this is not the LAN page either (2026-11-07: 139 suites red on main
  // from the throw here).
  return typeof document !== "undefined" && document.documentElement?.dataset?.host === "lan";
}

/** Inside the native iOS shell (its webview marks the UA; lan.py keys off the
 *  same marker). The shell draws its own top bar from the page's <title>, so a
 *  page drops its own title row there. */
export const IN_PHONE_APP = typeof navigator !== "undefined" && /FusedRender-iOS\//.test(navigator.userAgent);
