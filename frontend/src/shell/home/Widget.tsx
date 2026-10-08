// One grid cell: the frame (title, "See all", and in edit mode the Change / ×
// pill) around a body chosen by source.
import { useState, type CSSProperties, type ReactNode } from "react";
import { tabHref } from "@apps/ai_models/routes";
import { softNavigate } from "./strip";
import { SOURCES, type TileTarget, type WidgetSource, type Widget as WidgetModel } from "./layout";
import type { HomeLayoutApi } from "./useHomeLayout";
import { ChangeTile } from "./ChangeTile";
import { AppsWidget } from "./widgets/AppsWidget";
import { PlaygroundWidget, RecentsWidget, SessionsWidget } from "./widgets/StripWidgets";
import { TasksWidget } from "./widgets/TasksWidget";
import { BotsWidget } from "./widgets/BotsWidget";
import { FolderWidget, useWidgetFolder } from "./widgets/FolderWidget";
import { SearchWidget } from "./widgets/SearchWidget";
import { BuildWidget } from "./widgets/BuildWidget";
import { IndexWidget } from "./widgets/IndexWidget";
import { AppEmbedWidget, OpenAppLink, OpenPageLink, appName, pageTitle, useWidgetApp } from "./widgets/AppEmbedWidget";

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

export interface WidgetFrameProps {
  widget: WidgetModel;
  edit: boolean;
  /** Anchor id for the welcome tour on the first widget of a source. */
  anchorId?: string;
  /** Inline grid placement. Data, not state. */
  style?: CSSProperties;
  api: HomeLayoutApi;
  /** Rendered width in units (the narrow reflow clamps it). */
  cols: number;
  onRemove: () => void;
  /** Open the add sheet for a folder or page pick that targets this tile. */
  onRequestPanel: (target: TileTarget, source: WidgetSource) => void;
}

export function WidgetFrame(p: WidgetFrameProps): ReactNode {
  const { widget, edit } = p;
  const [changeOpen, setChangeOpen] = useState(false);
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
  // The search and build widgets ARE their box: no header, in edit mode too.
  const bare = widget.source === "search" || widget.source === "build";
  // Too narrow for the "Change" label: the pill shrinks to its icon.
  const compact = p.cols <= 2;
  return (
    <section
      id={p.anchorId}
      className={
        "hw-widget" +
        ` hw-size-${widget.size}` +
        (widget.source === "search" ? " is-search" : "") +
        (widget.source === "build" ? " is-build" : "") +
        (edit ? " is-edit" : "") +
        (edit && compact ? " is-compact" : "") +
        (widget.source === "app" ? " is-app" : "")
      }
      style={p.style}
      data-wid={widget.id}
      aria-label={edit ? `${title} widget` : undefined}
    >
      {bare ? null : <div className="hw-head">
        <h2 className="hw-title">
          {seeAll && !edit ? (
            <a className="hw-title-link" href={seeAll} onClick={(e) => softNavigate(e, seeAll)}>
              {title}
            </a>
          ) : (
            title
          )}
        </h2>
        {edit ? null : embedded ? (
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
      {edit ? (
        <span className={"hw-swap" + (bare ? " is-bare" : "")}>
          {bare ? null : (
            <button
              type="button"
              className={"hw-swap-change" + (changeOpen ? " is-open" : "")}
              aria-haspopup="dialog"
              aria-expanded={changeOpen}
              aria-label={compact ? `Change ${title}` : undefined}
              onClick={() => setChangeOpen((o) => !o)}
            >
              {compact ? "⇄" : "⇄ Change"}
            </button>
          )}
          <button type="button" className="hw-swap-x" aria-label={`Remove ${title}`} onClick={p.onRemove}>
            ×
          </button>
          {changeOpen ? (
            <ChangeTile
              api={p.api}
              target={{ kind: "swap", widget }}
              title={title}
              onClose={() => setChangeOpen(false)}
              onPick={(source) => {
                setChangeOpen(false);
                p.onRequestPanel({ kind: "swap", widget }, source);
              }}
            />
          ) : null}
        </span>
      ) : null}
    </section>
  );
}
