// The chat's URL params, and when the pane must take them back off the URL.
//
// The claude pane's template writes its own state — `session_id`, `run`, the
// `msg` anchor — onto the SHELL's url (it sets no `_fusedParamBoundary`, so the
// runtime's ancestor-climb lands there; templates/claude/template.html). That is
// what lets a task deep-link open the right conversation. But switching the
// pane's SUBJECT by clicking another row is a selection change, not a
// navigation: router.navigate() never runs, the `?sel=` mirror rewrites only its
// own key, and the three chat params survive onto a target they say nothing
// about. The pane's iframe reboots on the new `_file`, reads the stale params
// back off the shell url, and the old conversation follows the selection — a
// live run's whole transcript re-attached under whichever folder is clicked
// (the reported shape: duplicate a folder, chat in the duplicate, click the
// original — same conversation on both).
//
// So the pane drops the chat params whenever its chat TARGET changes. Module
// state rather than component state, deliberately: ListingPreviewPane remounts,
// and a per-mount ref would read every remount as a first mount and never see
// the change.
//
// What resets the tracking is a NAVIGATION, any navigation: whatever params the
// arriving entry carries are that entry's own — navigate() already strips them,
// and a deep link (a Tasks row, a bookmark through navigateUrl) carries them on
// purpose — so the first chat target the new entry shows ADOPTS its params
// rather than stripping them. A hard page load resets by reloading the module;
// in-app pushes say so through the router's own NAV_EVENT, and Back/Forward
// through popstate. Without those two listeners an SPA hop from the Tasks page
// would land on a pane whose tracked target is still the folder the user left,
// read the hop as a retarget, and strip the very deep-link params it arrived
// with.
import { NAV_EVENT, replaceSearch } from "@platform/lib/router";

// What the chat template owns on the shell url. `split` is layout, not
// conversation, and stays; `msg` goes with the session it anchors into.
const CHAT_PARAMS = ["session_id", "run", "msg"];

let lastTarget: string | null = null;

// The search string with the chat params removed, or null when none were
// present (so the caller can skip the URL write entirely). Pure — the stateful
// wrapper below and the tests share it.
export function searchWithoutChatParams(search: string): string | null {
  const params = new URLSearchParams(search);
  let changed = false;
  for (const key of CHAT_PARAMS) {
    if (params.has(key)) {
      params.delete(key);
      changed = true;
    }
  }
  if (!changed) return null;
  const qs = params.toString();
  return qs ? "?" + qs : "";
}

// The module's three touches of the page, injectable so the tests can drive
// the state machine without staging `location`/`history` globals (which the
// bun suite shares across files — a shim staged here collided with other
// files' shims depending on run order).
export interface ChatParamIo {
  pathname(): string;
  search(): string;
  write(url: string): void;
}

const pageIo: ChatParamIo = {
  pathname: () => location.pathname,
  search: () => location.search,
  write: replaceSearch,
};

// Called with the pane's current chat target on every render where the chat is
// what the pane shows, and with null otherwise. Null holds the tracked target
// rather than clearing it: the pane flipping to Git and back is not a retarget,
// and neither is the skeleton frame while companion probes are out.
//
// `urlNamed` says the target IS what the current url itself names — the row its
// `?sel=` points at, or the folder when there is no `?sel=`. Such a hop adopts
// rather than strips, because it is the URL playing out, not the user leaving a
// chat: a deep link with `?sel=…&session_id=…` mounts the pane on the FOLDER for
// a beat while the rows load, and the seeded selection then retargets it to the
// very row the link named — stripping there threw away the link's own params.
// A user's row CLICK is never url-named at the moment it retargets: the `?sel=`
// mirror trails the click by its debounce, so the url still names the row being
// LEFT, and the strip goes through exactly as before.
export function dropStaleChatParams(
  target: string | null,
  urlNamed: boolean,
  io: ChatParamIo = pageIo
): void {
  if (target === null) return;
  if (lastTarget === null || target === lastTarget || urlNamed) {
    lastTarget = target;
    return;
  }
  lastTarget = target;
  const stripped = searchWithoutChatParams(io.search());
  if (stripped !== null) io.write(io.pathname() + stripped);
}

// The navigation reset (see the module comment). Exported for the listeners
// below and for tests.
export function resetChatParamTracking(): void {
  lastTarget = null;
}

// Registered at module load, once, for the page the module actually runs in.
// Guarded because the bun test environment has no full `window`; the browser
// always does.
if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  window.addEventListener(NAV_EVENT, resetChatParamTracking);
  window.addEventListener("popstate", resetChatParamTracking);
}
