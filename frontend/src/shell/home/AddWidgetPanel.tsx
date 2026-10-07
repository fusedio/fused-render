// Right-hand panel in edit mode: every source with its description and format
// choices. Choosing one appends a widget with that source's default size.
import { useEffect, useState } from "react";
import { useBookmarksVersion } from "@platform/lib/hooks";
import { isFolder, loadBookmarks, type BookmarkFolder, type BookmarkItem } from "@platform/lib/bookmarks";
import { FORMAT_LABELS, MAX_WIDGETS, SOURCES, type WidgetSource } from "./layout";
import type { HomeLayoutApi } from "./useHomeLayout";

function allFolders(items: BookmarkItem[], out: BookmarkFolder[] = []): BookmarkFolder[] {
  for (const it of items) {
    if (isFolder(it)) {
      out.push(it);
      allFolders(it.children, out);
    }
  }
  return out;
}

export function AddWidgetPanel({ api, onClose }: { api: HomeLayoutApi; onClose: () => void }) {
  useBookmarksVersion();
  const [pickingFolder, setPickingFolder] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, [onClose]);
  const full = api.layout.widgets.length >= MAX_WIDGETS;
  const folders = allFolders(loadBookmarks());
  const sources = Object.keys(SOURCES) as WidgetSource[];
  return (
    <aside className="hw-panel" aria-label="Add a widget">
      <div className="hw-panel-head">
        <h2 className="hw-panel-title">Add a widget</h2>
        <button type="button" className="hw-x" aria-label="Close panel" onClick={onClose}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>
      {full ? <p className="hw-panel-note">Home is full ({MAX_WIDGETS} widgets). Remove one to add another.</p> : null}
      <ul className="hw-panel-list">
        {sources.map((s) => {
          const spec = SOURCES[s];
          return (
            <li key={s} className="hw-panel-item">
              <div className="hw-panel-name">{spec.label}</div>
              <div className="hw-panel-desc">{spec.description}</div>
              {s === "folder" ? (
                pickingFolder ? (
                  folders.length ? (
                    <div className="hw-panel-folders">
                      {folders.map((f) => (
                        <button
                          key={f.id}
                          type="button"
                          className="hw-btn"
                          disabled={full}
                          onClick={() => {
                            api.add("folder", { folderId: f.id });
                            setPickingFolder(false);
                          }}
                        >
                          {f.name}
                        </button>
                      ))}
                    </div>
                  ) : (
                    <p className="hw-panel-note">You have no bookmark folders yet. Create one in the sidebar.</p>
                  )
                ) : (
                  <div className="hw-panel-formats">
                    <button type="button" className="hw-btn" disabled={full} onClick={() => setPickingFolder(true)}>
                      Choose folder…
                    </button>
                  </div>
                )
              ) : (
                <div className="hw-panel-formats">
                  {spec.formats.map((f) => (
                    <button
                      key={f}
                      type="button"
                      className="hw-btn"
                      disabled={full}
                      onClick={() => api.add(s, { format: f })}
                    >
                      {FORMAT_LABELS[f]}
                    </button>
                  ))}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      <div className="hw-panel-foot">
        {confirmReset ? (
          <span className="hw-confirm">
            Replace your layout with the default?
            <button
              type="button"
              className="hw-btn is-danger"
              onClick={() => {
                api.reset();
                setConfirmReset(false);
              }}
            >
              Reset
            </button>
            <button type="button" className="hw-btn" onClick={() => setConfirmReset(false)}>
              Cancel
            </button>
          </span>
        ) : (
          <button type="button" className="hw-link" onClick={() => setConfirmReset(true)}>
            Reset to default
          </button>
        )}
      </div>
    </aside>
  );
}
