// Bootstrap: history wrapping, the runtime's fs-change hook, embed class, config
// load, React mount.
import { createRoot } from "react-dom/client";
import { TroubleCard } from "@platform/ui/TroubleCard";
import { IS_EMBED, IS_SNAPSHOT } from "@platform/lib/router";
import { clearListPrefetch, getConfig } from "@platform/lib/api";
import { hydrateBookmarks, refreshBookmarks } from "@platform/lib/bookmarks";
import { hydrateRecents } from "@apps/explorer/lib/recents";
import { notifyBookmarksChanged } from "@platform/lib/hooks";
import App from "@shell/App";
import "./shell.css";

// The preview iframe's injected runtime writes view params via
// parent.history.replaceState (same history object), which fires no event.
// Wrapping replaceState is the shell's only way to observe those param
// changes so the "Update bookmark" button can react to them. pushState is
// wrapped the same way so in-pane navigation is observable too — the layout
// view's runtime target dispatches fused:urlchange through both (LM-8, D46).
// Must happen before mount: pane runtimes may write at any time.
const origReplaceState = history.replaceState.bind(history);
history.replaceState = function (...args: Parameters<History["replaceState"]>) {
  origReplaceState(...args);
  window.dispatchEvent(new Event("fused:urlchange"));
};
const origPushState = history.pushState.bind(history);
history.pushState = function (...args: Parameters<History["pushState"]>) {
  origPushState(...args);
  window.dispatchEvent(new Event("fused:urlchange"));
};

// The one window global this app OWNS (the runtime's other globals are ones it
// reads from templates, not ones the shell writes), so it is declared here beside
// its assignment rather than in an ambient .d.ts nothing else would use.
declare global {
  interface Window {
    _fusedFsChanged?: () => void;
  }
}

// The preview iframe's injected runtime (static/runtime.js) reports a filesystem
// change up the ancestor chain through this global — writeFile, uploadFile,
// mkdir, and every runPython, since a script can write anything.
//
// It has to, because nothing else here can see those writes: they are fetches
// from inside the frame, which is its own JS realm with its own copy of lib/api,
// so this window's listing prefetch cache is untouched by them. The directory
// watcher is not a backstop either — it watches only the ONE folder a mounted
// listing is showing, and a template view of a FILE has no listing at all. The
// symptom was a view saving a file and a navigation to that folder seconds later
// painting the folder as it stood before the save.
//
// A global rather than a postMessage for the same reason the runtime reads params
// off ancestor URLs directly (D3/D4, D46) — same-origin by construction — and
// installed here, beside the history wrapping, because both are contracts with
// that runtime that must exist before any frame can load.
window._fusedFsChanged = clearListPrefetch;

if (IS_EMBED) document.body.classList.add("embed");
// Frozen-tree framing (router.ts IS_SNAPSHOT): a body class rather than props
// threaded through the view tree, exactly like `embed` above — every rule it
// drives is chrome, and chrome is what stylesheets already own.
if (IS_SNAPSHOT) document.body.classList.add("snapshot");

const root = createRoot(document.getElementById("root")!);

getConfig().then(
  (config) => {
    root.render(<App config={config} />);
    // Embed documents are display-only panes/previews. They have no global
    // sidebar, so hydrating its bookmark and recents stores in every iframe is
    // unrelated work — and multiplies the two bounded filesystem checks by the
    // number of preview cards on Home.
    if (IS_EMBED) return;
    // Load the bookmark cache from the server (async; renders empty first, then
    // the sidebar/breadcrumb re-read once it resolves). Independent of config —
    // fire after mount so a config failure still shows its error screen.
    hydrateBookmarks().then(notifyBookmarksChanged);
    // Recents hydrate the same way; the store notifies its own subscribers.
    // (The app builder's parallel (tag, name) store went with its route.)
    void hydrateRecents();
    // Poll every 30 s so another tab's/window's bookmark edits converge here
    // (D77). refreshBookmarks() re-renders only when the tree actually changed.
    // In-flight guarded (mirrors ServerStatusBanner's probingRef, D126): both
    // the interval and the focus listener below call the same pollBookmarks,
    // and refreshBookmarks() shares bookmarks.ts's serial mutation queue — an
    // unguarded burst of focus events (or a focus landing mid-tick) would
    // stack redundant GETs on that queue and delay real bookmark edits behind
    // them, not just waste a request.
    const BOOKMARK_POLL_MS = 30_000;
    let bookmarkPollInFlight = false;
    const pollBookmarks = () => {
      if (bookmarkPollInFlight) return;
      bookmarkPollInFlight = true;
      refreshBookmarks()
        .then((changed) => changed && notifyBookmarksChanged())
        .finally(() => {
          bookmarkPollInFlight = false;
        });
    };
    setInterval(pollBookmarks, BOOKMARK_POLL_MS);
    // Also refresh the instant the window regains focus — the common case for
    // the missing-file flag (D127): switch away, fix/restore the file, switch
    // back, and the sidebar reflects it immediately instead of waiting out the
    // rest of the 30 s tick. Same "refresh on focus" posture as
    // ServerStatusBanner's health probe (D126).
    window.addEventListener("focus", pollBookmarks);
  },
  (err: Error) =>
    // THE BOOT FAILURE, and the one error surface that cannot describe itself:
    // /api/config is what failed, so there is no version, no install path and
    // no platform to put in the report — the card degrades to the error and the
    // help link, which is exactly what §42's `troubleReport` is built to do.
    // It still beats what was here (one red line naming an endpoint), because
    // the user reading it has an unusable app and no idea whether to reinstall,
    // restart, or ask someone.
    root.render(
      <div className="trouble-page">
        <TroubleCard
          what="loading the app's configuration at startup (GET /api/config)"
          error={String(err.message || err)}
          facts={{ page: location.pathname + location.search }}
          onRetry={() => location.reload()}
        />
      </div>
    )
);
