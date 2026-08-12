// The file explorer's sidebar: Home (→ /explorer, the bookmark launcher),
// the bookmark tree, and file recents. Owned by the explorer app — the shell
// just picks this component when an explorer route is active.
import { SidebarFrame, NavItem, HOME_ICON } from "@platform/ui/sidebar/SidebarFrame";
import type { SidebarRailItem } from "@platform/ui/sidebar/SidebarFrame";
import { useUrlVersion } from "@platform/lib/hooks";
import type { Config } from "@platform/lib/api";
import BookmarksSection from "./BookmarksSection";
import RecentsSection from "./RecentsSection";

// One rail icon, and it is a DESTINATION like every shell rail icon: Home.
// The rail briefly carried Recents/Bookmarks icons that expanded the sidebar
// to their section — a second dialect the shell's rail didn't speak, so what
// a rail click did depended on the route. Now both sidebars agree: icons
// navigate and the rail stays collapsed; the chevron alone expands. Home is
// the row this sidebar pins on top when expanded, so the collapsed strip
// offers the same first move.
const RAIL: SidebarRailItem[] = [
  { key: "home", label: "Home", icon: HOME_ICON, href: "/explorer" },
];

export default function ExplorerSidebar({ config }: { config: Config }) {
  // Re-render on any nav/url change (active-item highlight).
  useUrlVersion();
  return (
    <SidebarFrame title="Explorer" homeHref="/explorer" rail={RAIL}>
      <div className="sidebar-section">
        <NavItem href="/explorer" id="explorer-home-link" label="Home" icon={HOME_ICON} />
      </div>
      <RecentsSection />
      <BookmarksSection />
    </SidebarFrame>
  );
}
