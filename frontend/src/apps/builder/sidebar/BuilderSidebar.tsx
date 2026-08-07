// The app builder's sidebar: Home (→ /apps, the apps hub) and app recents.
// Owned by the builder app — the shell picks this component whenever an
// /apps/<tag>/<name> route is active.
import { SidebarFrame, NavItem, HOME_ICON } from "@platform/ui/sidebar/SidebarFrame";
import { useUrlVersion } from "@platform/lib/hooks";
import type { Config } from "@platform/lib/api";
import RecentsSection from "./RecentsSection";

export default function BuilderSidebar({ config }: { config: Config }) {
  // Re-render on any nav/url change (active-item highlight).
  useUrlVersion();
  return (
    <SidebarFrame title="App" homeHref="/apps">
      <div className="sidebar-section">
        <NavItem href="/apps" id="builder-home-link" label="Home" icon={HOME_ICON} />
      </div>
      <RecentsSection />
    </SidebarFrame>
  );
}
