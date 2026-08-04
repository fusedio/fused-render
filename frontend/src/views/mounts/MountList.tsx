// The mount list: one card per mount, with its health dot, upload queue and
// per-mount actions. Split out of views/Mounts.tsx, which is now just the page.
import { useState } from "react";
import { deleteMount, reconnectMount } from "../../lib/api";
import type { Mount, MountUploads } from "../../lib/api";
import { navigate } from "../../lib/router";
import { uploadNotice } from "../../lib/uploads";
import { ErrorBanner } from "../../components/ErrorBanner";

// Files written to this mount that haven't reached the remote yet (D207).
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
        <div className="mount-hint warn" title={notice.reason}>
          Upload status unavailable — saved files may not have reached the remote.
        </div>
      );
    case "failed":
      return (
        <div
          className="mount-hint warn"
          title="These files were saved on your computer but the remote rejected the upload — rclone keeps retrying with a growing delay."
        >
          {notice.failed} {notice.failed === 1 ? "file has" : "files have"} not reached the
          remote
          {notice.names.length > 0 && <> — {notice.names.join(", ")}</>}
          {notice.truncated && <> …</>}. Saved locally; still retrying.
        </div>
      );
    case "pending":
      return (
        <div className="mount-hint" title="Saved on your computer and uploading to the remote.">
          Uploading {notice.pending} {notice.pending === 1 ? "file" : "files"}…
        </div>
      );
  }
}

export function MountRow({
  conn,
  onChanged,
}: {
  conn: Mount;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
    <div className="mount-card">
      <div className="mount-card-main">
        <span className={`mount-dot ${conn.state}`} role="img" aria-label={dotLabel} title={dotLabel} />
        <div className="mount-card-info">
          <div style={{ fontWeight: 600 }}>
            {conn.name}
            {conn.read_only && (
              <span className="mount-hint" title="This remote rejects writes — files open read-only">
                {" "}
                — read-only
              </span>
            )}
            {broken && (
              <span
                className="mount-hint warn"
                title="The mount stopped responding — remote data is not flowing. Use Reconnect to restore it."
              >
                {" "}
                — disconnected
              </span>
            )}
          </div>
          <div className="deploy-muted mount-remote" title={conn.mountpoint}>
            {conn.remote}
          </div>
          <UploadQueue uploads={conn.uploads} />
        </div>
        <div className="mount-card-actions">
          {conn.state === "mounted" ? (
            <button type="button" disabled={busy} onClick={() => navigate(conn.mountpoint, { isDir: true })}>
              Open
            </button>
          ) : (
            // "disconnected", "stale" and "unmounted" all recover the same way: there is
            // no unmount action (mounts automount and stay up), so Reconnect is
            // the single "something's wrong" repair — it force-clears any dead
            // mountpoint and mounts fresh (reconnect_mount also handles the
            // never-mounted case, where it just attaches).
            <button
              type="button"
              disabled={busy}
              onClick={() => act(() => reconnectMount(conn.id))}
            >
              {busy ? "Reconnecting…" : "Reconnect"}
            </button>
          )}
        </div>
        {!conn.builtin && (
          <button
            type="button"
            className="mount-delete"
            disabled={busy}
            title="Delete mount"
            aria-label="Delete mount"
            onClick={() => act(() => deleteMount(conn.id))}
          >
            ✕
          </button>
        )}
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}
