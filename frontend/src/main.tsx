// Bootstrap: history wrapping, embed class, config load, React mount.
import { createRoot } from "react-dom/client";
import { IS_EMBED } from "./lib/router";
import { getConfig } from "./lib/api";
import { hydrateBookmarks, refreshBookmarks } from "./lib/bookmarks";
import { notifyBookmarksChanged } from "./lib/hooks";
import App from "./App";
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

const root = createRoot(document.getElementById("root")!);

getConfig().then(
  (config) => {
    root.render(<App config={config} />);
    // Load the bookmark cache from the server (async; renders empty first, then
    // the sidebar/breadcrumb re-read once it resolves). Independent of config —
    // fire after mount so a config failure still shows its error screen.
    hydrateBookmarks().then(notifyBookmarksChanged);
    // Poll every 30 s so another tab's/window's bookmark edits converge here
    // (D77). refreshBookmarks() re-renders only when the tree actually changed.
    const BOOKMARK_POLL_MS = 30_000;
    setInterval(() => {
      refreshBookmarks().then((changed) => changed && notifyBookmarksChanged());
    }, BOOKMARK_POLL_MS);
  },
  (err: Error) =>
    root.render(
      <div className="status-message error">Failed to load config: {String(err.message || err)}</div>
    )
);
