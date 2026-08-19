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
// the change. A page load resets the module, which is exactly the boundary a
// deep link needs — the first chat target a fresh page shows keeps whatever
// params the link carried; only a change AFTER that is a retarget.
import { replaceSearch } from "@platform/lib/router";

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

// Called with the pane's current chat target on every render where the chat is
// what the pane shows, and with null otherwise. Null holds the tracked target
// rather than clearing it: the pane flipping to Git and back is not a retarget,
// and neither is the skeleton frame while companion probes are out.
export function dropStaleChatParams(target: string | null): void {
  if (target === null) return;
  if (lastTarget === null || target === lastTarget) {
    lastTarget = target;
    return;
  }
  lastTarget = target;
  const stripped = searchWithoutChatParams(location.search);
  if (stripped !== null) replaceSearch(location.pathname + stripped);
}

// Tests only: the module state IS the feature, so tests must be able to start over.
export function resetChatParamTracking(): void {
  lastTarget = null;
}
