// Two file-location widgets: Local files (the home folder's top level) and
// S3 files (the top level of the S3 data available today). Rows open the
// entry in the explorer, the same as clicking it in a listing.
import { formatSize, timeAgo } from "@platform/lib/format";
import { spaLinkProps } from "@platform/lib/router";
import { iconForEntry } from "@platform/ui/FileIcons";
import type { FsEntry } from "@platform/lib/api";
import type { RemoteEntry } from "@platform/lib/remote";
import { useCanvasesFeature } from "@apps/canvases/feature-flag";
import { S3_ROOT, useHomeDirListing, useS3Listing } from "../data";
import { useHome } from "@platform/lib/home-path";
import { itemCapacity, type Widget } from "../layout";
import { EmptyLine, ErrorLine, ItemIcons, ItemList, ListSkeleton, type WidgetItem } from "./bits";

const joinPath = (dir: string, name: string) => dir.replace(/\/+$/, "") + "/" + name;

/** "See all" for each: the folder itself in the explorer. */
export function filesSeeAll(home: string): string | undefined {
  return home ? spaLinkProps(home, { isDir: true }).href : undefined;
}
export const S3_SEE_ALL = spaLinkProps(S3_ROOT, { isDir: true }).href;

function localItem(dir: string, e: FsEntry): WidgetItem {
  const path = joinPath(dir, e.name);
  const when = timeAgo(e.mtime) ?? "";
  return {
    key: path,
    name: e.name,
    sub: e.is_dir ? when || "Folder" : [formatSize(e.size), when].filter(Boolean).join(" · "),
    icon: iconForEntry(e.name, e.is_dir),
    ...spaLinkProps(path, { isDir: e.is_dir }),
  };
}

function s3Item(e: RemoteEntry): WidgetItem {
  const size = e.size != null ? formatSize(e.size) : "";
  return {
    key: e.url,
    name: e.name.replace(/\/$/, ""),
    sub: size || (e.is_dir ? "Folder" : undefined),
    icon: iconForEntry(e.name, e.is_dir),
    ...spaLinkProps(e.url, { isDir: e.is_dir }),
  };
}

function Items({ widget, items, moreHref }: { widget: Widget; items: WidgetItem[]; moreHref?: string }) {
  const cap = itemCapacity(widget.size, widget.format);
  return widget.format === "icons" ? (
    <ItemIcons items={items} cap={cap} moreHref={moreHref} />
  ) : (
    <ItemList items={items} cap={cap} moreHref={moreHref} />
  );
}

export function LocalFilesWidget({ widget }: { widget: Widget }) {
  const home = useHome();
  return (
    <div className="hw-body">
      {home ? <LocalFilesBody widget={widget} home={home} /> : <ListSkeleton rows={3} label="Loading your files" />}
    </div>
  );
}

/** Mounted once the home folder is known, so nothing is fetched or cached for an empty path. */
function LocalFilesBody({ widget, home }: { widget: Widget; home: string }) {
  const { data, error, retry } = useHomeDirListing(home);
  if (error && !data) return <ErrorLine message={error} onRetry={retry} />;
  if (!data) return <ListSkeleton rows={3} label="Loading your files" />;
  if (!data.length) return <EmptyLine>Your home folder is empty.</EmptyLine>;
  return <Items widget={widget} items={data.map((e) => localItem(home, e))} moreHref={filesSeeAll(home)} />;
}

/** S3 browsing rides the Canvases switch (the explorer's S3 locations do), so
    with it off the tile says so instead of fetching. */
export function S3FilesWidget({ widget }: { widget: Widget }) {
  const on = useCanvasesFeature();
  return <div className="hw-body">{on ? <S3Body widget={widget} /> : <EmptyLine>Turn on Fused Workbench in Preferences to browse S3 files.</EmptyLine>}</div>;
}

function S3Body({ widget }: { widget: Widget }) {
  const { data, error, retry } = useS3Listing();
  if (error && !data) return <ErrorLine message={error} onRetry={retry} />;
  if (!data) return <ListSkeleton rows={3} label="Loading S3 files" />;
  if (!data.length) return <EmptyLine>Nothing here yet.</EmptyLine>;
  return <Items widget={widget} items={data.map(s3Item)} moreHref={S3_SEE_ALL} />;
}
