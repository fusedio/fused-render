// Two file-location widgets: Local files (the home folder's top level) and
// S3 files (the S3 buckets indexed for the signed-in account, one row per
// index). Rows open the entry in the explorer, the same as clicking it in a
// listing.
import { useEffect, useRef, useState } from "react";
import { formatSize, timeAgo } from "@platform/lib/format";
import { spaLinkProps } from "@platform/lib/router";
import { iconForEntry } from "@platform/ui/FileIcons";
import type { FsEntry } from "@platform/lib/api";
import { remoteSyncNow, type RemoteBucket } from "@platform/lib/remote";
import { notify } from "@platform/lib/notifications";
import { useCanvasesFeature } from "@apps/canvases/feature-flag";
import { S3_BUCKETS_CHANGED_EVENT, useHomeDirListing, useS3Buckets } from "../data";
import { useHome } from "@platform/lib/home-path";
import { itemCapacity, type Widget } from "../layout";
import { EmptyLine, ErrorLine, ItemIcons, ItemList, ListSkeleton, type WidgetItem } from "./bits";

const joinPath = (dir: string, name: string) => dir.replace(/\/+$/, "") + "/" + name;

/** "See all" for each: the folder itself in the explorer. */
export function filesSeeAll(home: string): string | undefined {
  return home ? spaLinkProps(home, { isDir: true }).href : undefined;
}

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

/** A bucket's row: named by bucket (and prefix, for an index of one part of
    it), its secondary line the sync state. */
function bucketItem(b: RemoteBucket): WidgetItem {
  const name = b.prefix ? `${b.bucket}/${b.prefix.replace(/\/+$/, "")}` : b.bucket;
  const files = `${b.rows.toLocaleString()} ${b.rows === 1 ? "file" : "files"}`;
  const sub =
    b.state === "ready"
      ? b.syncing ? `${files} · syncing…` : files
      : b.state === "fetching"
        ? "Syncing…"
        : b.state === "error"
          ? b.error || "Couldn't sync"
          : "Not synced yet";
  return {
    key: b.id,
    name,
    sub,
    icon: iconForEntry(name, true),
    ...spaLinkProps(b.url, { isDir: true }),
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
  const { data, error, retry } = useS3Buckets();
  if (error && !data) return <ErrorLine message={error} onRetry={retry} />;
  if (!data) return <ListSkeleton rows={3} label="Loading S3 buckets" />;
  if (!data.signed_in) return <EmptyLine>Sign in to Fused to browse S3.</EmptyLine>;
  if (!data.buckets.length) {
    return data.error ? <ErrorLine message={data.error} onRetry={retry} /> : <EmptyLine>No S3 buckets are indexed for you yet.</EmptyLine>;
  }
  return <Items widget={widget} items={data.buckets.map(bucketItem)} />;
}

/** How long after "refresh now" to re-read the list: long enough for the
    server to have marked the buckets it is syncing. */
const REFRESH_RELOAD_MS = 1500;

/** The S3 tile's header action (where "See all" sits on other tiles): ask the
    server to re-sync the bucket indexes now, then re-read the list. Nothing
    with Fused Workbench off, like the body. */
export function S3RefreshButton() {
  const on = useCanvasesFeature();
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  if (!on) return null;
  const refresh = () => {
    setBusy(true);
    remoteSyncNow().then(
      () => {
        timer.current = window.setTimeout(() => {
          setBusy(false);
          window.dispatchEvent(new Event(S3_BUCKETS_CHANGED_EVENT));
        }, REFRESH_RELOAD_MS);
      },
      (err: Error) => {
        setBusy(false);
        notify({ title: `Could not refresh S3: ${err.message || "the request failed"}`, tone: "error" });
      },
    );
  };
  return (
    <button type="button" className="hw-btn" disabled={busy} title="Check for new and updated S3 buckets now" onClick={refresh}>
      {busy ? "Refreshing…" : "Refresh"}
    </button>
  );
}
