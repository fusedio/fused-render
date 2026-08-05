// The file explorer's homepage (/explorer): a hero that says what the
// explorer is for (same visual ethos as the /apps hero — brand row, headline,
// one accent moment) over card grids for the two things worth jumping to:
// bookmarks and recent files. Entering any target navigates into
// /explorer/view/... (the explorer proper).
import type { ReactNode } from "react";
import { navigate, navigateUrl } from "@platform/lib/router";
import { basename } from "@platform/lib/format";
import { iconForEntry } from "@platform/ui/FileIcons";
import type { Config } from "@platform/lib/api";
import { allBookmarks, armBookmark, loadBookmarks, type Bookmark } from "@platform/lib/bookmarks";
import { useBookmarksVersion } from "@platform/lib/hooks";
import { loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { bookmarkFsPath } from "@apps/explorer/sidebar/BookmarksSection";
import logoMark from "@assets/logo-black-bg-transparent.png";

// How many recent files earn a card. The sidebar shows a tight top-3; the
// homepage has room to be a real jump-off point.
const MAX_RECENT_CARDS = 8;

// A launcher card: icon tile + name + path. Shared shape for bookmarks,
// recents, and the workspace root so the grids read as one system. An anchor
// so middle-click / Cmd-click open a new tab (same rationale as app cards).
function LaunchCard({
  href,
  icon,
  name,
  path,
  onOpen,
}: {
  href: string;
  icon: ReactNode;
  name: string;
  path: string;
  onOpen: () => void;
}) {
  return (
    <a
      className="fh-card"
      href={href}
      title={path}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        onOpen();
      }}
    >
      <span className="fh-card-icon" aria-hidden="true">
        {icon}
      </span>
      <span className="fh-card-text">
        <span className="fh-card-name">{name}</span>
        <span className="fh-card-path">{path}</span>
      </span>
    </a>
  );
}

function BookmarkCard({ b }: { b: Bookmark }) {
  const fsPath = bookmarkFsPath(b.url);
  return (
    <LaunchCard
      href={b.url}
      // The bookmark's own emoji when it has one, else the star every
      // default bookmark row wears.
      icon={
        b.icon ? (
          <span className="fh-card-emoji">{b.icon}</span>
        ) : (
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
            <path d="M8 1.6l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.4l-3.8 2 .7-4.3-3.1-3 4.3-.6L8 1.6z" />
          </svg>
        )
      }
      name={b.name}
      path={fsPath}
      onOpen={() => {
        // Same as a sidebar bookmark click: arm the bookmark so the
        // breadcrumb's "Update bookmark" tracks param changes at the target.
        armBookmark(b.id, b.url);
        navigateUrl(b.url);
      }}
    />
  );
}

export default function FilesHome({ config }: { config: Config }) {
  useBookmarksVersion();
  useRecentsVersion();
  // Folders flattened: the homepage is a launcher, and a bookmark buried two
  // folders deep is still one the user cared enough to save.
  const bookmarks = loadBookmarks().length ? allBookmarks() : [];
  // Raw MRU, not the sidebar's stable-slot top-3 — a full page doesn't jump
  // under the pointer the way a always-visible sidebar section does.
  const recents = loadRecents().entries.slice(0, MAX_RECENT_CARDS);

  return (
    <div className="files-home">
      <div className="files-home-inner">
        <header className="home-hero files-hero">
          <div className="home-hero-brand">
            <img className="home-hero-logo" src={logoMark} alt="" aria-hidden="true" />
            <span className="home-hero-brand-name">Fused Explorer</span>
          </div>
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
            <div className="fh-grid">
              {bookmarks.map((b) => (
                <BookmarkCard key={b.id} b={b} />
              ))}
            </div>
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
            <div className="fh-grid">
              {recents.map((r) => {
                const fsPath = recentFsPath(r.url);
                const name = r.title || basename(fsPath);
                return (
                  <LaunchCard
                    key={fsPath}
                    href={r.url}
                    icon={iconForEntry(basename(fsPath), false)}
                    name={name}
                    path={fsPath}
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
