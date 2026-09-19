// The pure half of the home-page focus-change-detection trigger: "given a
// hidden-since timestamp and now, should this fire?" — kept out of
// FilesHome.tsx the same way `shell/indexing-lib.ts` and
// `apps/explorer/lib/home-search.ts` keep their testable halves out of the
// component that owns the effect.
//
// See SPEC-focus-change-detection.md and DECISIONS.md. Home search is
// index-backed and global, so a file dropped anywhere under a scan root
// while the user was on another tab (a browser download landing in
// ~/Downloads, say) stays invisible until something rescans that root. The
// server (fused_render/index/detect.py) starts an ordinary incremental
// rescan of a stale-enough root when asked — this module decides WHEN to
// ask, not what the server does once asked.

// How long the page must have been hidden before regaining focus is worth
// acting on. The signal this exists for is "the user went away and did
// something else" — a real tab switch, not two of this app's own windows
// trading focus, which fires `visibilitychange` just as readily but changed
// nothing on disk.
//
// Mirrors `fused_render/index/detect.py`'s `MIN_HIDDEN_S` (30s) so a client
// that would not even bother asking agrees with the floor the server enforces
// independently — but this copy is advisory only: it exists to skip a POST
// that the server would refuse anyway, not to be trusted as the decision
// itself. The server re-validates `hidden_s` against its own constant no
// matter what this module decides, because the server cannot verify a
// client-reported duration at all — not because a larger claimed value is
// somehow the dangerous direction (the server's gate is `hidden_s <
// MIN_HIDDEN_S`, so a bigger number is exactly what passes it). What
// actually bounds a client spamming the endpoint is the server's pacing and
// staleness floors (`DETECT_INTERVAL_S`, `FOCUS_STALE_S`,
// `freshness.MIN_INTERVAL_S`), not this one.
export const MIN_HIDDEN_MS = 30_000;

/**
 * Whether a visibility transition — the page having been hidden for
 * `hiddenForMs` milliseconds before becoming visible again — is worth
 * reporting to the server.
 *
 * `hiddenForMs` is `null` when the page was never observed going hidden
 * (the very first `visibilitychange` a tab receives can be a "became
 * visible" with no prior "became hidden" recorded this session, e.g. if the
 * listener attached while already hidden) — there is no duration to judge,
 * so this is always false rather than treating `null` as "hidden forever".
 */
export function shouldNoteFocus(hiddenForMs: number | null): boolean {
  if (hiddenForMs === null) return false;
  return hiddenForMs >= MIN_HIDDEN_MS;
}

/** `hiddenForMs`, in the seconds the server's `hidden_s` body field wants —
 * rounded to the nearest whole second, which is more precision than a 30s
 * floor needs and keeps the wire payload a plain small number rather than a
 * float with a long tail. */
export function hiddenSeconds(hiddenForMs: number): number {
  return Math.round(hiddenForMs / 1000);
}

/**
 * Whether the page should be considered "away" right now.
 *
 * `doc.hidden` catches a real tab switch, occlusion, or minimize — that part
 * is unchanged. The other half is NOT simply `!doc.hasFocus()` (code review,
 * finding 8): this app can be rendered inside an iframe in the packaged
 * desktop shell, alongside sibling panes (the sidebar) that are part of the
 * SAME app window. Per the Page Visibility / focus spec, a framed document's
 * own `hasFocus()` goes false the instant focus moves to ANY other frame —
 * including a sibling pane the user never left the app to reach — so
 * checking it on THIS frame alone counts in-app navigation as "went away and
 * did something else", which is the wrong signal to rescan on.
 *
 * `topHasFocus` below asks the question one level up instead: a document's
 * `hasFocus()` is true whenever the OS-level focused area is ANYWHERE inside
 * that document's own frame subtree, so calling it on the OUTERMOST
 * reachable same-origin ancestor is true for focus anywhere in the app shell
 * (this frame, a sibling pane, the sidebar) and only false once the whole
 * app window itself loses focus to a different application — which is
 * exactly the case this trigger exists for.
 *
 * When no such ancestor is reachable — not framed at all (`window.top ===
 * window`, true for a plain browser tab, local dev, and this function's own
 * unit tests) or a genuine cross-origin ancestor (should never happen for
 * this app's own shell, but must not throw out of a focus listener) — this
 * falls back to the frame's own `hasFocus()`, i.e. the pre-fix, more eager
 * behaviour. That fallback path is NOT a reliable fix for finding 8; it is
 * only exercised when the shell-aware check above cannot run at all.
 */
export function isAway(doc: Document = document, win: Window = window): boolean {
  if (doc.hidden) return true;
  return !topHasFocus(doc, win);
}

function topHasFocus(doc: Document, win: Window): boolean {
  try {
    const top = win.top;
    if (top && typeof top.document?.hasFocus === "function") {
      return top.document.hasFocus();
    }
  } catch {
    // Cross-origin ancestor — fall through to this frame's own signal below.
  }
  return doc.hasFocus();
}
