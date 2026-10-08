// A bookmark folder's contents as a widget. Reads the in-memory bookmark cache
// (platform/lib/bookmarks.ts) and re-reads on the bookmarks-changed event.
import { useBookmarksVersion } from "@platform/lib/hooks";
import { isFolder, loadBookmarks, type BookmarkFolder, type BookmarkItem } from "@platform/lib/bookmarks";
import { navigateUrl } from "@platform/lib/router";
import { itemCapacity, type Widget } from "../layout";
import { EmptyLine, ItemIcons, ItemList, type WidgetItem } from "./bits";

export function findFolder(items: BookmarkItem[], id: string): BookmarkFolder | undefined {
  for (const item of items) {
    if (!isFolder(item)) continue;
    if (item.id === id) return item;
    const hit = findFolder(item.children, id);
    if (hit) return hit;
  }
  return undefined;
}

export function folderItems(folder: BookmarkFolder): WidgetItem[] {
  return folder.children.map((c) =>
    isFolder(c)
      ? { key: c.id, name: c.name, icon: "📁" }
      : {
          key: c.id,
          name: c.name,
          href: c.url,
          onClick: (e: React.MouseEvent) => {
            if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            e.preventDefault();
            navigateUrl(c.url);
          },
          icon: c.icon || "★",
        },
  );
}

/** The folder a widget points at, or undefined once it was deleted. */
export function useWidgetFolder(widget: Widget): BookmarkFolder | undefined {
  useBookmarksVersion();
  return widget.folderId ? findFolder(loadBookmarks(), widget.folderId) : undefined;
}

export function FolderWidget({ widget }: { widget: Widget }) {
  const folder = useWidgetFolder(widget);
  if (!folder) {
    return (
      <div className="hw-body">
        <EmptyLine>This bookmark folder was deleted.</EmptyLine>
      </div>
    );
  }
  const items = folderItems(folder);
  if (!items.length) {
    return (
      <div className="hw-body">
        <EmptyLine>This folder is empty.</EmptyLine>
      </div>
    );
  }
  const cap = itemCapacity(widget.size, widget.format);
  return (
    <div className="hw-body">
      {widget.format === "icons" ? <ItemIcons items={items} cap={cap} /> : <ItemList items={items} cap={cap} />}
    </div>
  );
}
