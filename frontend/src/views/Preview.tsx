// File preview. Dispatch is exactly two-way (ARCHITECTURE §6):
//   1. stat.templates non-empty -> render active mode in iframe (+_file on iframe URL)
//   2. else                      -> fallback metadata card
// No file-type checks live in the shell — html arrives through stat.templates
// like everything else, via the "_render" sentinel (SPEC PT-12).
import { useState, type ReactNode } from "react";
import { rawUrl } from "../lib/api";
import type { StatResult, TemplateEntry } from "../lib/api";
import { formatSize, formatMtime } from "../lib/format";
import ModeSwitcher, { templateModeIcon } from "../components/ModeSwitcher";
import { useUrlVersion } from "../lib/hooks";
import Listing from "./Listing";

// Sentinel modes the shell knows how to render without a template folder
// (mirrors the server's KNOWN_SENTINELS, SPEC PT-12/D78): `_render` (the file
// itself in an iframe) and `_listing` (the shell's built-in directory listing,
// no iframe). Any other path-null entry is an unknown sentinel, filtered out.
const KNOWN_SENTINEL_MODES = new Set(["_render", "_listing"]);

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

// --- Annotate toggle (SPEC §17, AN-1..AN-4) ---------------------------------
// Annotate is ORTHOGONAL to `_mode` (which belongs to template-mode selection,
// PT-9): reserved `_annotate=1` shell param, bookmarkable, deleted when off.
// It overlays whichever template mode is active — the active mode's iframe is
// re-rendered with `_annotate=1` appended, which makes the server inject the
// overlay script (AN-4).

const ANNOTATE_ICON = (
  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
  </svg>
);

// Count of OPEN comments in the current shell URL's `_comments` param (AN-3).
// `_comments` holds a JSON array of thread objects (AN-5); URLSearchParams
// already percent-decodes, so the value parses directly. The badge counts
// threads whose status is not "resolved" (default status is open).
function openCommentCount(): number {
  const raw = new URLSearchParams(location.search).get("_comments");
  if (!raw) return 0;
  try {
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return 0;
    return arr.filter((t) => t && t.status !== "resolved").length;
  } catch {
    return 0;
  }
}

// URL-backed annotate state: the URL is the ONLY source of truth (no mirrored
// useState) because the overlay itself can turn annotate off — Escape inside
// the iframe deletes `_annotate` on this shell URL (AN-12) and fires
// fused:urlchange, which useUrlVersion turns into a re-render here. Toggle
// clicks write with the same replaceState discipline as setMode.
function useAnnotate(): [boolean, () => void] {
  useUrlVersion();
  const on = new URLSearchParams(location.search).get("_annotate") === "1";
  const toggle = async () => {
    if (!on) {
      // Entering annotate REMOUNTS the preview iframe (React key change) —
      // an editor buffer with edits newer than the last autosave would be
      // silently discarded. Same-origin, so ask the iframe to flush first
      // (code template exposes __fusedFlushEdits); refuse the switch when the
      // buffer can't be made safe (save failure / unresolved conflict — the
      // template's own banner explains). The 10s bound only catches a truly
      // hung write (localhost saves are near-instant) so the toggle can't
      // wedge forever; timing out aborts the switch, never the save.
      const frame = document.querySelector<HTMLIFrameElement>(".preview-body iframe");
      const flush = frame?.contentWindow && (frame.contentWindow as any).__fusedFlushEdits;
      if (typeof flush === "function") {
        try {
          const res = await Promise.race([
            flush(),
            new Promise((r) => setTimeout(() => r({ ok: false }), 10000)),
          ]);
          if (res && (res as { ok: boolean }).ok === false) return;
        } catch {
          return;
        }
      }
    }
    const params = new URLSearchParams(location.search);
    if (!on) params.set("_annotate", "1");
    else params.delete("_annotate");
    const search = params.toString();
    history.replaceState(null, "", location.pathname + (search ? "?" + search : ""));
  };
  return [on, toggle];
}

