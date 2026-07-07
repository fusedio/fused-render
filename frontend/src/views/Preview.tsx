// File preview. Dispatch is exactly two-way (ARCHITECTURE §6):
//   1. stat.templates non-empty -> render active mode in iframe (+_file on iframe URL)
//   2. else                      -> fallback metadata card
// No file-type checks live in the shell — html arrives through stat.templates
// like everything else, via the "_render" sentinel (SPEC PT-12).
import { useState, type ReactNode } from "react";
import { rawUrl } from "../lib/api";
import type { StatResult, TemplateEntry } from "../lib/api";
import { formatSize, formatMtime } from "../lib/format";
import { navigateUrl } from "../lib/router";
import ModeSwitcher, { templateModeIcon } from "../components/ModeSwitcher";

// Directory previews (a `.zarr` store maps to a directory template, D64) keep
// a way into the raw members: navigate to the same path with `?listing=1`,
// which App's dispatch honors to force the plain listing view. The pathname
// (which already carries the /view/ or /embed/ prefix) is preserved.
function browseContents() {
  navigateUrl(location.pathname + "?listing=1");
}

interface HeaderProps {
  fsPath: string;
  stat: StatResult;
  children?: ReactNode;
}

function Header({ fsPath, stat, children }: HeaderProps) {
  return (
    <div className="preview-header">
      <h1 title={fsPath}>{stat.name}</h1>
      <div className="preview-actions">{children}</div>
    </div>
  );
}

// `_mode` (shell URL) selects among stat.templates by name (SPEC PT-9): absent
// or unknown/stale value falls back to the default (templates[0]) silently.
function activeTemplate(templates: TemplateEntry[]): TemplateEntry {
  const requested = new URLSearchParams(location.search).get("_mode");
  return templates.find((t) => t.mode === requested) || templates[0];
}

function TemplatePreview({ fsPath, stat, templates }: { fsPath: string; stat: StatResult; templates: TemplateEntry[] }) {
  // Caller only renders this when `templates` (already sentinel-filtered by
  // Preview's dispatch, SPEC PT-12) is non-empty.
  const [mode, setModeState] = useState<string>(() => activeTemplate(templates).mode);
  const entry = templates.find((t) => t.mode === mode) || templates[0];

  const setMode = (next: string) => {
    if (next === mode) return;
    const params = new URLSearchParams(location.search);
    // Selecting the default mode DELETES _mode (clean URLs); any other mode sets it.
    if (next === templates[0].mode) params.delete("_mode");
    else params.set("_mode", next);
    const search = params.toString();
    history.replaceState(null, "", location.pathname + (search ? "?" + search : ""));
    setModeState(next);
  };

  // "_render" sentinel (PT-12): render the target file itself, no _file param.
  // Ordinary entries: target file rides on the iframe's own URL as _file —
  // the shell URL's pathname already names the file, so no duplication there.
  const src =
    entry.mode === "_render"
      ? `/render?path=${encodeURIComponent(fsPath)}`
      : `/render?path=${encodeURIComponent(entry.path as string)}&_file=${encodeURIComponent(fsPath)}`;

  return (
    <>
      <Header fsPath={fsPath} stat={stat}>
        {/* Directory template (e.g. a .zarr store): a "Browse contents" action
            drops into the raw member listing (D64). */}
        {stat.is_dir && (
          <button type="button" onClick={browseContents}>
            Browse contents
          </button>
        )}
        <ModeSwitcher
          entries={templates.map((t) => ({ mode: t.mode, icon: templateModeIcon(t) }))}
          active={entry.mode}
          onSelect={setMode}
        />
      </Header>
      <div className="preview-body">
        {/* key: switching modes replaces the iframe (fresh document per switch). */}
        <iframe key={mode} src={src} />
        {/* Embed mode hides the whole preview-header (shell.css), so a directory
            template also surfaces "Browse contents" as a corner chip pinned over
            the iframe — CSS reveals it only in embed. File previews render no
            chip, so embed chrome stays empty for them. */}
        {stat.is_dir && (
          <button type="button" className="preview-browse-chip" onClick={browseContents}>
            Browse contents
          </button>
        )}
      </div>
    </>
  );
}

function FallbackPreview({ fsPath, stat }: { fsPath: string; stat: StatResult }) {
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

interface PreviewProps {
  fsPath: string;
  stat: StatResult;
}

export default function Preview({ fsPath, stat }: PreviewProps) {
  // Defensive filter (SPEC PT-12): an entry with path===null whose mode isn't
  // a recognized sentinel is dropped — only "_render" exists today. Filtering
  // here keeps the non-empty dispatch check honest (an all-unknown list falls
  // back instead of crashing TemplatePreview).
  const templates = stat.templates.filter((t) => t.path !== null || t.mode === "_render");
  if (templates.length > 0) return <TemplatePreview fsPath={fsPath} stat={stat} templates={templates} />;
  return <FallbackPreview fsPath={fsPath} stat={stat} />;
}
