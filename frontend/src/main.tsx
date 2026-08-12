// Bootstrap: history wrapping, embed class, config load, React mount.
import { createRoot } from "react-dom/client";
import { IS_EMBED, IS_SNAPSHOT } from "@platform/lib/router";
import { getConfig } from "@platform/lib/api";
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

if (IS_EMBED) document.body.classList.add("embed");
// Frozen-tree framing (router.ts IS_SNAPSHOT): a body class rather than props
// threaded through the view tree, exactly like `embed` above — every rule it
// drives is chrome, and chrome is what stylesheets already own.
if (IS_SNAPSHOT) document.body.classList.add("snapshot");

const root = createRoot(document.getElementById("root")!);

getConfig().then(
  (config) => {
    root.render(<App config={config} />);
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
    root.render(
      <div className="status-message error">Failed to load config: {String(err.message || err)}</div>
    )
);
