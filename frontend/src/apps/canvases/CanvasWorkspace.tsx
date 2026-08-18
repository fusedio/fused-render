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
// the next local push; the banner says so). This page never reloads the
// workbench iframe — the hosted workbench refreshes itself on upstream
// changes.
import { useCallback, useEffect, useRef, useState } from "react";
import { embedUrlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  fixWithClaude,
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
  const [dir, setDir] = useState<string | null>(null);
  const [sync, setSync] = useState<SyncStatus | null>(null);
  // Splitter position: workbench pane width as a fraction of the row.
  const [leftFrac, setLeftFrac] = useState(0.55);
  const [dragging, setDragging] = useState(false);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A "Fix with Claude" run in flight: the editor iframe attaches to it via
  // its ?run= param, so the user watches the fix happen in the chat they
  // already have. Kept after the push recovers — the session may still be
  // finishing its final message.
  const [fixRunId, setFixRunId] = useState<string | null>(null);
  const [fixBusy, setFixBusy] = useState(false);
  const [fixError, setFixError] = useState<string | null>(null);
  // True once the push recovered while a fix run was attached: a LATER
  // failure is a new problem, so the button must offer a new fix rather
  // than pointing at the finished run forever. The iframe stays attached
  // to the old run until a new fix replaces it.
  const [fixStale, setFixStale] = useState(false);
  // Guards fix_run_running's one blind spot: right after spawn, the run's
  // transcript file may not exist yet, so a poll landing in that gap reads
  // "not running" indistinguishable from "already finished". Only trust a
  // "not running" reading once this run has been OBSERVED running at least
  // once — reset per fixRunId in onFix.
  const sawFixRunningRef = useRef(false);
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
        const started = await startSync(name);
        if (!cancelled) setDir(started.dir);
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

  // Sync status poll for the status strip; re-arms the watcher if it drops.
  useEffect(() => {
    const id = window.setInterval(() => {
      void getSyncStatus(name, fixRunId ?? undefined)
        .then((s) => {
          setSync(s);
          setDir((d) => d ?? s.dir);
          // A push recovering ("idle") always means the button should offer a
          // fresh fix for any later failure. Otherwise, while a run_id is
          // attached, fix_run_running is the actual signal: push_state alone
          // can't distinguish a fix still working (which must stay disabled,
          // or a second click races it on the same clone) from one that
          // finished and failed again (which must re-enable, or a bad fix
          // leaves the button stuck on "Claude is on it" forever).
          if (fixRunId && s.fix_run_running) sawFixRunningRef.current = true;
          const fixRunDone =
            fixRunId && s.fix_run_running === false && sawFixRunningRef.current;
          if (s.push_state === "idle" || fixRunDone) setFixStale(true);
          // Self-heal: a server restart drops the watcher; re-arm it.
          if (!s.watching) void startSync(name).catch(() => undefined);
        })
        .catch(() => undefined);
    }, SYNC_POLL_MS);
    return () => window.clearInterval(id);
  }, [name, fixRunId]);

  // Splitter drag: track the pointer over the whole window so the drag
  // survives entering the iframes (which would otherwise swallow mousemove —
  // pointer-events are disabled on both panes while dragging).
  useEffect(() => {
    if (!dragging) return;
    const onMove = (e: MouseEvent) => {
      const row = rowRef.current;
      if (!row) return;
      const rect = row.getBoundingClientRect();
      const frac = (e.clientX - rect.left) / rect.width;
      setLeftFrac(Math.min(0.85, Math.max(0.15, frac)));
    };
    const onUp = () => setDragging(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [dragging]);

  // Right pane: the local clone opened in the chrome-free explorer embed with
  // the Claude template — the editing surface over the files the watcher
  // pushes.
  const editorSrc = dir
    ? embedUrlForFsPath(
        dir,
        fixRunId
          ? `?_mode=claude&run=${encodeURIComponent(fixRunId)}`
          : "?_mode=claude",
      )
    : null;

  const onFix = async () => {
    setFixBusy(true);
    setFixError(null);
    try {
      const { run_id } = await fixWithClaude(name);
      sawFixRunningRef.current = false;
      setFixRunId(run_id);
      setFixStale(false);
    } catch (e) {
      setFixError((e as Error).message);
    } finally {
      setFixBusy(false);
    }
  };

  const frameSrc =
    baseUrl && handle
      ? `${baseUrl}/workbench/${encodeURIComponent(handle)}/${encodeURIComponent(name)}?fused_embed_auth=1`
      : null;

  // No header strip (owner call): the workspace is the two panes, full
  // height. What the strip used to carry is either redundant or surfaces
  // elsewhere — back is the browser's Back button, the canvas name is the
  // page title/URL, "Open local files" is the chat pane's own affordances
  // over the same folder, and sync state only matters when it FAILS, which
  // the error banner below still reports. The poll keeps running for the
  // banner and the watcher self-heal.
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {sync?.push_state === "error" && sync.error && (
        <div
          style={{
            padding: "8px 12px",
            borderBottom: "1px solid rgba(220,60,60,0.35)",
            background: "rgba(220,60,60,0.08)",
            fontSize: 13,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <strong>{sync.error}</strong>
            <button
              className="btn btn-primary"
              disabled={fixBusy || (!!fixRunId && !fixStale)}
              onClick={() => void onFix()}
              title="Start a Claude session on the local clone, primed with these errors"
            >
              {fixRunId && !fixStale
                ? "Claude is on it — see chat →"
                : fixBusy
                  ? "Starting…"
                  : "Fix with Claude"}
            </button>
            {fixError && <span style={{ color: "rgb(200,60,60)" }}>{fixError}</span>}
          </div>
          {sync.error_detail?.length > 0 && (
            <pre
              style={{
                margin: "6px 0 0",
                maxHeight: 140,
                overflow: "auto",
                whiteSpace: "pre-wrap",
                fontSize: 12,
                opacity: 0.85,
              }}
            >
              {sync.error_detail.join("\n")}
            </pre>
          )}
        </div>
      )}
      <div
        ref={rowRef}
        style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "row" }}
      >
        <div
          style={{
            flex: `0 0 ${leftFrac * 100}%`,
            minWidth: 0,
            pointerEvents: dragging ? "none" : "auto",
          }}
        >
          {frameSrc ? (
            <iframe
              ref={frameRef}
              src={frameSrc}
              title={`Workbench: ${name}`}
              style={{ width: "100%", height: "100%", border: 0 }}
            />
          ) : (
            !error && <p style={{ padding: 16 }}>Loading workbench…</p>
          )}
        </div>
        <div
          onMouseDown={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          style={{
            flex: "0 0 6px",
            cursor: "col-resize",
            background: dragging
              ? "rgba(100,140,255,0.5)"
              : "rgba(128,128,128,0.25)",
          }}
          title="Drag to resize"
        />
        <div
          style={{
            flex: 1,
            minWidth: 0,
            pointerEvents: dragging ? "none" : "auto",
          }}
        >
          {editorSrc ? (
            <iframe
              src={editorSrc}
              title={`Edit: ${name}`}
              style={{ width: "100%", height: "100%", border: 0 }}
            />
          ) : (
            !error && <p style={{ padding: 16 }}>Loading editor…</p>
          )}
        </div>
      </div>
    </div>
  );
}
