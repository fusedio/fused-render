// THE PANEL CHAT'S PARAMS ARE THE HOST URL'S `_right` (SPEC §56 WS-18, D1515).
//
// A page's Claude pane keeps its chat's params (`session_id`, `run`, `split`,
// …) in the page URL, through the URL store (apps/claude/params/store
// `createUrlParamsStore`) and its rules: the once-per-visit push only after a
// gesture, the 400ms throttle, a traversal dropping a pending write. The
// panel's chat keeps EXACTLY those rules by running the same store over an
// env whose "location" is the chat's slice of `_right` (`chat:<file>:<query>`):
//   - `location.search` is "?" + the view's query, read live from the host;
//   - `replace` / `history.pushState` write `_right` back (a replace, a push);
//   - its own `fused:urlchange` / `popstate` go to a private target the host
//     dispatches on, while the gesture listeners (`pointerdown`, `keydown`)
//     and `pagehide` stay the window's — the gesture is the reader's, anywhere
//     in the window.
// The pathname is fixed: the mount is keyed on the file, so a write queued for
// another file can never land (the store's own stale-path rule).
import { createUrlParamsStore, type ParamsStore, type UrlEnv } from "@apps/claude/params/store";

/** The fixed pathname the store sees (never a real route). */
const PANEL_PATH = "/_ws_panel_chat";

/** The host's side of the store. */
export interface PanelParamsHost {
  /** The chat view's query (no `?`) as the host URL holds it now. */
  query(): string;
  /** Write a new query: a replace (a sync) or a push (a history entry,
   *  `state` the store's entry flag to merge into the host's). */
  write(query: string, how: "replace" | "push", state?: unknown): void;
}

const SELF_EVENTS = new Set(["fused:urlchange", "popstate"]);

function searchOf(url: string): string {
  const q = url.indexOf("?");
  return q === -1 ? "" : url.slice(q + 1);
}

export function createPanelParamsStore(
  host: PanelParamsHost,
  events: EventTarget,
): ParamsStore & { attach(): void; dispose(): void } {
  // The window's listeners for the gesture and pagehide, the private target
  // for the URL events (see header).
  const facade: EventTarget = {
    addEventListener: (type: string, cb: EventListenerOrEventListenerObject | null, opts?: boolean | AddEventListenerOptions) =>
      (SELF_EVENTS.has(type) ? events : window).addEventListener(type, cb, opts),
    removeEventListener: (type: string, cb: EventListenerOrEventListenerObject | null, opts?: boolean | EventListenerOptions) =>
      (SELF_EVENTS.has(type) ? events : window).removeEventListener(type, cb, opts),
    dispatchEvent: (e: Event) => events.dispatchEvent(e),
  };
  const env: UrlEnv = {
    location: {
      get pathname() {
        return PANEL_PATH;
      },
      get search() {
        const q = host.query();
        return q ? "?" + q : "";
      },
    },
    history: {
      get state() {
        return history.state;
      },
      pushState: (state, _unused, url) => host.write(searchOf(url), "push", state),
    },
    replace: (url) => host.write(searchOf(url), "replace"),
    events: facade,
    dispatchUrlChange: () => events.dispatchEvent(new Event("fused:urlchange")),
    now: Date.now,
    setTimeout: (fn, ms) => window.setTimeout(fn, ms),
    clearTimeout: (id) => window.clearTimeout(id as number),
  };
  return createUrlParamsStore(env);
}
