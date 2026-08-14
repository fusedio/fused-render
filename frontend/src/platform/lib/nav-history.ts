// BACK AND FORWARD, for the crumb bar's own pair of arrows. UI-free.
//
// The shell is a single document that never reloads (lib/router pushes state and
// dispatches NAV_EVENT), so the browser's session history IS the app's
// navigation history: every folder hop, every file opened, every bookmark
// followed is one entry in it. There is nothing to model here — `history.back()`
// and `history.forward()` already do exactly the right thing, and they do it for
// entries this app never created (an external link that landed the tab here) as
// well as its own. Which is why this file wraps rather than reimplements: an
// app-owned stack beside the browser's would be a second, disagreeing answer to
// "where was I", and the first time they disagreed the arrows would lie.
//
// THE HARD PART IS THE OTHER HALF: whether either direction has anywhere to go.
// The classic History API cannot say. `history.length` counts the whole session
// including forward entries and gives no position within it, so it answers
// neither question, and the usual workaround — stamping an index into
// history.state and diffing it on popstate — is not available to this app for a
// concrete reason: three other places push and replace state with payloads of
// their own (router's `{ fsDir }` nav hint, Tabs' `{ fusedActiveTab }`, Panel's
// structural pushes), and a reload keeps the OLD stamps in the browser's
// serialized state while our counter restarts at zero. Stale stamps colliding
// with fresh ones is a disabled Back button on a page that can go back, which is
// strictly worse than an enabled one that does nothing.
//
// So ASK THE ENGINE, and only the engine: the Navigation API exposes
// `navigation.canGoBack` / `canGoForward` as facts about the real entry list,
// reload and foreign entries included. Chromium has it, so the browser the app
// actually opens in answers truthfully. WebKit does not (the menubar pin's
// WKWebView, Safari), and there the honest answer is "unknown" — which resolves
// to ENABLED, never disabled. A live button that occasionally no-ops costs a
// click; a greyed-out button that was wrong costs the user the belief that the
// control works at all.
//
// `reach` and not `canGoBack`, because the pair is read together and the two
// bits always come from one source — a snapshot of what is within reach.

export type NavReach = { back: boolean; forward: boolean };

// The slice of `window.navigation` this file uses. Hand-written because the
// installed TypeScript's lib.dom does not ship the Navigation API yet, and both
// fields are optional so a PARTIAL implementation (the API present, these
// getters not) still falls through to the enabled default rather than reading
// `undefined` as `false`.
type NavigationLike = {
  canGoBack?: boolean;
  canGoForward?: boolean;
  addEventListener?: (type: string, listener: () => void) => void;
  removeEventListener?: (type: string, listener: () => void) => void;
};

// Pure, and the whole policy in four lines: a missing API — or a missing field
// on a present one — means "unknown", and unknown means enabled. Exported for
// its own test; nothing else should need it.
export function navReachOf(nav: NavigationLike | null | undefined): NavReach {
  return {
    back: typeof nav?.canGoBack === "boolean" ? nav.canGoBack : true,
    forward: typeof nav?.canGoForward === "boolean" ? nav.canGoForward : true,
  };
}

function navigationApi(): NavigationLike | undefined {
  return (window as unknown as { navigation?: NavigationLike }).navigation;
}

// useSyncExternalStore compares snapshots by IDENTITY, so a freshly built object
// on every read is an infinite re-render loop. Cached and replaced only when a
// bit actually flips — the standard memoized-snapshot shape, same as the sidebar
// store's.
let cached: NavReach = { back: true, forward: true };

export function navReach(): NavReach {
  const next = navReachOf(navigationApi());
  if (next.back !== cached.back || next.forward !== cached.forward) cached = next;
  return cached;
}

// THREE EVENTS, and none of them is redundant.
//
//   currententrychange  the Navigation API's own signal, and the only one that
//                       is guaranteed to fire AFTER canGoBack/canGoForward have
//                       been updated. Absent on engines without the API — where
//                       the snapshot is constant anyway, so nothing is missed.
//   popstate            a traversal. It fires early enough that the getters can
//                       still read stale on some paths; harmless, because the
//                       currententrychange behind it re-reads.
//   fused:navigate      an in-app push (lib/router's NAV_EVENT). A push always
//                       clears the forward list, which is a state change with no
//                       traversal and no popstate to announce it.
//
// Spelled out rather than imported as router's `NAV_EVENT` constant, which is
// the one thing in here that is a trade and not a fact: router runs real work at
// module init (it rewrites legacy URLs off `location` and reads three framing
// flags), so importing it would drag a document into every consumer of this
// file — including its test, which is otherwise pure.
export function subscribeNavReach(onChange: () => void): () => void {
  const nav = navigationApi();
  nav?.addEventListener?.("currententrychange", onChange);
  window.addEventListener("popstate", onChange);
  window.addEventListener("fused:navigate", onChange);
  return () => {
    nav?.removeEventListener?.("currententrychange", onChange);
    window.removeEventListener("popstate", onChange);
    window.removeEventListener("fused:navigate", onChange);
  };
}

// Plain history traversals, not `navigation.back()`: the classic calls work on
// every engine and land in the same place, and popstate — which the whole shell
// already listens to (useNavEpoch) — is what remounts the destination view.
export function goBack(): void {
  history.back();
}

export function goForward(): void {
  history.forward();
}
