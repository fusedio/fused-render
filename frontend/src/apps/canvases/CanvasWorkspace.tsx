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
import { statPath } from "@platform/lib/api";
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
import {
  decideLock,
  lockMessage,
  nextAckState,
  type AckState,
  type LockHold,
} from "./canvas-lock-lib";

const SYNC_POLL_MS = 2000;

// A status poll that keeps failing must not strand a lock ON forever. The
// decide effect below is keyed on `sync`, and a caught poll failure used to
// leave `sync` untouched — so a lock in force at the moment polling started
// failing was never re-evaluated again at all (finding 6). After this many
// consecutive misses, the canvas is treated as not-watched (same shape as a
// dropped watcher), which both releases the lock and re-arms the self-heal.
const FAILED_POLLS_BEFORE_RELEASE = 3;

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

// How long the lock is HELD after a sync operation has settled.
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
//
// It is armed by any hold ending, not just a push's: the two legs of one publish
// (pull, then push) and several publishes in a row otherwise unlock and relock
// the pane once each, which the user sees as a flicker. One window spanning them
// makes it one engagement. See canvas-lock-lib's header for the accepted cost.
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
  // The claude template's path, from the clone dir's own stat — the same
  // resolution the explorer's sidebar uses, so a user override (§16) wins here
  // too. Needed because the chat is framed DIRECTLY via /render (below), not
  // through /explorer/embed.
  const [chatTpl, setChatTpl] = useState<string | null>(null);
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
  // Lock state. `hold` is why we are locked. `ack` is whether the WORKBENCH
  // confirmed it can enforce it for the CURRENT engagement — reset to
  // "waiting" every time a new engagement starts (canvas-lock-lib's
  // `nextAckState`), never sticky across engagements: enforcement belongs to
  // the deployed workbench, and this page assumes nothing about it it hasn't
  // just been told for THIS lock.
  const [lockHold, setLockHold] = useState<LockHold | null>(null);
  const [ackState, setAckState] = useState<AckState>("waiting");
  // The hold this same decision returned last time — read synchronously (not
  // via the `lockHold` state, which only updates after a render) so the grace
  // window can tell "a hold just ended" from "nothing was holding" (see
  // decideLock's `prevHold`).
  const prevHoldRef = useRef<LockHold | null>(null);
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
        if (cancelled) return;
        setDir(started.dir);
        const st = await statPath(started.dir);
        if (!cancelled) {
          setChatTpl(
            st.templates?.find((t) => t.mode === "claude")?.path ?? null,
          );
        }
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

  const locked = lockHold !== null;

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== baseOriginRef.current) return;
      const type = event.data?.type;
      if (type === "fused-embed-auth-ready" || type === "fused-embed-auth-refresh") {
        void seedToken();
        // Re-assert the lock on (re)load: a workbench that reloaded mid-lock
        // comes back editable otherwise, and this is the one message that tells
        // us a fresh frame is listening.
        if (locked) sendLock(true);
      }
      if (type === "fused-embed-lock-ack") {
        setAckState((a) => nextAckState(a, "ack"));
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [seedToken, sendLock, locked]);

  // Decide whether the workbench should be locked, from the polled status. The
  // rule itself lives in canvas-lock-lib (pure, and tested — the release half
  // is correctness-critical). Re-runs on every poll, which is what advances the
  // grace window.
  useEffect(() => {
    const now = Date.now();
    const decision = decideLock(
      sync,
      prevHoldRef.current,
      settledAtRef.current,
      now,
      LOCK_RELEASE_GRACE_MS,
    );
    prevHoldRef.current = decision.hold;
    settledAtRef.current = decision.settledAt;
    setLockHold(decision.hold);
    if (decision.hold !== "settling" || decision.settledAt === null) return;
    // The next poll would get there anyway; this just releases on time rather
    // than up to SYNC_POLL_MS late.
    const remaining = decision.settledAt + LOCK_RELEASE_GRACE_MS - now;
    const id = window.setTimeout(() => {
      setLockHold((h) => {
        if (h !== "settling") return h;
        prevHoldRef.current = null;
        return null;
      });
    }, Math.max(0, remaining));
    return () => window.clearTimeout(id);
  }, [sync]);

  // Push each lock transition to the workbench, and — enforcement belongs to
  // the WORKBENCH, this page only asks and observes — probe whether THIS
  // engagement can actually be enforced. Reset per engagement (`locked`
  // flipping false→true), so the fallback scrim this drives defaults to "on"
  // for a brand new lock rather than inheriting a previous engagement's answer.
  //
  // No "same episode" exemption: the reset is unconditional. What removes the
  // mid-publish flicker is decideLock's coalescing (consecutive operations stay
  // inside one engagement, so this effect does not re-run), and an engagement
  // that DOES start fresh — after a full release, a dropped watcher, or a
  // failed push the user then fixes — may be facing a frame that reloaded in
  // between. Inheriting "acked" there would show a lock with nothing enforcing
  // it and no fallback scrim either.
  useEffect(() => {
    sendLock(locked);
    if (!locked) return;
    setAckState((a) => nextAckState(a, "engage"));
    // No ack yet: give the workbench a moment, then treat silence as "this
    // deployment does not support the lock" and fall back to the translucent
    // scrim. Silence is the expected answer until the workbench ships its
    // half; an ack arriving after this fires still upgrades to pass-through
    // (nextAckState's "ack" always wins).
    const id = window.setTimeout(() => {
      setAckState((a) => nextAckState(a, "timeout"));
    }, LOCK_ACK_TIMEOUT_MS);
    return () => window.clearTimeout(id);
  }, [locked, sendLock]);

  // Sync status poll for the status strip; re-arms the watcher if it drops.
  // The button's enabled/disabled state reads straight off sync.fix_active —
  // set server-side the instant a fix spawns, cleared only by that run's own
  // completion (D336 follow-up), never guessed from transcript activity here.
  useEffect(() => {
    let consecutiveFailures = 0;
    const id = window.setInterval(() => {
      void getSyncStatus(name)
        .then((s) => {
          consecutiveFailures = 0;
          setSync(s);
          setDir((d) => d ?? s.dir);
          // Self-heal: a server restart drops the watcher; re-arm it.
          if (!s.watching) void startSync(name).catch(() => undefined);
        })
        .catch(() => {
          consecutiveFailures += 1;
          // A repeatedly failing poll must not strand a lock ON forever
          // (finding 6): the decide effect is keyed on `sync`, and a caught
          // failure otherwise leaves `sync` — and therefore the lock —
          // frozen at whatever it was when polling started failing, with
          // nothing left to ever re-evaluate it. After a few misses, treat
          // the canvas the same as a dropped watcher: decideLock's own
          // `!status.watching` branch releases it, and the next successful
          // poll's `if (!s.watching)` re-arms sync the normal way.
          if (consecutiveFailures >= FAILED_POLLS_BEFORE_RELEASE) {
            setSync((s) => (s ? { ...s, watching: false } : s));
          }
        });
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

  // Right pane: the Claude chat over the local clone, framed DIRECTLY via
  // /render — the same construction the explorer sidebar uses (Preview.tsx
  // sideSrcFor) and for the same reason `chat_only=1` rides along: the
  // template's own left preview would be a second canvas beside the live
  // workbench. Direct rather than through /explorer/embed because the
  // annotate-target contract is parent-scoped: the template looks for
  // `data-fused-annotate-target` in `window.parent.document`, and only a
  // sibling iframe (the workbench, marked below) satisfies that — one frame
  // deeper and the mark is invisible to it.
  //
  // The `run` param does NOT ride this src: the template reads it through
  // fused.params, i.e. off THIS page's URL (it sets no param boundary), so a
  // fix run is handed over by writing `run` there — see the effect below.
  const editorSrc =
    dir && chatTpl
      ? `/render?path=${encodeURIComponent(chatTpl)}&_file=${encodeURIComponent(dir)}&chat_only=1`
      : null;

  // Hand a fresh fix run to the chat: `run` goes on this page's own URL (where
  // fused.params reads it — the template adopts the session and then clears
  // the param itself), and the iframe's key remount below makes the template
  // boot and see it.
  useEffect(() => {
    if (!fixRunId) return;
    const url = new URL(window.location.href);
    url.searchParams.set("run", fixRunId);
    window.history.replaceState(window.history.state, "", url);
  }, [fixRunId]);

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
  // Whether THIS engagement's ack has arrived. Enforcement lives in the
  // workbench: acked means it will refuse edits and allow pan/zoom on its
  // own, so this page renders no blocking scrim at all — only the banner,
  // with pointer-events: none, so a click always reaches the iframe beneath
  // it. Unacked (still waiting, or the ack window elapsed with nothing) is
  // the ONLY case that falls back to a blocking overlay, and even then it
  // stays a light, translucent scrim — the canvas must stay visible and, per
  // the banner's own wording, this is a courtesy, not a guarantee.
  const enforced = ackState === "acked";

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
            // This bar never overlaps the panes below it, but it carries no
            // interactive elements either way — pointer-events: none end to
            // end, so nothing here can ever be the thing standing between a
            // click and the iframe.
            pointerEvents: "none",
          }}
        >
          <strong>{lockMessage(lockHold)}</strong>
          {enforced ? (
            <span style={{ opacity: 0.85 }}>
              — the workbench enforces this itself; pan and zoom still work.
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
              /* The annotate-target mark, same contract as Preview.tsx's: the
                 claude chat beside this frame finds it via parent.document and
                 aims its comment mode here. The workbench is cross-origin, so
                 the template's own overlay (its annXO branch) draws point
                 notes over this frame's box rather than inside its DOM. */
              data-fused-annotate-target=""
              style={{ width: "100%", height: "100%", border: 0 }}
            />
          ) : (
            !error && <p style={{ padding: 16 }}>Loading workbench…</p>
          )}
          {locked && !enforced && (
            // Fallback for a workbench that hasn't (yet, or ever) acked this
            // engagement: block the clicks we CAN block. This is a courtesy,
            // not a guarantee — the workbench's own autosave and its upstream
            // auto-acknowledge run on timers inside the frame and are
            // untouched by an overlay. Deliberately a LIGHT, translucent
            // scrim (not the near-black 45% this used to be) — the canvas
            // must stay visible underneath, and the banner above already
            // says this is only a courtesy.
            <div
              data-testid="workbench-lock-scrim"
              title={lockMessage(lockHold)}
              style={{
                position: "absolute",
                inset: 0,
                background: "rgba(20,20,25,0.14)",
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
              // key forces a REMOUNT when a fix run starts: the template only
              // reads `run` at boot (it adopts the session and clears the
              // param), so an already-running chat has to be rebooted to see
              // it — the "have to reload the page to see the fix session" bug.
              key={fixRunId ?? "chat"}
              src={editorSrc}
              /* The chat's tab-capture screenshots (template annXO branch,
                 D355) call getDisplayMedia from inside this frame, and
                 display capture is gated by Permissions Policy — whether a
                 same-origin iframe inherits it without an explicit allow
                 varies by browser, so it is granted here rather than hoped
                 for. */
              allow="display-capture"
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
