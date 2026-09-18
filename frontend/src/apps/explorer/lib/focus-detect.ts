// The pure half of the home-page focus-change-detection trigger: "given a
// hidden-since timestamp and now, should this fire?" — kept out of
// FilesHome.tsx the same way `shell/indexing-lib.ts` and
// `apps/explorer/lib/home-search.ts` keep their testable halves out of the
// component that owns the effect.
//
// See SPEC-focus-change-detection.md. Home search is index-backed and
// global, so a file dropped anywhere under a scan root while the user was on
// another tab (a browser download landing in ~/Downloads, say) stays
// invisible until something rescans that root. The server can detect this
// cheaply on macOS by replaying the FSEvents journal
// (fused_render/index/detect.py), but only when asked — this module decides
// WHEN to ask.

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
// matter what this module decides, because a stale tab or a modified client
// must not be able to force a check by just claiming a bigger number.
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
