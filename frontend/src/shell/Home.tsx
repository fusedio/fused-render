// The app's front door (/home): the explorer's search hero over a configurable
// 4-column widget grid. The default layout is the four strips this page used
// to hard-code — Fused Apps, AI Playground, Claude Sessions, Recent files —
// each a one-row "See all" strip; "Customize" turns on edit mode (drag to
// reorder, resize, change format, add or remove widgets) and the layout is
// saved on the server (GET/PUT /api/home/layout).
//
// Lives in the shell layer on purpose: it composes builder cards
// (AppPreviewCard) with explorer cards and libs, which only the shell may
// import together (scripts/check-boundaries.mjs). The widgets themselves are
// in shell/home/.
import { useEffect, useRef, useState } from "react";
import type { Config } from "@platform/lib/api";
import { useIndexStatus } from "@platform/lib/index-status";
import { useRecentsVersion } from "@apps/explorer/lib/recents";
import { FilesSearch } from "@apps/explorer/FilesHome";
import { ClaudeHealthStrip } from "@platform/ui/ClaudeHealthStrip";
import { FdaStrip } from "@platform/ui/FdaStrip";
import { AddWidgetPanel } from "./home/AddWidgetPanel";
import { WidgetGrid } from "./home/WidgetGrid";
import { useHomeLayout } from "./home/useHomeLayout";

export default function Home({ config }: { config: Config }) {
  // Same normalization every other config.home consumer applies.
  const home = config.home.replace(/\\/g, "/");
  useRecentsVersion();

  const layoutApi = useHomeLayout();
  const [edit, setEdit] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  useEffect(() => {
    if (!edit) {
      setPanelOpen(false);
      setConfirmReset(false);
      return;
    }
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") setEdit(false);
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [edit]);

  // Search takes over the page body while a query is live — the same posture
  // as the explorer home. The index poll only runs while the box needs its
  // "indexing…" caveat.
  const [searching, setSearching] = useState(false);
  // Bumped when the box starts a scan, so the poll looks again immediately
  // instead of on its next idle beat (see FilesSearch's `onScanRequested`).
  const [indexNonce, setIndexNonce] = useState(0);
  const indexScan = useIndexStatus(searching, indexNonce);
  const initialQuery = useRef(new URLSearchParams(location.search).get("q") || "").current;

  return (
    <div className="files-home">
      <div className="files-home-inner home-wide">
        <header className="home-hero files-hero">
          <FilesSearch
            home={home}
            initialQuery={initialQuery}
            indexScan={indexScan}
            onActiveChange={setSearching}
            onScanRequested={() => setIndexNonce((n) => n + 1)}
          />
        </header>

        {searching ? null : (
          <div className="home-strips">
            {/* Above the grid, because on a machine where Claude Code is not
                set up this is the only thing on the page the user can act on —
                and it renders nothing at all once there is nothing to say.
                Hidden while a search is live for the same reason the grid is:
                the search result IS the page then. */}
            <ClaudeHealthStrip />
            <FdaStrip />
            {edit ? (
              <div className="hw-editbar">
                <span className="hw-editbar-hint">Editing Home · drag ⋮⋮ to rearrange</span>
                {confirmReset ? (
                  <span className="hw-confirm">
                    Replace your layout with the default?
                    <button
                      type="button"
                      className="hw-tb is-danger"
                      onClick={() => {
                        layoutApi.reset();
                        setConfirmReset(false);
                      }}
                    >
                      Reset
                    </button>
                    <button type="button" className="hw-tb is-ghost" onClick={() => setConfirmReset(false)}>
                      Cancel
                    </button>
                  </span>
                ) : (
                  <button type="button" className="hw-tb is-ghost" onClick={() => setConfirmReset(true)}>
                    Reset to default
                  </button>
                )}
                <button type="button" className="hw-tb" onClick={() => setPanelOpen(true)}>
                  + Add widget
                </button>
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
                  Customize
                </button>
              </div>
            )}
            <div className="hw-stage">
              {layoutApi.loaded ? (
                <div className="hw-stage-main">
                  <WidgetGrid api={layoutApi} edit={edit} onAdd={() => setPanelOpen(true)} />
                  {!layoutApi.layout.widgets.length && !edit ? (
                    <p className="fh-empty">Your Home is empty. Choose Customize to add widgets.</p>
                  ) : null}
                </div>
              ) : null}
              {edit && panelOpen ? <AddWidgetPanel api={layoutApi} onClose={() => setPanelOpen(false)} /> : null}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
