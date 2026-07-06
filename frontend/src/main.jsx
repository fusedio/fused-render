// Bootstrap: history wrapping, embed class, config load, React mount.
import React from "react";
import { createRoot } from "react-dom/client";
import { IS_EMBED } from "./lib/router.js";
import { getConfig } from "./lib/api.js";
import App from "./App.jsx";
import "./shell.css";

// The preview iframe's injected runtime writes view params via
// parent.history.replaceState (same history object), which fires no event.
// Wrapping replaceState is the shell's only way to observe those param
// changes so the "Update bookmark" button can react to them. pushState is
// wrapped the same way so in-pane navigation is observable too — the layout
// view's runtime target dispatches fused:urlchange through both (LM-8, D46).
// Must happen before mount: pane runtimes may write at any time.
const origReplaceState = history.replaceState.bind(history);
history.replaceState = function (...args) {
  origReplaceState(...args);
  window.dispatchEvent(new Event("fused:urlchange"));
};
const origPushState = history.pushState.bind(history);
history.pushState = function (...args) {
  origPushState(...args);
  window.dispatchEvent(new Event("fused:urlchange"));
};

if (IS_EMBED) document.body.classList.add("embed");

const root = createRoot(document.getElementById("root"));

getConfig().then(
  (config) => root.render(<App config={config} />),
  (err) =>
    root.render(
      <div className="status-message error">Failed to load config: {String(err.message || err)}</div>
    )
);
