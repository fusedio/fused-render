// The workspace's tab strip (WorkspaceHost): Home first — its icon and name,
// no close, never moved — then one tab per page, each its page's icon, its
// label and a ×. Presentational: what activating or closing means for history,
// the URL and the frames is the host's; this only reports gestures.
import { useState, type ReactNode } from "react";
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
  return (
    <div className="ws-strip" role="tablist" aria-label="Pages">
      {tabs.map((t) => (
        <div
          key={t.id}
          role="tab"
          aria-selected={t.active}
          tabIndex={0}
          title={t.label}
          className={"ws-tab" + (t.active ? " is-active" : "") + (t.home ? " is-home" : "")}
          onClick={() => onSelect(t.id)}
          onKeyDown={(e) => {
            if (e.target !== e.currentTarget) return;
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onSelect(t.id);
            }
          }}
        >
          <TabIcon tab={t} />
          <span className="ws-tab-label">{t.label}</span>
          {!t.home && (
            <button
              type="button"
              className="ws-tab-close"
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
