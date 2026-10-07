// One grid cell: the frame (title, "See all", and in edit mode the toolbar)
// around a body chosen by source.
import { ChevronDown } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type DragEvent, type KeyboardEvent, type ReactNode } from "react";
import { tabHref } from "@apps/ai_models/routes";
import { softNavigate } from "./strip";
import { SOURCES, type Widget as WidgetModel, type WidgetFormat, type WidgetSize } from "./layout";
import { AppsWidget } from "./widgets/AppsWidget";
import { PlaygroundWidget, RecentsWidget, SessionsWidget } from "./widgets/StripWidgets";
import { TasksWidget } from "./widgets/TasksWidget";
import { BotsWidget } from "./widgets/BotsWidget";
import { FolderWidget, useWidgetFolder } from "./widgets/FolderWidget";
import { SearchWidget } from "./widgets/SearchWidget";
import { BuildWidget } from "./widgets/BuildWidget";
import { IndexWidget } from "./widgets/IndexWidget";
import { AppEmbedWidget, OpenAppLink, OpenPageLink, appName, pageTitle, useWidgetApp } from "./widgets/AppEmbedWidget";
import { FormatPicks, SizeChips } from "./Pickers";

const SEE_ALL: Partial<Record<WidgetModel["source"], string>> = {
  apps: "/apps",
  playground: tabHref("playground", ""),
  sessions: "/explorer?tab=sessions",
  recents: "/explorer?tab=recents",
  tasks: "/tasks",
  bots: "/bots",
};

function Body({ widget, edit, onRemove }: { widget: WidgetModel; edit: boolean; onRemove: () => void }) {
  switch (widget.source) {
    case "search":
      return <SearchWidget edit={edit} size={widget.size} />;
    case "build":
      return <BuildWidget edit={edit} />;
    case "spacer":
      return edit ? <div className="hw-body hw-spacer-ph" aria-hidden="true">Empty</div> : null;
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
    case "app":
      return <AppEmbedWidget widget={widget} edit={edit} onRemove={onRemove} />;
  }
}

function EditPopover({
  widget,
  title,
  onResize,
  onReformat,
  onRemove,
}: {
  widget: WidgetModel;
  title: string;
  onResize: (size: WidgetSize) => void;
  onReformat: (format: WidgetFormat) => void;
  onRemove: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [alignLeft, setAlignLeft] = useState(false);
  const root = useRef<HTMLSpanElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const down = (e: PointerEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
        button.current?.focus();
      }
    };
    document.addEventListener("pointerdown", down, true);
    document.addEventListener("keydown", key, true);
    return () => {
      document.removeEventListener("pointerdown", down, true);
      document.removeEventListener("keydown", key, true);
    };
  }, [open]);
  // Right-aligned under the button; a widget near the left edge would push the
  // 360px card off-screen, so flip it to grow rightwards there. Re-measured
  // after a size pick, which moves the button.
  useLayoutEffect(() => {
    if (!open || !button.current) return;
    setAlignLeft(button.current.getBoundingClientRect().right - 360 < 8);
  }, [open, widget.size]);
  const spec = SOURCES[widget.source];
  return (
    <span className="hw-menu-wrap" ref={root}>
      <button
        ref={button}
        type="button"
        className={"hw-editbtn" + (open ? " is-open" : "")}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        Edit
        <ChevronDown aria-hidden="true" />
      </button>
      {open ? (
        <div
          className={"hw-pop" + (alignLeft ? " is-left" : "")}
          role="dialog"
          aria-label={`Edit ${title}`}
        >
          <div className="hw-label">Size</div>
          <SizeChips sizes={spec.sizes} value={widget.size} onChange={onResize} />
          {spec.formats.length > 1 ? (
            <>
              <div className="hw-label">Show as</div>
              <FormatPicks source={widget.source} formats={spec.formats} value={widget.format} onChange={onReformat} />
            </>
          ) : null}
          <div className="hw-pop-divider" />
          <button
            type="button"
            className="hw-pop-remove"
            onClick={() => {
              setOpen(false);
              onRemove();
            }}
          >
            Remove widget
          </button>
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
  const embedded = useWidgetApp(widget);
  const title =
    widget.source === "folder"
      ? (folder?.name ?? spec.label)
      : widget.source === "app"
        ? embedded
          ? appName(embedded)
          : widget.appPath
            ? pageTitle(widget.appPath)
            : spec.label
        : spec.label;
  const seeAll = SEE_ALL[widget.source];
  // The search and build widgets ARE their box, and a spacer is nothing at all: no header in view mode.
  const bare = (widget.source === "search" || widget.source === "build" || widget.source === "spacer") && !edit;
  return (
    <section
      id={p.anchorId}
      className={
        "hw-widget" +
        ` hw-size-${widget.size}` +
        (bare && widget.source === "search" ? " is-search" : "") +
        (bare && widget.source === "build" ? " is-build" : "") +
        (widget.source === "spacer" ? " is-spacer" : "") +
        (edit ? " is-edit" : "") +
        (widget.source === "app" ? " is-app" : "") +
        (p.dragging ? " is-dragging" : "") +
        (p.dropTarget ? " is-drop" : "")
      }
      data-wid={widget.id}
      aria-hidden={!edit && widget.source === "spacer" ? true : undefined}
      draggable={edit}
      tabIndex={edit ? 0 : undefined}
      aria-label={edit ? `${title} widget. Alt plus arrow keys to move.` : undefined}
      onDragStart={edit ? p.onDragStart : undefined}
      onDragOver={edit ? p.onDragOver : undefined}
      onDrop={edit ? p.onDrop : undefined}
      onDragEnd={edit ? p.onDragEnd : undefined}
      onKeyDown={edit ? p.onKeyDown : undefined}
    >
      {bare ? null : <div className="hw-head">
        {edit ? (
          <span className="hw-grip" aria-hidden="true" title="Drag to reorder">
            <svg viewBox="0 0 10 14" fill="currentColor" aria-hidden="true"><circle cx="3" cy="2.5" r="1.4"/><circle cx="7" cy="2.5" r="1.4"/><circle cx="3" cy="7" r="1.4"/><circle cx="7" cy="7" r="1.4"/><circle cx="3" cy="11.5" r="1.4"/><circle cx="7" cy="11.5" r="1.4"/></svg>
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
          <>
            <EditPopover widget={widget} title={title} onResize={p.onResize} onReformat={p.onReformat} onRemove={p.onRemove} />
            <button type="button" className="hw-remove" aria-label={`Remove ${title}`} onClick={p.onRemove}>
              ×
            </button>
          </>
        ) : embedded ? (
          <OpenAppLink app={embedded} />
        ) : widget.source === "app" && widget.appPath ? (
          <OpenPageLink path={widget.appPath} />
        ) : seeAll ? (
          <a className="home-sec-more" href={seeAll} onClick={(e) => softNavigate(e, seeAll)}>
            See all
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M9 6l6 6-6 6" />
            </svg>
          </a>
        ) : null}
      </div>}
      <Body widget={widget} edit={edit} onRemove={p.onRemove} />
    </section>
  );
}
