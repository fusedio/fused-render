// File preview. Dispatch is exactly three-way (ARCHITECTURE §6):
//   1. stat.template != null -> render template in iframe (+_file on iframe URL)
//   2. .html/.htm             -> render the file itself in iframe
//   3. else                   -> fallback metadata card
// No other file-type logic lives in the shell.
import React, { useState } from "react";
import { rawUrl } from "../lib/api.js";
import { formatSize, formatMtime } from "../lib/format.js";

function Header({ fsPath, stat, children }) {
  return (
    <div className="preview-header">
      <h1 title={fsPath}>{stat.name}</h1>
      <div className="preview-actions">{children}</div>
    </div>
  );
}

function TemplatePreview({ fsPath, stat }) {
  // Target file rides on the iframe's own URL, not the shell URL — the shell
  // URL's pathname already names the file, so no ?_file= duplication there.
  const src = `/render?path=${encodeURIComponent(stat.template)}&_file=${encodeURIComponent(fsPath)}`;
  return (
    <>
      <Header fsPath={fsPath} stat={stat} />
      <div className="preview-body">
        <iframe src={src} />
      </div>
    </>
  );
}

function HtmlPreview({ fsPath, stat, config }) {
  // `_mode` is a reserved shell param (runtime already hides all `_`-prefixed
  // keys from fused.params). It rides the shell URL so the Rendered/Source
  // choice is bookmarkable: ?_mode=source opens straight into the source view.
  // Initial render honors the URL but must not rewrite it — only clicks do
  // (replaceState per the D8 no-history convention).
  const [mode, setModeState] = useState(() =>
    new URLSearchParams(location.search).get("_mode") === "source" ? "source" : "render"
  );

  const setMode = (next) => {
    if (next === mode) return;
    const params = new URLSearchParams(location.search);
    // Switching to render DELETES _mode (absent = default, keeps URLs clean);
    // switching to source sets it. All other query params are preserved.
    if (next === "source") params.set("_mode", "source");
    else params.delete("_mode");
    const search = params.toString();
    history.replaceState(null, "", location.pathname + (search ? "?" + search : ""));
    setModeState(next);
  };

  // Source view is the code template pointed at the HTML file — an editable
  // CodeMirror buffer, same as opening any .py/.js/etc. (_file rides on the
  // iframe URL, like TemplatePreview). config.source_template is the abs path
  // of the editable code template (from /api/config).
  const src =
    mode === "source"
      ? `/render?path=${encodeURIComponent(config.source_template)}&_file=${encodeURIComponent(fsPath)}`
      : `/render?path=${encodeURIComponent(fsPath)}`;

  return (
    <>
      <Header fsPath={fsPath} stat={stat}>
        <button className={mode === "render" ? "active" : ""} onClick={() => setMode("render")}>
          Rendered
        </button>
        <button className={mode === "source" ? "active" : ""} onClick={() => setMode("source")}>
          Source
        </button>
      </Header>
      <div className="preview-body">
        {/* key: switching modes replaces the iframe (fresh document), matching
            the vanilla shell's innerHTML swap. */}
        <iframe key={mode} src={src} />
      </div>
    </>
  );
}

function FallbackPreview({ fsPath, stat }) {
  return (
    <>
      <Header fsPath={fsPath} stat={stat} />
      <div className="preview-body">
        <div className="metadata-card">
          <dl>
            <dt>Name</dt>
            <dd>{stat.name}</dd>
            <dt>Path</dt>
            <dd>{fsPath}</dd>
            <dt>Size</dt>
            <dd>{formatSize(stat.size)}</dd>
            <dt>Modified</dt>
            <dd>{formatMtime(stat.mtime)}</dd>
          </dl>
          <a href={rawUrl(fsPath)} download={stat.name}>
            Download
          </a>
        </div>
      </div>
    </>
  );
}

export default function Preview({ fsPath, stat, config }) {
  const ext = fsPath.toLowerCase().split(".").pop();
  if (stat.template) return <TemplatePreview fsPath={fsPath} stat={stat} />;
  if (ext === "html" || ext === "htm") return <HtmlPreview fsPath={fsPath} stat={stat} config={config} />;
  return <FallbackPreview fsPath={fsPath} stat={stat} />;
}
