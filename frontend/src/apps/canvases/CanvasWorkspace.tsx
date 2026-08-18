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
import { decideLock, type LockHold, lockMessage } from "./canvas-lock-lib";

const SYNC_POLL_MS = 2000;

// -- the left-pane lock --------------------------------------------------------
//
// While a Claude session edits the clone, the user must not also be editing the
// same canvas in the embedded workbench: workbench collection saves are
// last-writer-wins with no revision precondition, which is exactly the D339
// incident (a stale tab autosaved its pre-push in-memory state back over the
// remote). So we ask the workbench to go read-only.
//
// ENFORCEMENT LIVES IN THE DEPLOYED WORKBENCH, not here. An overlay in this page
// stops clicks but NOT the workbench's own autosave timers, so this page must
// never imply protection it does not have. Hence the capability handshake: the
// workbench acks, and without an ack we fall back to a scrim plus a visible
// warning that the lock is advisory only.
const LOCK_ACK_TIMEOUT_MS = 2000;

// How long the lock is HELD after the session's work has settled.
//
// Releasing the instant the agent's process exits re-opens the very window the
// lock exists to close, for two reasons that compound: on unlock the workbench
// FLUSHES whatever is dirty in its memory (anything that accumulated before or
// during the lock autosaves immediately), and the embedded workbench only
// notices upstream changes on its own ~10s poll, after which it re-hydrates in
// place. Release too early and the sequence is: agent's last push still in
// flight or not yet pulled → workbench flushes stale in-memory state →
// last-writer-wins overwrites the agent's work. That is the D339 shape again,
// reproduced by our own unlock. So the grace window has to outlast that poll.
const WORKBENCH_UPSTREAM_POLL_MS = 10000;
const LOCK_RELEASE_GRACE_MS = WORKBENCH_UPSTREAM_POLL_MS + 3000;

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
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const baseOriginRef = useRef<string | null>(null);
  // Lock state. `hold` is why we are locked; `acked` is whether the workbench
  // confirmed it can actually enforce it (null = not yet known, false = it
  // never answered, so the lock is advisory and we say so). Sticky once true:
  // the capability belongs to the deployed workbench, not to one lock cycle.
  const [lockHold, setLockHold] = useState<LockHold | null>(null);
  const [lockAcked, setLockAcked] = useState<boolean | null>(null);
  // Whether a lock is currently engaged, readable synchronously from the effect
  // that decides the next hold (conditions 2 and 3 may only EXTEND a lock).
  const lockEngagedRef = useRef(false);
  // When the session's work first looked settled, in LOCAL time. Deliberately
  // not `last_push_at`: that is the server's clock, and comparing it to
  // Date.now() makes the grace window wrong by whatever the skew is.
  const settledAtRef = useRef<number | null>(null);

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

  // Tell the workbench to go read-only (or not). Same discipline as the token
  // handshake: an EXACT targetOrigin, never "*".
  const sendLock = useCallback((locked: boolean) => {
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    frame.contentWindow.postMessage({ type: "fused-embed-lock", locked }, origin);
  }, []);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== baseOriginRef.current) return;
      const type = event.data?.type;
      if (type === "fused-embed-auth-ready" || type === "fused-embed-auth-refresh") {
        void seedToken();
        // Re-assert the lock on (re)load: a workbench that reloaded mid-lock
        // comes back editable otherwise, and this is the one message that tells
        // us a fresh frame is listening.
        if (lockEngagedRef.current) sendLock(true);
      }
      if (type === "fused-embed-lock-ack") setLockAcked(true);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [seedToken, sendLock]);

  // Decide whether the workbench should be locked, from the polled status. The
  // rule itself lives in canvas-lock-lib (pure, and tested — the release half
  // is correctness-critical). Re-runs on every poll, which is what advances the
  // grace window.
  useEffect(() => {
    const now = Date.now();
    const decision = decideLock(
      sync,
      lockEngagedRef.current,
      settledAtRef.current,
      now,
      LOCK_RELEASE_GRACE_MS,
    );
    settledAtRef.current = decision.settledAt;
    setLockHold(decision.hold);
    if (decision.hold !== "settling" || decision.settledAt === null) return;
    // The next poll would get there anyway; this just releases on time rather
    // than up to SYNC_POLL_MS late.
    const remaining = decision.settledAt + LOCK_RELEASE_GRACE_MS - now;
    const id = window.setTimeout(
      () => setLockHold((h) => (h === "settling" ? null : h)),
      Math.max(0, remaining),
    );
    return () => window.clearTimeout(id);
  }, [sync]);

  // Push each lock transition to the workbench, and probe whether it can
  // actually enforce it.
  const locked = lockHold !== null;
  useEffect(() => {
    lockEngagedRef.current = locked;
    sendLock(locked);
    if (!locked || lockAcked !== null) return;
    // No ack yet: give the workbench a moment, then treat silence as "this
    // deployment does not support the lock" and fall back to the honest
    // advisory UI. Silence is the expected answer until the workbench ships
    // its half.
    const id = window.setTimeout(() => {
      setLockAcked((a) => (a === null ? false : a));
    }, LOCK_ACK_TIMEOUT_MS);
    return () => window.clearTimeout(id);
  }, [locked, lockAcked, sendLock]);

  // Sync status poll for the status strip; re-arms the watcher if it drops.
  // The button's enabled/disabled state reads straight off sync.fix_active —
  // set server-side the instant a fix spawns, cleared only by that run's own
  // completion (D336 follow-up), never guessed from transcript activity here.
  useEffect(() => {
    const id = window.setInterval(() => {
      void getSyncStatus(name)
        .then((s) => {
          setSync(s);
          setDir((d) => d ?? s.dir);
          // Self-heal: a server restart drops the watcher; re-arm it.
          if (!s.watching) void startSync(name).catch(() => undefined);
        })
        .catch(() => undefined);
    }, SYNC_POLL_MS);
    return () => window.clearInterval(id);
  }, [name]);

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
      setFixRunId(run_id);
      // Don't wait for the next poll (up to SYNC_POLL_MS away) to reflect
      // that a fix is now running — the gap let a double-click through to a
      // second POST (a 409, now that the server itself locks, but still a
      // confusing one to show right after a successful click).
      setSync((s) => (s ? { ...s, fix_active: true } : s));
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
  const enforced = lockAcked === true;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {locked && (
        <div
          style={{
            padding: "8px 12px",
            borderBottom: enforced
              ? "1px solid rgba(90,140,255,0.35)"
              : "1px solid rgba(210,150,40,0.45)",
            background: enforced
              ? "rgba(90,140,255,0.08)"
              : "rgba(210,150,40,0.10)",
            fontSize: 13,
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          <strong>{lockMessage(lockHold)}</strong>
          {enforced ? (
            <span style={{ opacity: 0.85 }}>
              — the workbench is read-only until it finishes.
            </span>
          ) : (
            // Never claim protection we do not have: without the ack the
            // workbench's own autosave timers are still running, and an
            // overlay cannot stop them.
            <span style={{ opacity: 0.85 }}>
              — please don’t edit it in the workbench. This version of the
              workbench can’t be locked, so changes made there may overwrite
              Claude’s work.
            </span>
          )}
        </div>
      )}
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
              disabled={fixBusy || !!sync.fix_active}
              onClick={() => void onFix()}
              title="Start a Claude session on the local clone, primed with these errors"
            >
              {sync.fix_active
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
            position: "relative",
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
          {locked && !enforced && (
            // Fallback for a workbench that never acked: block the clicks we
            // CAN block. This is a courtesy, not a guarantee — the workbench's
            // own autosave and its upstream auto-acknowledge run on timers
            // inside the frame and are untouched by an overlay. The banner
            // above says exactly that, so the scrim never reads as safety.
            <div
              data-testid="workbench-lock-scrim"
              title="Claude is editing this canvas"
              style={{
                position: "absolute",
                inset: 0,
                background: "rgba(20,20,25,0.45)",
                cursor: "not-allowed",
              }}
            />
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
              // key forces a REMOUNT when a fix run starts: a plain src update
              // on a mounted iframe can be shadowed by the embed shell's own
              // history rewrites, leaving the chat pane on its old URL — the
              // "have to reload the page to see the fix session" bug.
              key={fixRunId ?? "chat"}
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
