// One grid cell: the frame (title, "See all", and in edit mode the toolbar)
// around a body chosen by source.
import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent, type ReactNode } from "react";
import { tabHref } from "@apps/ai_models/routes";
import { softNavigate } from "./strip";
import { SIZE_LABELS, SOURCES, type Widget as WidgetModel, type WidgetFormat, type WidgetSize } from "./layout";
import { AppsWidget } from "./widgets/AppsWidget";
import { PlaygroundWidget, RecentsWidget, SessionsWidget } from "./widgets/StripWidgets";
import { TasksWidget } from "./widgets/TasksWidget";
import { BotsWidget } from "./widgets/BotsWidget";
import { FolderWidget, useWidgetFolder } from "./widgets/FolderWidget";
import { IndexWidget } from "./widgets/IndexWidget";

const SEE_ALL: Partial<Record<WidgetModel["source"], string>> = {
  apps: "/apps",
  playground: tabHref("playground", ""),
  sessions: "/explorer?tab=sessions",
  recents: "/explorer?tab=recents",
  tasks: "/tasks",
  bots: "/bots",
};

const FORMAT_LABELS: Record<WidgetFormat, string> = {
  cards: "Cards",
  list: "List",
  icons: "Icons",
  board: "Board",
  count: "Count",
};

function Body({ widget }: { widget: WidgetModel }) {
  switch (widget.source) {
    case "apps":
      return <AppsWidget widget={widget} />;
    case "playground":
      return <PlaygroundWidget widget={widget} />;
    case "sessions":
      return <SessionsWidget widget={widget} />;
    case "recents":
      return <RecentsWidget widget={widget} />;
    case "tasks":
      return <TasksWidget widget={widget} />;
    case "bots":
      return <BotsWidget widget={widget} />;
    case "folder":
      return <FolderWidget widget={widget} />;
    case "index":
      return <IndexWidget widget={widget} />;
  }
}

function SizeMenu({
  widget,
  onResize,
}: {
  widget: WidgetModel;
  onResize: (size: WidgetSize) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const down = (e: PointerEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", down, true);
    document.addEventListener("keydown", key, true);
    return () => {
      document.removeEventListener("pointerdown", down, true);
      document.removeEventListener("keydown", key, true);
    };
  }, [open]);
  const sizes = SOURCES[widget.source].sizes;
  return (
    <span className="hw-menu-wrap" ref={root}>
      <button
        type="button"
        className="hw-chip"
        aria-haspopup="menu"
        aria-expanded={open}
        title="Widget size"
        onClick={() => setOpen((o) => !o)}
      >
        {SIZE_LABELS[widget.size]}
        <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {open ? (
        <div className="hw-menu" role="menu">
          {sizes.map((s) => (
            <button
              key={s}
              type="button"
              role="menuitemradio"
              aria-checked={s === widget.size}
              className={"hw-menu-item" + (s === widget.size ? " is-on" : "")}
              onClick={() => {
                onResize(s);
                setOpen(false);
              }}
            >
              {SIZE_LABELS[s]}
            </button>
          ))}
        </div>
      ) : null}
    </span>
  );
}

export interface WidgetFrameProps {
  widget: WidgetModel;
  edit: boolean;
  /** Anchor id for the welcome tour on the first widget of a source. */
  anchorId?: string;
  dragging: boolean;
  dropTarget: boolean;
  onResize: (size: WidgetSize) => void;
  onReformat: (format: WidgetFormat) => void;
  onRemove: () => void;
  onDragStart: (e: DragEvent) => void;
  onDragOver: (e: DragEvent) => void;
  onDrop: (e: DragEvent) => void;
  onDragEnd: () => void;
  onKeyDown: (e: KeyboardEvent) => void;
}

export function WidgetFrame(p: WidgetFrameProps): ReactNode {
  const { widget, edit } = p;
  const spec = SOURCES[widget.source];
  const folder = useWidgetFolder(widget);
  const title = widget.source === "folder" ? (folder?.name ?? spec.label) : spec.label;
  const seeAll = SEE_ALL[widget.source];
  return (
    <section
      id={p.anchorId}
      className={
        "hw-widget" +
        ` hw-size-${widget.size}` +
        (edit ? " is-edit" : "") +
        (p.dragging ? " is-dragging" : "") +
        (p.dropTarget ? " is-drop" : "")
      }
      data-wid={widget.id}
      draggable={edit}
      tabIndex={edit ? 0 : undefined}
      aria-label={edit ? `${title} widget. Alt plus arrow keys to move.` : undefined}
      onDragStart={edit ? p.onDragStart : undefined}
      onDragOver={edit ? p.onDragOver : undefined}
      onDrop={edit ? p.onDrop : undefined}
      onDragEnd={edit ? p.onDragEnd : undefined}
      onKeyDown={edit ? p.onKeyDown : undefined}
    >
      <div className="hw-head">
        {edit ? (
          <span className="hw-grip" aria-hidden="true" title="Drag to reorder">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
              <circle cx="9" cy="6" r="1.6" /><circle cx="15" cy="6" r="1.6" />
              <circle cx="9" cy="12" r="1.6" /><circle cx="15" cy="12" r="1.6" />
              <circle cx="9" cy="18" r="1.6" /><circle cx="15" cy="18" r="1.6" />
            </svg>
          </span>
        ) : null}
        <h2 className="hw-title">
          {seeAll && !edit ? (
            <a className="hw-title-link" href={seeAll} onClick={(e) => softNavigate(e, seeAll)}>
              {title}
            </a>
          ) : (
            title
          )}
        </h2>
        {edit ? (
          <div className="hw-tools">
            <SizeMenu widget={widget} onResize={p.onResize} />
            {spec.formats.length > 1 ? (
              <span className="hw-seg" role="radiogroup" aria-label="Format">
                {spec.formats.map((f) => (
                  <button
                    key={f}
                    type="button"
                    role="radio"
                    aria-checked={f === widget.format}
                    className={"hw-seg-btn" + (f === widget.format ? " is-on" : "")}
                    onClick={() => p.onReformat(f)}
                  >
                    {FORMAT_LABELS[f]}
                  </button>
                ))}
              </span>
            ) : null}
            <button type="button" className="hw-x" aria-label={`Remove ${title}`} title="Remove" onClick={p.onRemove}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true">
                <path d="M6 6l12 12M18 6L6 18" />
              </svg>
            </button>
          </div>
        ) : seeAll ? (
          <a className="home-sec-more" href={seeAll} onClick={(e) => softNavigate(e, seeAll)}>
            See all
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M9 6l6 6-6 6" />
            </svg>
          </a>
        ) : null}
      </div>
      <Body widget={widget} />
    </section>
  );
}
