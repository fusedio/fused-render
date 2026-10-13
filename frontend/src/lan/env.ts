// Inside the native iOS shell (its webview marks the UA; lan.py keys off the
// same marker). The shell draws its own top bar from the page's <title>, so a
// page drops its own title row there.
export const IN_APP = typeof navigator !== "undefined" && /FusedRender-iOS\//.test(navigator.userAgent);
