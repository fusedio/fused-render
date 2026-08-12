// The file explorer's sidebar: Home (→ /explorer, the bookmark launcher),
// the bookmark tree, and file recents. Owned by the explorer app — the shell
// just picks this component when an explorer route is active.
import { SidebarFrame, NavItem, HOME_ICON } from "@platform/ui/sidebar/SidebarFrame";
import type { SidebarRailItem } from "@platform/ui/sidebar/SidebarFrame";
import { useUrlVersion } from "@platform/lib/hooks";
import type { Config } from "@platform/lib/api";
import { loadRecents, displayRecents, setRecentsCollapsed, useRecentsVersion } from "@apps/explorer/lib/recents";
import BookmarksSection from "./BookmarksSection";
import RecentsSection from "./RecentsSection";

// Rail glyphs — outline versions of the marks the sections themselves use:
// RecentsSection's clock and the bookmark rows' star, at the rail's 16px.
const RECENTS_RAIL_ICON = (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
    <circle cx="8" cy="8" r="6.2" />
    <path d="M8 4.8V8l2.3 1.6" />
  </svg>
);
const BOOKMARKS_RAIL_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 2.8l2.8 5.7 6.3.9-4.6 4.4 1.1 6.3L12 17.2l-5.6 2.9 1.1-6.3-4.6-4.4 6.3-.9L12 2.8z" />
  </svg>
);

export default function ExplorerSidebar({ config }: { config: Config }) {
  // Re-render on any nav/url change (active-item highlight).
  useUrlVersion();
  // The rail mirrors the sections: Recents (which renders null when empty —
  // so its icon disappears with it) above Bookmarks, the order they hold in
  // the expanded sidebar.
  useRecentsVersion();
  const rail: SidebarRailItem[] = [
    ...(displayRecents().length
      ? [
          {
            key: "recents",
            label: "Recents",
            icon: RECENTS_RAIL_ICON,
            // The icon promises the section, so a section-collapsed Recents
            // opens with it — expanding to a hidden list would land nowhere.
            onExpand: () => {
              if (loadRecents().collapsed) void setRecentsCollapsed(false);
            },
          },
        ]
      : []),
    { key: "bookmarks", label: "Bookmarks", icon: BOOKMARKS_RAIL_ICON },
  ];
  return (
    <SidebarFrame title="Explorer" homeHref="/explorer" rail={rail}>
      <div className="sidebar-section">
        <NavItem href="/explorer" id="explorer-home-link" label="Home" icon={HOME_ICON} />
      </div>
      <RecentsSection />
      <BookmarksSection />
    </SidebarFrame>
  );
}
