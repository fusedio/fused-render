// Community apps from the marketplace catalog: featured first, then the most
// installed. A row opens the app's store page (install from there).
import { AppIcon } from "@platform/ui/appstore/AppIcon";
import { MARKETPLACE_HOME, appDetailHref, refreshCatalog, useCatalog, type AppEntry } from "@platform/lib/marketplace";
import { itemCapacity, type Widget } from "../layout";
import { EmptyLine, ErrorLine, ItemIcons, ItemList, ListSkeleton, type WidgetItem } from "./bits";

/** Featured first, then by installs, then by name. Pure, so it is testable. */
export function communityOrder(apps: AppEntry[]): AppEntry[] {
  return apps
    .filter((a) => a.compatible !== false)
    .sort(
      (a, b) =>
        Number(b.featured) - Number(a.featured) ||
        (b.install_count ?? 0) - (a.install_count ?? 0) ||
        a.name.localeCompare(b.name),
    );
}

function appItem(app: AppEntry, size: number): WidgetItem {
  return {
    key: app.slug,
    name: app.name,
    sub: app.installed ? "Installed" : app.description || undefined,
    icon: <AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={size} />,
    href: appDetailHref(app.slug),
    pill: app.update_available ? { label: "update", tone: "warn" } : undefined,
  };
}

export function MarketplaceWidget({ widget }: { widget: Widget }) {
  const { catalog, error } = useCatalog();
  let body;
  if (!catalog && error) body = <ErrorLine message="Couldn't reach the Community." onRetry={() => void refreshCatalog({ force: true })} />;
  else if (!catalog) body = <ListSkeleton rows={3} label="Loading Community apps" />;
  else {
    const apps = communityOrder(catalog.apps);
    if (!apps.length) body = <EmptyLine>No Community apps yet.</EmptyLine>;
    else {
      const icons = widget.format === "icons";
      const items = apps.map((a) => appItem(a, icons ? 48 : 22));
      const cap = itemCapacity(widget.size, widget.format);
      body = icons ? (
        <ItemIcons items={items} cap={cap} moreHref={MARKETPLACE_HOME} />
      ) : (
        <ItemList items={items} cap={cap} moreHref={MARKETPLACE_HOME} />
      );
    }
  }
  return <div className="hw-body">{body}</div>;
}
