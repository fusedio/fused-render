// The "Add a widget" sheet: a centred modal with the sources on the left and,
// on the right, a large preview of the chosen look plus the format and size
// picks. "Add to Home" appends with the chosen format and size.
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { useBookmarksVersion } from "@platform/lib/hooks";
import { isFolder, loadBookmarks, type BookmarkFolder, type BookmarkItem } from "@platform/lib/bookmarks";
import { FormatPreview } from "./FormatPreview";
import { FormatPicks, SizeChips, stageZoom } from "./Pickers";
import { MAX_WIDGETS, SOURCES, type WidgetFormat, type WidgetSize, type WidgetSource } from "./layout";
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

const SOURCE_KEYS = Object.keys(SOURCES) as WidgetSource[];

export function AddWidgetPanel({ api, onClose }: { api: HomeLayoutApi; onClose: () => void }) {
  useBookmarksVersion();
  const dialog = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const [source, setSource] = useState<WidgetSource>(SOURCE_KEYS[0]);
  const [format, setFormat] = useState<WidgetFormat>(SOURCES[SOURCE_KEYS[0]].formats[0]);
  const [size, setSize] = useState<WidgetSize>(SOURCES[SOURCE_KEYS[0]].sizes[0]);
  const [folderId, setFolderId] = useState<string | null>(null);
  const folders = allFolders(loadBookmarks());
  const spec = SOURCES[source];
  const full = api.layout.widgets.length >= MAX_WIDGETS;
  const chosenFolder = folders.find((f) => f.id === folderId) ?? folders[0] ?? null;
  const noFolders = source === "folder" && !folders.length;

  const pick = (s: WidgetSource) => {
    setSource(s);
    setFormat(SOURCES[s].formats[0]);
    setSize(SOURCES[s].sizes[0]);
  };

  // Focus the dialog on open; hand focus back to whatever opened it on close.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => opener?.focus?.();
  }, []);

  useEffect(() => {
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, [onClose]);

  const onListKey = (e: KeyboardEvent) => {
    const d = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const next = SOURCE_KEYS[SOURCE_KEYS.indexOf(source) + d];
    if (!next) return;
    pick(next);
    list.current?.querySelector<HTMLElement>(`[data-source="${next}"]`)?.focus();
  };

  // Keep Tab inside the dialog.
  const onDialogKey = (e: KeyboardEvent) => {
    if (e.key !== "Tab" || !dialog.current) return;
    const items = Array.from(
      dialog.current.querySelectorAll<HTMLElement>("button:not(:disabled), [tabindex]:not([tabindex='-1'])"),
    );
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === dialog.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };

  const add = () => {
    if (full || noFolders) return;
    api.add(source, source === "folder" ? { folderId: chosenFolder?.id, format, size } : { format, size });
    onClose();
    // The new widget is the last one in the grid; wait a beat for it to mount.
    setTimeout(() => {
      const all = document.querySelectorAll(".hw-widget");
      all[all.length - 1]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }, 80);
  };

  return createPortal(
    <div className="hw-scrim" onPointerDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={dialog}
        className="hw-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="Add a widget"
        tabIndex={-1}
        onKeyDown={onDialogKey}
      >
        <div className="hw-sheet-body">
          <div className="hw-sheet-side" role="listbox" aria-label="Widget source" ref={list} onKeyDown={onListKey}>
            {SOURCE_KEYS.map((s) => (
              <button
                key={s}
                type="button"
                role="option"
                aria-selected={s === source}
                data-source={s}
                tabIndex={s === source ? 0 : -1}
                className={"hw-src" + (s === source ? " is-on" : "")}
                onClick={() => pick(s)}
              >
                <span className="hw-src-sq" />
                {SOURCES[s].label}
              </button>
            ))}
          </div>
          <div className="hw-sheet-main">
            <div>
              <h3 className="hw-sheet-title">{spec.label}</h3>
              <p className="hw-sheet-desc">{spec.description}</p>
            </div>
            {full ? <p className="hw-sheet-note">Home is full ({MAX_WIDGETS} widgets). Remove one to add another.</p> : null}
            {source === "folder" ? (
              <div className="hw-stage-box is-folders">
                {folders.length ? (
                  <div className="hw-folders" role="radiogroup" aria-label="Bookmark folder">
                    {folders.map((f) => (
                      <button
                        key={f.id}
                        type="button"
                        role="radio"
                        aria-checked={f.id === chosenFolder?.id}
                        className={"hw-folder" + (f.id === chosenFolder?.id ? " is-on" : "")}
                        onClick={() => setFolderId(f.id)}
                      >
                        {f.name}
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="hw-nofolders">
                    <span className="hw-src-sq is-lg" />
                    <b>No bookmark folders yet</b>
                    <span>Bookmark a folder from the file explorer, then come back to pin it here.</span>
                  </div>
                )}
              </div>
            ) : (
              <div className="hw-stage-box">
                <span className="hw-stage-in" style={{ zoom: stageZoom(source, format) }}>
                  <FormatPreview source={source} format={format} />
                </span>
              </div>
            )}
            <div className="hw-opts">
              {spec.formats.length > 1 ? (
                <div className="hw-opt">
                  <span className="hw-label">Show as</span>
                  <FormatPicks source={source} formats={spec.formats} value={format} onChange={setFormat} />
                </div>
              ) : null}
              <div className="hw-opt">
                <span className="hw-label">Size</span>
                <SizeChips sizes={spec.sizes} value={size} onChange={setSize} />
              </div>
            </div>
          </div>
        </div>
        <div className="hw-sheet-foot">
          <button type="button" className="hw-tb is-ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="hw-tb is-primary" disabled={full || noFolders} onClick={add}>
            Add to Home
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
