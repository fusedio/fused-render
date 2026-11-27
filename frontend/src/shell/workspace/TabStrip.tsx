// The workspace's tab strip (WorkspaceHost): Home first — its icon and name,
// no close, never moved — then one tab per page, each its page's icon, its
// label and a ×. Presentational: what activating or closing means for history,
// the URL and the frames is the host's; this only reports gestures.
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { AppWindow, Bot, FileText, Folder, ListTodo, Settings, Store, X } from "lucide-react";
import { CanvasGlyph } from "@apps/canvases/glyph";
import type { TabKind } from "./workspace-lib";

const SVG = {
  width: 14,
  height: 14,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
  className: "ws-tab-icon",
} as const;
const LUCIDE = { size: 14, strokeWidth: 2, "aria-hidden": true, className: "ws-tab-icon" } as const;

// ONE ICON PER KIND OF PAGE, and where the left sidebar has an entry for that
// page it is the sidebar's own drawing (GlobalSidebar HOME_ICON,
// AI_MODELS_ICON, MARKETPLACE_ICON, CANVASES_ICON): a page reads the same in
// the strip as in the list it was opened from.
const KIND_ICON: Record<TabKind, ReactNode> = {
  home: (
    <svg {...SVG} strokeWidth="2">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5 9.5V21h14V9.5" />
      <path d="M10 21v-6h4v6" />
    </svg>
  ),
  models: (
    <svg {...SVG} strokeWidth="1.8">
      <rect x="5" y="5" width="14" height="14" rx="3" />
      <rect x="9" y="9" width="6" height="6" rx="1" />
      <path d="M8.5 5V2.5M12 5V2.5M15.5 5V2.5M8.5 19v2.5M12 19v2.5M15.5 19v2.5M5 8.5H2.5M5 12H2.5M5 15.5H2.5M19 8.5h2.5M19 12h2.5M19 15.5h2.5" />
    </svg>
  ),
  community: <Store {...LUCIDE} strokeWidth={1.9} />,
  canvas: (
    <span className="ws-tab-icon">
      <CanvasGlyph size={14} />
    </span>
  ),
  folder: <Folder {...LUCIDE} />,
  file: <FileText {...LUCIDE} />,
  app: <AppWindow {...LUCIDE} />,
  tasks: <ListTodo {...LUCIDE} />,
  bots: <Bot {...LUCIDE} />,
  settings: <Settings {...LUCIDE} />,
  page: <FileText {...LUCIDE} />,
};

export interface StripTab {
  id: string;
  label: string;
  kind: TabKind;
  // The page's OWN icon (an app's), or null: the kind's glyph then.
  icon: string | null;
  active: boolean;
  home: boolean;
}

// A page icon that fails to load falls back to the kind's glyph rather than a
// broken image; remembered per href so a re-render does not retry it.
function TabIcon({ tab }: { tab: StripTab }) {
  const [broken, setBroken] = useState<string | null>(null);
  if (tab.home || !tab.icon || broken === tab.icon) return <>{KIND_ICON[tab.home ? "home" : tab.kind]}</>;
  const src = tab.icon;
  return <img src={src} alt="" aria-hidden="true" className="ws-tab-icon" onError={() => setBroken(src)} />;
}

export default function TabStrip({
  tabs,
  onSelect,
  onClose,
}: {
  tabs: StripTab[];
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
}) {
  const strip = useRef<HTMLDivElement | null>(null);
  const tabEls = useRef(new Map<string, HTMLDivElement>()).current;
  const activeId = tabs.find((t) => t.active)?.id ?? null;

  // MORE TABS THAN FIT: the strip scrolls sideways with no bar, so its edges
  // fade where tabs lie beyond them (workspace.css reads the two attributes) —
  // the only word that there is more.
  const markEdges = useCallback(() => {
    const el = strip.current;
    if (!el) return;
    el.toggleAttribute("data-more-left", el.scrollLeft > 1);
    el.toggleAttribute("data-more-right", el.scrollLeft + el.clientWidth < el.scrollWidth - 1);
  }, []);

  // THE SHOWN TAB IS ON SCREEN. A tab opened past the strip's edge, or
  // switched to from the sidebar while the strip was scrolled elsewhere, was
  // active and invisible. Layout phase, so it never paints off screen; by
  // hand, not scrollIntoView, which may scroll an ancestor too.
  const reveal = useCallback(
    (id: string) => {
      const el = strip.current;
      const tab = tabEls.get(id);
      if (!el || !tab) return;
      const s = el.getBoundingClientRect();
      const t = tab.getBoundingClientRect();
      // Clear of the edge's fade (workspace.css, 28px), not merely inside.
      if (t.left < s.left) el.scrollLeft -= s.left - t.left + 32;
      else if (t.right > s.right) el.scrollLeft += t.right - s.right + 32;
    },
    [tabEls],
  );
  useLayoutEffect(() => {
    if (activeId) reveal(activeId);
    markEdges();
  }, [activeId, tabs.length, reveal, markEdges]);

  // …and when the STRIP changes width (the right panel dragged wider, the
  // window narrowed): the shown tab could end up past its edge with nothing
  // having been opened or switched.
  const activeRef = useRef(activeId);
  activeRef.current = activeId;
  useEffect(() => {
    const el = strip.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (activeRef.current) reveal(activeRef.current);
      markEdges();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [markEdges, reveal]);

  // ONE TAB STOP for the whole strip (the shown tab), and the arrows walk it —
  // the tablist pattern. Twelve tabs were twenty-four stops before (each tab
  // and each ×). Delete closes the focused tab; the × stays a pointer target.
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>, t: StripTab) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(t.id);
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && !t.home) {
      e.preventDefault();
      onClose(t.id);
      return;
    }
    const i = tabs.findIndex((x) => x.id === t.id);
    const to =
      e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : -1;
    if (to < 0 || to >= tabs.length || to === i) return;
    e.preventDefault();
    reveal(tabs[to].id);
    tabEls.get(tabs[to].id)?.focus({ preventScroll: true });
    markEdges();
  };

  return (
    <div ref={strip} className="ws-strip" role="tablist" aria-label="Pages" onScroll={markEdges}>
      {tabs.map((t) => (
        <div
          key={t.id}
          ref={(el) => {
            if (el) tabEls.set(t.id, el);
            else tabEls.delete(t.id);
          }}
          role="tab"
          aria-selected={t.active}
          tabIndex={t.active ? 0 : -1}
          title={t.label}
          className={"ws-tab" + (t.active ? " is-active" : "") + (t.home ? " is-home" : "")}
          onClick={() => onSelect(t.id)}
          onKeyDown={(e) => onKeyDown(e, t)}
        >
          <TabIcon tab={t} />
          <span className="ws-tab-label">{t.label}</span>
          {!t.home && (
            <button
              type="button"
              className="ws-tab-close"
              tabIndex={-1}
              aria-label={`Close ${t.label}`}
              title="Close tab"
              onClick={(e) => {
                e.stopPropagation();
                onClose(t.id);
              }}
            >
              <X size={12} strokeWidth={2} aria-hidden="true" />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}
