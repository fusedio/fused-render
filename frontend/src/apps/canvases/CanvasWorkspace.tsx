// Canvas workspace (/canvases/<name>): the embedded live workbench beside a
// sync status strip for the local clone.
//
// The iframe loads the AUTHENTICATED workbench editor
// (<base>/workbench/<handle>/<name>?fused_embed_auth=1). The Auth0 cookie
// never reaches a cross-site iframe, so the workbench's embed-auth mode asks
// its parent (this page) for a Bearer token over postMessage:
//   iframe → { type: "fused-embed-auth-ready" }   (also -refresh on a 401)
//   parent → { type: "fused-embed-auth-token", accessToken }
// The token is the CLI's own `fused login` JWT (GET /api/canvases/token) and
// is posted with an EXACT targetOrigin — never "*" — so it cannot leak to a
// frame that navigated elsewhere.
//
// Sync: the server watches the clone folder and pushes on every quiet period
// (local wins — an edit made inside the embedded workbench is overwritten by
// the next local push; the banner says so). The hosted workbench has no
// push-driven reload, so when push_seq moves this page reloads the iframe.
import { useCallback, useEffect, useRef, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  getAccessToken,
  getCanvasesStatus,
  getSyncStatus,
  getWhoami,
  startSync,
  type SyncStatus,
} from "./api";

const SYNC_POLL_MS = 2000;

export function canvasNameFromPath(pathname: string): string | null {
  const match = /^\/canvases\/([A-Za-z0-9_]+)$/.exec(pathname);
  return match ? decodeURIComponent(match[1]) : null;
}

export default function CanvasWorkspace({ name }: { name: string }) {
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [sync, setSync] = useState<SyncStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped when push_seq moves → remounts the iframe so the editor shows the
  // pushed code (the hosted workbench never reloads itself on push).
  const [frameEpoch, setFrameEpoch] = useState(0);
  const lastPushSeq = useRef<number | null>(null);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const baseOriginRef = useRef<string | null>(null);

  // Boot: base URL + handle + make sure the watcher runs.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const status = await getCanvasesStatus();
        if (cancelled) return;
        setBaseUrl(status.workbench_base_url);
        baseOriginRef.current = new URL(status.workbench_base_url).origin;
        const who = await getWhoami();
        if (cancelled) return;
        if (!who.handle) {
          setError("could not determine your Fused username (fused whoami)");
          return;
        }
        setHandle(who.handle);
        await startSync(name);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [name]);

  // Token handshake with the embedded workbench.
  const seedToken = useCallback(async () => {
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    try {
      const { access_token } = await getAccessToken();
      frame.contentWindow.postMessage(
        { type: "fused-embed-auth-token", accessToken: access_token },
        origin, // exact origin — this message carries a credential
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== baseOriginRef.current) return;
      const type = event.data?.type;
      if (type === "fused-embed-auth-ready" || type === "fused-embed-auth-refresh") {
        void seedToken();
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [seedToken]);

  // Sync status poll; reload the iframe when a push lands.
  useEffect(() => {
    const id = window.setInterval(() => {
      void getSyncStatus(name)
        .then((s) => {
          setSync(s);
          // Self-heal: a server restart drops the watcher; re-arm it.
          if (!s.watching) void startSync(name).catch(() => undefined);
          if (lastPushSeq.current !== null && s.push_seq !== lastPushSeq.current) {
            setFrameEpoch((n) => n + 1);
          }
          lastPushSeq.current = s.push_seq;
        })
        .catch(() => undefined);
    }, SYNC_POLL_MS);
    return () => window.clearInterval(id);
  }, [name]);

  const frameSrc =
    baseUrl && handle
      ? `${baseUrl}/workbench/${encodeURIComponent(handle)}/${encodeURIComponent(name)}?fused_embed_auth=1`
      : null;

  const pushLabel =
    sync?.push_state === "pushing"
      ? "Pushing…"
      : sync?.push_state === "pending"
        ? "Local changes — push queued"
        : sync?.push_state === "error"
          ? "Push failed"
          : sync?.last_push_at
            ? "Synced"
            : "Watching";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "6px 12px",
          borderBottom: "1px solid rgba(128,128,128,0.25)",
          fontSize: 13,
        }}
      >
        <button onClick={() => navigateUrl("/canvases")}>← Canvases</button>
        <strong>{name}</strong>
        <span
          style={{
            padding: "2px 8px",
            borderRadius: 10,
            background:
              sync?.push_state === "error"
                ? "rgba(220,60,60,0.15)"
                : "rgba(128,128,128,0.12)",
          }}
        >
          {pushLabel}
        </span>
        {sync?.dir && (
          <button
            onClick={() =>
              navigateUrl(
                "/explorer/view/" +
                  sync.dir.split("/").filter(Boolean).map(encodeURIComponent).join("/"),
              )
            }
            title={sync.dir}
          >
            Open local files
          </button>
        )}
        <span style={{ marginLeft: "auto", opacity: 0.6 }}>
          Local wins: edits made inside the embedded workbench are overwritten
          by the next local push.
        </span>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {sync?.push_state === "error" && sync.error && <ErrorBanner>{sync.error}</ErrorBanner>}
      <div style={{ flex: 1, minHeight: 0 }}>
        {frameSrc ? (
          <iframe
            key={frameEpoch}
            ref={frameRef}
            src={frameSrc}
            title={`Workbench: ${name}`}
            style={{ width: "100%", height: "100%", border: 0 }}
          />
        ) : (
          !error && <p style={{ padding: 16 }}>Loading workbench…</p>
        )}
      </div>
    </div>
  );
}