// Comment-bubble toggle button in the icon-button family of ModeSwitcher (AN-2)
// with the open-comment badge (AN-3). The badge recomputes on every URL change
// — comments written inside the iframe surface via replaceState +
// fused:urlchange (useUrlVersion).
function AnnotateToggle({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  useUrlVersion();
  const count = openCommentCount();
  return (
    <button
      type="button"
      className={"mode-switcher-btn annotate-toggle" + (on ? " active" : "")}
      title="Annotate"
      onClick={onToggle}
    >
      {ANNOTATE_ICON}
      {count > 0 && <span className="annotate-badge">{count}</span>}
    </button>
  );
}

function TemplatePreview({ fsPath, stat, templates }: { fsPath: string; stat: StatResult; templates: TemplateEntry[] }) {
  // Caller only renders this when `templates` (already sentinel-filtered by
  // Preview's dispatch, SPEC PT-12) is non-empty.
  const [mode, setModeState] = useState<string>(() => activeTemplate(templates).mode);
  const entry = templates.find((t) => t.mode === mode) || templates[0];
  const [annotate, toggleAnnotate] = useAnnotate();
  // `_listing` sentinel (D78): the shell's built-in directory listing, mounted
  // in place of the preview iframe — no iframe, no `_file`; annotate (which
  // injects into the iframe) is not offered for it. Every directory renders
  // through this same header + body chrome (even a plain folder's single
  // `_listing` mode), so the preview header is uniform across files and dirs.
  const isListing = entry.mode === "_listing";

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
  // Annotate ON appends `_annotate=1` to the SAME src (AN-4) — the server then
  // injects the overlay script alongside the runtime. `_listing` builds no src
  // (it renders a shell component, not an iframe).
  const src = isListing
    ? null
    : (entry.mode === "_render"
        ? `/render?path=${encodeURIComponent(fsPath)}`
        : `/render?path=${encodeURIComponent(entry.path as string)}&_file=${encodeURIComponent(fsPath)}`) +
      (annotate ? "&_annotate=1" : "");

  // Embed hides the whole preview-header, hence the switcher (shell.css). A
  // directory whose mode list carries `_listing` alongside a real preview (a
  // .zarr store, or a custom view + listing) surfaces a corner chip to toggle
  // into/out of the member listing (D78 — replaces the old `?listing=1`
  // "Browse contents"). Pointless when `_listing` is the sole/default-only
  // mode, so shown only when there is another mode to toggle back to.
  const toggleListing =
    templates.length > 1 && templates.some((t) => t.mode === "_listing")
      ? () => setMode(isListing ? templates[0].mode : "_listing")
      : null;

  return (
    <>
      <Header fsPath={fsPath} stat={stat}>
        <ModeSwitcher
          entries={templates.map((t) => ({ mode: t.mode, icon: templateModeIcon(t) }))}
          active={entry.mode}
          onSelect={setMode}
        />
        {!isListing && <AnnotateToggle on={annotate} onToggle={toggleAnnotate} />}
      </Header>
      <div className="preview-body">
        {isListing ? (
          <Listing fsPath={fsPath} />
        ) : (
          /* key: switching mode or annotate replaces the iframe (fresh document
             per switch). */
          <iframe key={mode + (annotate ? "+a" : "")} src={src as string} />
        )}
        {toggleListing && (
          <button type="button" className="preview-browse-chip" onClick={toggleListing}>
            {isListing ? "Back" : "Browse contents"}
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
  // a recognized sentinel (`_render`, `_listing`) is dropped. Filtering here
  // keeps the non-empty dispatch check honest (an all-unknown list falls back
  // instead of crashing TemplatePreview).
  const templates = stat.templates.filter((t) => t.path !== null || KNOWN_SENTINEL_MODES.has(t.mode));
  if (templates.length > 0) return <TemplatePreview fsPath={fsPath} stat={stat} templates={templates} />;
  return <FallbackPreview fsPath={fsPath} stat={stat} />;
}
