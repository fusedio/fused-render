// The mount list: one dense row per mount, with its health icon, upload queue
// and per-mount actions. Split out of shell/Mounts.tsx, which is now just the
// page. Rows follow the Flow entity-row recipe but are written out here rather
// than through <EntityRow>: a mount row has a second line (the remote spec, an
// upload notice, its own error), which the single-line composite has no slot for.
import { useState } from "react";
import { FolderOpenIcon, RefreshCwIcon, XIcon } from "lucide-react";
import { deleteMount, reconnectMount } from "@platform/lib/api";
import type { Mount, MountUploads } from "@platform/lib/api";
import type { RcloneRemote } from "@platform/lib/api";
import { navigate } from "@platform/lib/router";
import { uploadNotice } from "@platform/lib/uploads";
import { Button } from "@platform/shadcn/ui/button";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { StatusIcon } from "@platform/ui/flow/StatusIcon";
import { ProviderIcon } from "@platform/ui/ProviderIcons";
import type { ProviderIconKey } from "@platform/ui/ProviderIcons";
import type { StatusBucket } from "@platform/ui/status-colors";
import { Note } from "./bits";

// The row's mark, from the SERVER's classification of the remote behind the
// mount — never from sniffing its name. The server only tells us the coarse
// cloud (s3 / gcs / other) and whether it is anonymous, so this is a family
// mark rather than a brand logo: a bucket for object storage, a globe for
// anonymous public data, and the generic server stack for everything the user
// connected themselves (Drive, Dropbox, Box, a custom endpoint). A mount whose
// remote is no longer in the config falls back to that same generic mark.
function markFor(remoteSpec: string, remotes: RcloneRemote[]): ProviderIconKey {
  const base = remoteSpec.slice(0, remoteSpec.indexOf(":") + 1);
  const r = remotes.find((x) => x.name === base);
  if (!r) return "s3compat";
  if (r.kind === "public") return "public";
  return r.provider === "s3" || r.provider === "gcs" ? "detected" : "s3compat";
}

// Mount health → status-colors bucket. Explicit rather than `status={state}`:
// the shared map knows "stale" as orange (a waiting state) and knows nothing of
// "mounted"/"disconnected", and here both broken states are the same red.
const HEALTH_BUCKET: Record<Mount["state"], StatusBucket> = {
  mounted: "green",
  disconnected: "red",
  stale: "red",
  unmounted: "neutral",
};

// Files written to this mount that haven't reached the remote yet (D221).
// Worth its own line because a mount caches writes locally and uploads them
// afterwards: the user already saw the save succeed, so a rejection at the
// remote (quota, permissions, a revoked token) is otherwise completely
// invisible.
//
// Three states, and conflating any two of them is the bug this shape exists to
// prevent — see lib/uploads.ts, which owns the decision and is tested there.
function UploadQueue({ uploads }: { uploads?: MountUploads | null }) {
  const notice = uploadNotice(uploads);
  switch (notice.kind) {
    case "none":
      return null;
    case "unknown":
      return (
        <Note tone="warn" title={notice.reason}>
          Upload status unavailable — saved files may not have reached the remote.
        </Note>
      );
    case "failed":
      return (
        <Note
          tone="warn"
          title="These files were saved on your computer but the remote rejected the upload — rclone keeps retrying with a growing delay."
        >
          {notice.failed} {notice.failed === 1 ? "file has" : "files have"} not reached the remote
          {notice.names.length > 0 && <> — {notice.names.join(", ")}</>}
          {notice.truncated && <> …</>}. Saved locally; still retrying.
        </Note>
      );
    case "pending":
      return (
        <Note title="Saved on your computer and uploading to the remote.">
          Uploading {notice.pending} {notice.pending === 1 ? "file" : "files"}…
        </Note>
      );
  }
}

export function MountRow({
  conn,
  remotes,
  onChanged,
}: {
  conn: Mount;
  // Only to classify the row's mark (markFor) — the row itself is driven
  // entirely by `conn`.
  remotes: RcloneRemote[];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const iconKey = markFor(conn.remote, remotes);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  // "disconnected": a mount is (or was) there but its rclone daemon no longer
  // serves it — listings show stale/empty data and a plain unmount fails.
  // "stale": the 2026-07-16 split-brain — rclone still lists the mount but the
  // kernel dropped it (e.g. the macOS "Server connections interrupted" dialog's
  // Disconnect). Both are unhealthy and both recover the same way: Reconnect
  // force-clears the dead mountpoint and mounts fresh.
  const dotLabel = {
    mounted: "Mounted",
    disconnected: "Disconnected — remote data is not flowing",
    stale: "Disconnected — the mount dropped; reconnect to restore it",
    unmounted: "Not mounted",
  }[conn.state];
  // Both broken states show the same "disconnected" badge and Reconnect remedy;
  // "stale" is a distinct backend state (for logs/diagnosis) but the same fix.
  const broken = conn.state === "disconnected" || conn.state === "stale";

  return (
    <div data-slot="entity-row" className="flex flex-col gap-2 px-4 py-2 text-sm border-b border-border last:border-b-0">
      <div className="flex items-center gap-3 min-w-0">
        <StatusIcon bucket={HEALTH_BUCKET[conn.state]} filled={conn.state === "mounted"} label={dotLabel} />
        <span className="shrink-0 flex items-center text-muted-foreground size-4 [&_svg]:size-full" aria-hidden="true">
          <ProviderIcon provider={iconKey} />
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2 min-w-0">
            <span className="font-medium truncate">{conn.name}</span>
            {conn.read_only && (
              <span className="text-xs text-muted-foreground shrink-0" title="This remote rejects writes — files open read-only">
                read-only
              </span>
            )}
            {broken && (
              <Note
                tone="warn"
                className="shrink-0"
                title="The mount stopped responding — remote data is not flowing. Use Reconnect to restore it."
              >
                disconnected
              </Note>
            )}
          </div>
          <div className="font-mono text-xs text-muted-foreground truncate" title={conn.mountpoint}>
            {conn.remote}
          </div>
          <UploadQueue uploads={conn.uploads} />
        </div>
        <div className="shrink-0 flex items-center gap-1">
          {conn.state === "mounted" ? (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={busy}
              title="Open this mount in the explorer"
              onClick={() => navigate(conn.mountpoint, { isDir: true })}
            >
              <FolderOpenIcon data-icon="inline-start" />
              Open
            </Button>
          ) : (
            // "disconnected", "stale" and "unmounted" all recover the same way: there is
            // no unmount action (mounts automount and stay up), so Reconnect is
            // the single "something's wrong" repair — it force-clears any dead
            // mountpoint and mounts fresh (reconnect_mount also handles the
            // never-mounted case, where it just attaches).
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => act(() => reconnectMount(conn.id))}
            >
              <RefreshCwIcon data-icon="inline-start" className={busy ? "motion-safe:animate-spin" : undefined} />
              {busy ? "Reconnecting…" : "Reconnect"}
            </Button>
          )}
          {/* An icon-only remove, but LABELLED (aria-label + title): removing a
              mount only unmounts it; nothing on the remote is touched, and a
              bare "✕" gave no clue which of those two it was. */}
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground hover:text-destructive"
            disabled={busy}
            aria-label={`Remove mount ${conn.name}`}
            title="Remove this mount — the folder disappears locally; nothing on the remote is deleted"
            onClick={() => act(() => deleteMount(conn.id))}
          >
            <XIcon />
          </Button>
        </div>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}
