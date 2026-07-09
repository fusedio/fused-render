// Mode menu for pane/tab chrome (Panel's pane bar, Tabs' active tab): the
// trigger shows the pane's ACTIVE template-mode icon; clicking opens a
// dropdown of every available mode (icon + name) for the pane's live
// location. Selecting one rewrites the pane-local `_mode` (same
// default-deletes rule as Preview's setMode, PT-9) and hands the new query to
// the caller, which reloads its iframe imperatively (crumb-click discipline —
// no React re-render may touch a live iframe).
//
// Rendered entirely with spans, not buttons: in tab mode the trigger lives
// INSIDE the tab's <button>, and nested buttons are invalid HTML.
import { useEffect, useRef, useState, type MouseEvent } from "react";
import { statPath, type TemplateEntry } from "../lib/api";
import { templateModeIcon, modeTitle, KNOWN_SENTINEL_MODES } from "./ModeSwitcher";

// Split a pane query at its raw `_layout=(...)` span (kept byte-identical —
// it may contain literal `&`), so the head is plain params URLSearchParams
// can edit. Same discipline as Tabs' composeFolderTabsUrl.
function splitAtLayout(query: string): [string, string] {
  const s = (query || "").replace(/^\?/, "");
  const i = s.indexOf("_layout=(");
  if (i === -1) return [s, ""];
  return [s.slice(0, i).replace(/&$/, ""), s.slice(i)];
}

interface PaneModeMenuProps {
  path: string;
  query: string;
  // Receives the pane's new query (leading "?" or empty); the caller writes
  // iframe.src = embedSrc(path, query) itself — it owns the iframe ref.
  onNavigate: (query: string) => void;
}

export default function PaneModeMenu({ path, query, onNavigate }: PaneModeMenuProps) {
  const [templates, setTemplates] = useState<TemplateEntry[]>([]);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null); // non-null = open
  const rootRef = useRef<HTMLSpanElement | null>(null);

  // Re-stat on every path change (pane navigation) so the menu tracks the
  // live location's modes. Sentinel paths (/_panel, /_tab) and stat errors
  // yield no templates — the menu hides itself below.
  useEffect(() => {
    let stale = false;
    setTemplates([]);
    setPos(null);
    statPath(path)
      .then((s) => {
        // Same defensive sentinel filter as Preview's dispatch (PT-12): keep
        // resolved templates and the known sentinels (`_render`, `_listing`),
        // so a directory pane's menu offers the listing beside zarr/custom views.
        if (!stale) setTemplates(s.templates.filter((t) => t.path !== null || KNOWN_SENTINEL_MODES.has(t.mode)));
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [path]);

  // Close on outside pointerdown, or on window blur — a click landing in any
  // iframe never reaches this document, but it does blur the shell window.
  useEffect(() => {
    if (!pos) return;
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setPos(null);
    };
    const onBlur = () => setPos(null);
    document.addEventListener("pointerdown", onDown);
    window.addEventListener("blur", onBlur);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      window.removeEventListener("blur", onBlur);
    };
  }, [pos]);

  if (templates.length < 2) return null;

  const activeMode = new URLSearchParams(splitAtLayout(query)[0]).get("_mode");
  const active = templates.find((t) => t.mode === activeMode) || templates[0];

  const toggle = (e: MouseEvent) => {
    e.stopPropagation();
    if (pos) {
      setPos(null);
      return;
    }
    // position:fixed — .panel-pane clips overflow, so the dropdown can't be
    // absolutely positioned inside the bar. Clamped to the viewport's right edge.
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setPos({ top: r.bottom + 4, left: Math.max(0, Math.min(r.left, window.innerWidth - 150)) });
  };

  const select = (e: MouseEvent, mode: string) => {
    e.stopPropagation();
    setPos(null);
    if (mode === active.mode) return;
    const [head, tail] = splitAtLayout(query);
    const params = new URLSearchParams(head);
    // Selecting the default mode DELETES _mode (clean URLs, PT-9).
    if (mode === templates[0].mode) params.delete("_mode");
    else params.set("_mode", mode);
    const qs = params.toString();
    const q = qs + (tail ? (qs ? "&" : "") + tail : "");
    onNavigate(q ? "?" + q : "");
  };

  return (
    <span className="pane-mode-menu" ref={rootRef}>
      <span className="pane-mode-btn" title={"Mode: " + modeTitle(active.mode)} onClick={toggle}>
        {templateModeIcon(active)}
      </span>
      {pos && (
        <span className="pane-mode-dropdown" style={{ top: pos.top, left: pos.left }}>
          {templates.map((t) => (
            <span
              key={t.mode}
              className={"pane-mode-item" + (t.mode === active.mode ? " active" : "")}
              onClick={(e) => select(e, t.mode)}
            >
              {templateModeIcon(t)}
              <span>{modeTitle(t.mode)}</span>
            </span>
          ))}
        </span>
      )}
    </span>
  );
}
