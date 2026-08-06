// The file explorer's homepage (/explorer): a hero that says what the
// explorer is for (same visual ethos as the /apps hero — brand row, headline,
// one accent moment) over card grids for the two things worth jumping to:
// bookmarks and recent files. Entering any target navigates into
// /explorer/view/... (the explorer proper).
import { useLayoutEffect, useRef, useState } from "react";
import { navigate, navigateUrl } from "@platform/lib/router";
import { basename, timeAgo } from "@platform/lib/format";
import type { Config } from "@platform/lib/api";
import { allBookmarks, loadBookmarks } from "@platform/lib/bookmarks";
import { useBookmarksVersion } from "@platform/lib/hooks";
import { loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { BookmarkPreviewCard } from "@apps/explorer/BookmarkCards";
import { HeroBrand } from "@platform/ui/HeroBrand";

// How many recent files the list shows. The sidebar shows a tight top-3; the
// homepage list stays short too — a jump-off point, not a history browser.
const MAX_RECENTS = 5;

// How many bookmark-grid rows show before the "Show more" fold.
const BOOKMARK_ROWS = 2;

// The bookmark grid's live column count, so the two-row fold shows exactly
// two full rows at any viewport width. Measured from the grid's resolved
// template (auto-fill decides the count) and re-measured on resize.
function useGridColumns(ref: React.RefObject<HTMLDivElement | null>, mounted: boolean): number {
  const [cols, setCols] = useState(1);
  // `mounted` (the grid renders only when there are bookmarks) re-runs the
  // effect when the grid appears — the ref object itself never changes.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () =>
      setCols(getComputedStyle(el).gridTemplateColumns.split(" ").length || 1);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, mounted]);
  return cols;
}

// One recents row: name, path, last-opened stamp. An anchor so middle-click /
// Cmd-click open a new tab (same rationale as app cards).
function RecentRow({
  href,
  name,
  path,
  openedAt,
  onOpen,
}: {
  href: string;
  name: string;
  path: string;
  openedAt: string;
  onOpen: () => void;
}) {
  const when = timeAgo(Date.parse(openedAt) / 1000);
  return (
    <a
      className="fh-recent"
      href={href}
      title={path}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        onOpen();
      }}
    >
      <span className="fh-recent-name">{name}</span>
      <span className="fh-recent-path">{path}</span>
      {when && <span className="fh-recent-when">{when}</span>}
    </a>
  );
}

export default function FilesHome({ config }: { config: Config }) {
  useBookmarksVersion();
  useRecentsVersion();
  // Folders flattened: the homepage is a launcher, and a bookmark buried two
  // folders deep is still one the user cared enough to save.
  const bookmarks = loadBookmarks().length ? allBookmarks() : [];
  // Bookmarks fold at two grid rows; "Show more" renders the rest in place
  // (recents just move down) and flips to "Show less" to collapse again.
  const [expanded, setExpanded] = useState(false);
  const gridRef = useRef<HTMLDivElement | null>(null);
  const cols = useGridColumns(gridRef, bookmarks.length > 0);
  const fold = cols * BOOKMARK_ROWS;
  const shownBookmarks = expanded ? bookmarks : bookmarks.slice(0, fold);
  // Raw MRU, not the sidebar's stable-slot top-3 — a full page doesn't jump
  // under the pointer the way a always-visible sidebar section does.
  const recents = loadRecents().entries.slice(0, MAX_RECENTS);

  return (
    <div className="files-home">
      <div className="files-home-inner">
        <header className="home-hero files-hero">
          <HeroBrand name="Fused Explorer" />
          <h1 className="home-hero-title">
            Browse and preview <span className="home-hero-accent">your files</span>
          </h1>
          <p className="files-hero-sub">
            Open anything in your workspace — data, maps, images, notebooks. Preview it
            instantly, split views side by side, and bookmark the places you keep coming
            back to.
          </p>
          {config.fused_dir && (
            <button
              type="button"
              className="files-hero-cta"
              onClick={() => navigate(config.fused_dir!, { isDir: true })}
            >
              Browse workspace
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M5 12h14M13 6l6 6-6 6" />
              </svg>
            </button>
          )}
        </header>

        <section className="fh-section">
          <h2 className="fh-heading">Bookmarks</h2>
          {bookmarks.length ? (
            <>
              <div className="fhb-grid" ref={gridRef}>
                {shownBookmarks.map((b) => (
                  <BookmarkPreviewCard key={b.id} b={b} />
                ))}
              </div>
              {bookmarks.length > fold && (
                <button type="button" className="fhb-more" onClick={() => setExpanded((v) => !v)}>
                  {expanded ? "Show less" : "Show more"}
                  <svg
                    width="12"
                    height="12"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                    style={expanded ? { transform: "rotate(180deg)" } : undefined}
                  >
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </button>
              )}
            </>
          ) : (
            <p className="fh-empty">
              No bookmarks yet. While browsing, use the star in the breadcrumb to save a
              spot — it'll show up here.
            </p>
          )}
        </section>

        <section className="fh-section">
          <h2 className="fh-heading">Recent files</h2>
          {recents.length ? (
            <div className="fh-recents">
              {recents.map((r) => {
                const fsPath = recentFsPath(r.url);
                const name = r.title || basename(fsPath);
                return (
                  <RecentRow
                    key={fsPath}
                    href={r.url}
                    name={name}
                    path={fsPath}
                    openedAt={r.openedAt}
                    onOpen={() => navigateUrl(r.url)}
                  />
                );
              })}
            </div>
          ) : (
            <p className="fh-empty">Nothing opened yet. Files you view will show up here.</p>
          )}
        </section>
      </div>
    </div>
  );
}
