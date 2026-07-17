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
import { resolveConditions, statPath, type TemplateEntry } from "../lib/api";
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
  // Deferred condition.py verdicts (CT-12): null while any gated entry is
  // unresolved. The resolveConditions call is shared with Preview's (one
  // in-flight request per path), so this costs no extra gate evaluation.
  const [conditions, setConditions] = useState<Record<string, boolean> | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null); // non-null = open
  const rootRef = useRef<HTMLSpanElement | null>(null);

  // Re-stat on every path change (pane navigation) so the menu tracks the
  // live location's modes. Sentinel paths (/_panel, /_tab) and stat errors
  // yield no templates — the menu hides itself below.
  useEffect(() => {
    let stale = false;
    setTemplates([]);
    setConditions(null);
    setPos(null);
    statPath(path)
      .then((s) => {
        // Same defensive sentinel filter as Preview's dispatch (PT-12): keep
        // resolved templates and the known sentinels (`_render`, `_listing`),
        // so a directory pane's menu offers the listing beside zarr/custom views.
        if (stale) return;
        const filtered = s.templates.filter(
          (t) => t.path !== null || KNOWN_SENTINEL_MODES.has(t.mode),
        );
        setTemplates(filtered);
        if (!filtered.some((t) => t.conditional)) {
          setConditions({});
          return;
        }
        resolveConditions(path)
          .then((r) => {
            if (!stale) setConditions(r.conditions);
          })
          .catch(() => {
            // Fail closed, like a broken gate: every gated entry reads denied.
            if (!stale) setConditions({});
          });
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

  // Pending = gated, verdict still in flight (shown as a disabled spinner);
  // once verdicts land, denied entries drop from the menu entirely. The
  // default (and the trigger's fallback) is the first UNCONDITIONAL entry —
  // a gated template is never the default while a normal one exists (CT-12).
  const isPending = (t: TemplateEntry) => !!t.conditional && conditions === null;
  const visible = templates.filter(
    (t) => !t.conditional || conditions === null || conditions[t.mode] === true,
  );
  if (visible.length < 2) return null;

  const defaultEntry = visible.find((t) => !t.conditional) || visible[0];
  const activeMode = new URLSearchParams(splitAtLayout(query)[0]).get("_mode");
  const active = visible.find((t) => t.mode === activeMode && !isPending(t)) || defaultEntry;

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
    if (mode === defaultEntry.mode) params.delete("_mode");
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
          {visible.map((t) => (
            <span
              key={t.mode}
              className={
                "pane-mode-item" +
                (t.mode === active.mode ? " active" : "") +
                (isPending(t) ? " pending" : "")
              }
              title={isPending(t) ? "Checking if this view applies…" : undefined}
              onClick={(e) => {
                if (!isPending(t)) select(e, t.mode);
                else e.stopPropagation();
              }}
            >
              {isPending(t) ? <span className="mode-icon-spinner" /> : templateModeIcon(t)}
              <span>{modeTitle(t.mode)}</span>
            </span>
          ))}
        </span>
      )}
    </span>
  );
}
