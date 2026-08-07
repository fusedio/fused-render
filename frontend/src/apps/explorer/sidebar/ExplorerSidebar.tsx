// The file explorer's sidebar: Home (→ /explorer, the bookmark launcher),
// the bookmark tree, and file recents. Owned by the explorer app — the shell
// just picks this component when an explorer route is active.
import { SidebarFrame, NavItem, HOME_ICON } from "@platform/ui/sidebar/SidebarFrame";
import { useUrlVersion } from "@platform/lib/hooks";
import type { Config } from "@platform/lib/api";
import BookmarksSection from "./BookmarksSection";
import RecentsSection from "./RecentsSection";

export default function ExplorerSidebar({ config }: { config: Config }) {
  // Re-render on any nav/url change (active-item highlight).
  useUrlVersion();
  return (
    <SidebarFrame title="Explorer" version={config.version} homeHref="/explorer">
      <div className="sidebar-section">
        <NavItem href="/explorer" id="explorer-home-link" label="Home" icon={HOME_ICON} />
      </div>
      <BookmarksSection />
      <RecentsSection />
    </SidebarFrame>
  );
}
