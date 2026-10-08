// The app's front door (/home): a configurable 4-column widget grid whose
// first widget is the file search. The default layout is the search bar plus
// the four strips this page used to hard-code — Fused Apps, AI Playground, Claude Sessions, Recent files —
// each a one-row "See all" strip; "Customize" turns on edit mode (pick a layout
// preset, change what a tile shows, add or remove widgets) and the layout is
// saved on the server (GET/PUT /api/home/layout).
//
// Lives in the shell layer on purpose: it composes builder cards
// (AppPreviewCard) with explorer cards and libs, which only the shell may
// import together (scripts/check-boundaries.mjs). The widgets themselves are
// in shell/home/.
import { useEffect, useRef, useState } from "react";
import { SlidersHorizontal } from "lucide-react";
import type { Config } from "@platform/lib/api";
import { useRecentsVersion } from "@apps/explorer/lib/recents";
import { ClaudeHealthStrip } from "@platform/ui/ClaudeHealthStrip";
import { FdaStrip } from "@platform/ui/FdaStrip";
import { loadBookmarks } from "@platform/lib/bookmarks";
import { AddWidgetPanel, allFolders } from "./home/AddWidgetPanel";
import { PRESETS, matchPreset, type HomeLayout, type TileTarget, type WidgetSource } from "./home/layout";
import { PresetThumb } from "./home/PresetThumb";
import { WidgetGrid } from "./home/WidgetGrid";
import { useHomeLayout } from "./home/useHomeLayout";
import { SearchHostContext, useSearchHost } from "./home/widgets/SearchWidget";

/** Files shows a bookmark folder when there is one to show. */
function firstBookmarkFolderId(): string | undefined {
  return allFolders(loadBookmarks())[0]?.id;
}

export default function Home({ config }: { config: Config }) {
  // Same normalization every other config.home consumer applies.
  const home = config.home.replace(/\\/g, "/");
  useRecentsVersion();

  const layoutApi = useHomeLayout();
  const [edit, setEdit] = useState(false);
  // The add sheet: bare ("+ Add widget"), or aimed at one tile or slot (a
  // folder or page picked from a Change popover).
  const [panel, setPanel] = useState<null | { target?: TileTarget; initialSource?: WidgetSource }>(null);
  // The layout before the last preset click, for Undo.
  const prevLayout = useRef<HomeLayout | null>(null);
  const [canUndo, setCanUndo] = useState(false);
  useEffect(() => {
    if (!edit) {
      setPanel(null);
      prevLayout.current = null;
      setCanUndo(false);
      return;
    }
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") setEdit(false);
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [edit]);

  // Search is a widget, but a live query takes over the page body — the same
  // posture as the explorer home. The state lives here so the takeover can hide
  // everything else; the widget draws the box (home/widgets/SearchWidget.tsx).
  const search = useSearchHost(home);
  const searching = search.searching;

  return (
    <div className="files-home">
      <div className="files-home-inner home-wide">
        <SearchHostContext.Provider value={search}>
          <div className="home-strips">
            {/* Above the grid, because on a machine where Claude Code is not
                set up this is the only thing on the page the user can act on —
                and it renders nothing at all once there is nothing to say.
                Hidden while a search is live for the same reason the grid is:
                the search result IS the page then. */}
            {searching ? null : <ClaudeHealthStrip />}
            {searching ? null : <FdaStrip />}
            {searching ? null : edit ? (
              <div className="hw-editbar">
                <span className="hw-editbar-label">Layout</span>
                <div className="hw-presets" role="radiogroup" aria-label="Layout">
                  {matchPreset(layoutApi.layout) === null ? (
                    <span className="hw-preset is-custom" aria-current="true">
                      Custom
                    </span>
                  ) : null}
                  {PRESETS.map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      role="radio"
                      aria-checked={matchPreset(layoutApi.layout) === p.id}
                      className="hw-preset"
                      title={p.blurb}
                      onClick={() => {
                        prevLayout.current = layoutApi.layout;
                        setCanUndo(true);
                        layoutApi.applyPreset(p.id, { folderId: firstBookmarkFolderId() });
                      }}
                    >
                      <PresetThumb id={p.id} />
                      {p.name}
                    </button>
                  ))}
                </div>
                {canUndo ? (
                  <button
                    type="button"
                    className="hw-tb is-ghost"
                    onClick={() => {
                      if (prevLayout.current) layoutApi.restore(prevLayout.current);
                      prevLayout.current = null;
                      setCanUndo(false);
                    }}
                  >
                    Undo
                  </button>
                ) : null}
                <span className="hw-editbar-sp" />
                <button
                  type="button"
                  className="hw-tb is-primary"
                  aria-pressed={edit}
                  onClick={() => setEdit(false)}
                >
                  Done
                </button>
              </div>
            ) : (
              <div className="hw-toolbar">
                <button
                  type="button"
                  className="hw-customize"
                  disabled={!layoutApi.loaded}
                  aria-pressed={false}
                  onClick={() => setEdit(true)}
                >
                  <SlidersHorizontal size={13} aria-hidden="true" />
                  Customize
                </button>
              </div>
            )}
            <div className="hw-stage">
              {layoutApi.loaded ? (
                <div className="hw-stage-main">
                  <WidgetGrid api={layoutApi} edit={edit} searching={searching} onAdd={() => setPanel({})}
                    onRequestPanel={(target, initialSource) => setPanel({ target, initialSource })}
                  />
                  {!layoutApi.layout.widgets.length && !edit && !searching ? (
                    <p className="fh-empty">Your Home is empty. Choose Customize to add widgets.</p>
                  ) : null}
                </div>
              ) : null}
              {edit && panel && !searching ? (
                <AddWidgetPanel api={layoutApi} target={panel.target} initialSource={panel.initialSource} onClose={() => setPanel(null)} />
              ) : null}
            </div>
          </div>
        </SearchHostContext.Provider>
      </div>
    </div>
  );
}
