// The explorer sidebar's Recents section (SPEC §29): last files opened, in
// stable-slot display order (RC-11). Extracted from the shell Sidebar when
// recents became an explorer concept (super-app step 2).
import React from "react";
import { navigateUrl } from "@platform/lib/router";
import { basename } from "@platform/lib/format";
import { loadRecents, displayRecents, setRecentsCollapsed, useRecentsVersion } from "@apps/explorer/lib/recents";
import { bookmarkFsPath } from "@apps/explorer/sidebar/BookmarksSection";

export default function RecentsSection() {
  useRecentsVersion();
  const { collapsed: recentsCollapsed } = loadRecents();
  const recents = displayRecents();

  const onRecentsHeadingClick = () => {
    // Persisted with the data itself (recents.json), like D44's folder
    // collapse; the store notifies, so no explicit re-render call here.
    void setRecentsCollapsed(!recentsCollapsed);
  };

  const onRecentClick = (e: React.MouseEvent<HTMLAnchorElement>, url: string) => {
    // Plain navigation to the stored url verbatim — query preserved
    // (navigateUrl, not navigate). href kept for middle-click/copy-link.
    // Opening a recent arms nothing — it is not a bookmark.
    e.preventDefault();
    navigateUrl(url);
  };

  if (recents.length === 0) return null;
  return (
    <div className="sidebar-section sidebar-recents">
      <div
        className="sidebar-heading recents-heading"
        title={recentsCollapsed ? "Show recents" : "Hide recents"}
        onClick={onRecentsHeadingClick}
      >
        Recents
        {recentsCollapsed && <span className="recents-count">{recents.length}</span>}
      </div>
      {!recentsCollapsed &&
        recents.map((r) => {
          const fsPath = bookmarkFsPath(r.url);
          return (
            <a
              // Keyed by fs path, not url: the url mutates on every live
              // param write, and a key change would remount (flash) the row.
              key={fsPath}
              // No active/selected state on recents rows (owner call —
              // unlike bookmark rows): the section is a jump list, not a
              // location indicator.
              className="bookmark-row recent-row"
              href={r.url}
              title={fsPath}
              onClick={(e) => onRecentClick(e, r.url)}
            >
              <span className="bookmark-glyph recent-glyph" aria-hidden="true">
                {/* Clock in the star-glyph slot, inline so it follows
                    currentColor like the folder icon. */}
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                  <circle cx="8" cy="8" r="6.2" />
                  <path d="M8 4.8V8l2.3 1.6" />
                </svg>
              </span>
              <span className="bookmark-name">{r.title || basename(fsPath)}</span>
            </a>
          );
        })}
    </div>
  );
}
