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
// Node selection (the explorer's embed route framed in the right pane): the same
// listener carries three more messages once the workbench is told this host
// speaks v2 (`fused_embed_v=2` on the frame URL — the flag also hides the
// workbench's own code editor, so an OLD host keeps that editor and an old
// WORKBENCH simply never sends a selection):
//   iframe → { type: "fused-embed-selection", udfName, backendId, kind }
//            on every change and once after ready; a null name (pane click,
//            folder node, multi-select) returns this pane to the canvas chat
//            (owner call: the right pane follows the canvas)
//   parent → { type: "fused-embed-select", udfName }  (restore ?node= on
//            load; null from the sidebar's close button, so both sides agree)
//   parent → { type: "fused-embed-remote-changed", push_seq }  after a push,
//            so the workbench re-fetches now instead of on its 10s poll.
// Keyed by UDF NAME, never a React Flow node id (those are regenerated every
// load): the node's source is `<clone>/<udfName>.py` (widgets: `.json`).
//
// Sync: the server watches the clone folder and pushes on every quiet period
// (local wins — an edit made inside the embedded workbench is overwritten by
// the next local push; the banner says so). This page never reloads the
// workbench iframe — the hosted workbench refreshes itself on upstream
// changes.
import { isBackendOff, useAgentBackends } from "@platform/lib/agent-backends";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChatMount } from "@apps/claude";
import { statPath } from "@platform/lib/api";
import { embedUrlForFsPath } from "@platform/lib/router";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
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

/** The selected node, as the workbench names it. `file` is the node's
 *  source file IN THE CLONE, basename only (`<name>.py`, or a widget's
 *  `.json` / `.md` / `.html`), as the workbench's selection message states
 *  it — the authority on which file a node is. */
interface NodeSel {
  udfName: string;
  kind: "udf" | "widget";
  file: string;
}

/** The node's source under the clone. */
function nodeFile(dir: string, node: NodeSel): string {
  return `${dir.replace(/\/+$/, "")}/${node.file}`;
}

/** Which of the explorer's content modes the node pane frames: the chat on
 *  the file, or the file's code. It is the `_mode` of the framed embed URL and
 *  rides this page's URL as `node_tab` so a reload keeps it. */
type NodeTab = "claude" | "code";

function nodeTabFromUrl(): NodeTab {
  return new URLSearchParams(window.location.search).get("node_tab") === "code" ? "code" : "claude";
}

/**
 * THE EXPLORER, HEADLESS, on the node's file (owner, 2026-10-17: "use embed —
 * that's the true headless thing"): the explorer's own embed route framed in
 * the right pane, chrome-free (no crumb bar, no sidebar column), with its
 * content mode set to the chat or the code. Same origin, so the chat inside
 * keeps its own file pane and files its sessions per FILE exactly as the
 * explorer does (the app-state block names the file); its `session_id` lives
 * on the frame's URL, not this page's.
 */
function nodeEmbedSrc(file: string, tab: NodeTab): string {
  return embedUrlForFsPath(file, "?_mode=" + tab + "&_side=off");
}

/** The node restored from this page's own URL (`?node=` + `node_file=`), or
 *  null. An older URL with `node=` alone is read as a UDF (`<name>.py`). */
function nodeFromUrl(): NodeSel | null {
  const q = new URLSearchParams(window.location.search);
  const name = q.get("node");
  if (!name) return null;
  const file = q.get("node_file") || `${name}.py`;
  return { udfName: name, kind: file.endsWith(".py") ? "udf" : "widget", file };
}

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
  // The selected node (null = the canvas chat). Seeded from `?node=` so a
  // reload keeps the node; written back below.
  const [node, setNode] = useState<NodeSel | null>(() => nodeFromUrl());
  const [nodeTab, setNodeTab] = useState<NodeTab>(() => nodeTabFromUrl());
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

  // Ask the workbench to select a node (or, with null, to deselect). Exact
  // targetOrigin, like every message to the frame.
  const sendSelect = useCallback((udfName: string | null) => {
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    frame.contentWindow.postMessage({ type: "fused-embed-select", udfName }, origin);
  }, []);

  // The node this page currently shows, readable from the message handler
  // without re-subscribing it on every selection.
  const nodeRef = useRef<NodeSel | null>(node);
  nodeRef.current = node;

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
        // Restore the node this page is on (a reload, or a frame that
        // reloaded): the workbench answers with a selection message, which is
        // what keeps the two sides in step — see the handler below.
        if (type === "fused-embed-auth-ready" && nodeRef.current) {
          sendSelect(nodeRef.current.udfName);
        }
      }
      if (type === "fused-embed-lock-ack") {
        setAckState((a) => nextAckState(a, "ack"));
      }
      if (type === "fused-embed-selection") {
        const name = event.data?.udfName;
        if (typeof name === "string" && name) {
          const kind = event.data?.kind === "widget" ? "widget" : "udf";
          const stated = event.data?.file;
          const file =
            typeof stated === "string" && stated && !stated.includes("/")
              ? stated
              : `${name}.${kind === "widget" ? "json" : "py"}`;
          setNode((cur) =>
            cur && cur.udfName === name && cur.file === file ? cur : { udfName: name, kind, file },
          );
        } else {
          // The canvas deselected (pane click, folder node, multi-select, or
          // the node is gone after a re-hydrate): follow it back to the
          // canvas chat. Owner call — the canvas is the one source of truth
          // for what the right pane is about.
          setNode(null);
        }
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [seedToken, sendLock, sendSelect, locked]);

  // Mirror the node onto this page's URL so a reload lands on it, and drop the
  // chat ids whenever the chat CHANGES: `session_id`/`run` belong to whichever
  // chat the URL names (one ChatMount at a time, both on "url" params), and
  // canvas → node, node → canvas and node → node are all a different chat.
  // The previous chat's session is one click away in its own Recent list.
  //
  // Every transition clears, not only node → node: with canvas → node left
  // alone, the node sidebar's fresh ChatMount read the canvas chat's
  // `session_id` off the URL and RESUMED it — a folder session shown under a
  // node, which the node's Recent list (filed by the transcript's leading
  // app-state `entry`, which a folder chat never has) could not find again
  // after the next remount. The mount itself is exempt: a reload lands on
  // `?node=` + `session_id` together and must resume that node's chat.
  const prevNodeKeyRef = useRef<string | null>(null);
  const nodeEffectRanRef = useRef(false);
  useEffect(() => {
    const key = node ? node.file : null;
    const url = new URL(window.location.href);
    if (node) {
      url.searchParams.set("node", node.udfName);
      url.searchParams.set("node_file", node.file);
      if (nodeTab === "code") url.searchParams.set("node_tab", "code");
      else url.searchParams.delete("node_tab");
    } else {
      url.searchParams.delete("node");
      url.searchParams.delete("node_file");
      url.searchParams.delete("node_tab");
    }
    if (nodeEffectRanRef.current && prevNodeKeyRef.current !== key) {
      url.searchParams.delete("session_id");
      url.searchParams.delete("run");
    }
    nodeEffectRanRef.current = true;
    prevNodeKeyRef.current = key;
    if (url.href !== window.location.href) {
      window.history.replaceState(window.history.state, "", url);
    }
  }, [node, nodeTab]);

  // After every push, tell the workbench to look upstream NOW — its own poll
  // is 10s, and the user just saved in the Code tab (or Claude just edited)
  // and is looking at the canvas for the result.
  const lastPushSeqRef = useRef<number | null>(null);
  useEffect(() => {
    const seq = sync?.push_seq ?? null;
    if (seq === null) return;
    const prev = lastPushSeqRef.current;
    lastPushSeqRef.current = seq;
    if (prev === null || seq <= prev) return;
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    frame.contentWindow.postMessage(
      { type: "fused-embed-remote-changed", push_seq: seq },
      origin,
    );
  }, [sync?.push_seq]);

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

  // Right pane: the Claude chat over the local clone, chat-only — the chat's
  // own left preview would be a second canvas beside the live workbench. Shown
  // once the clone exists and the registry says a chat serves it.
  const chatHere = !!(dir && chatTpl);

  // Hand a fresh fix run to the chat: `run` goes on this page's own URL (the
  // chat's "url" params store reads it — the chat adopts the session and then
  // clears the param itself), and the key remount below makes the chat boot
  // and see it.
  useEffect(() => {
    if (!fixRunId) return;
    const url = new URL(window.location.href);
    url.searchParams.set("run", fixRunId);
    window.history.replaceState(window.history.state, "", url);
  }, [fixRunId]);

  useAgentBackends();
  const claudeOff = isBackendOff("claude");
  const onFix = async () => {
    setFixBusy(true);
    setFixError(null);
    try {
      const { run_id } = await fixWithClaude(name);
      // The fix runs on the CLONE (canvases.py spawns on the dir), so it is
      // the canvas chat's to show: leave the node, on both sides.
      if (nodeRef.current) {
        sendSelect(null);
        setNode(null);
      }
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
      ? `${baseUrl}/workbench/${encodeURIComponent(handle)}/${encodeURIComponent(name)}?fused_embed_auth=1&fused_embed_v=2`
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
            {/* Claude's feature (D17): no door while Claude is switched off. */}
            {!claudeOff && <button
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
            </button>}
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
              /* NO `data-fused-annotate-target` here, deliberately: the canvas
                 chat gets this frame as a getter (`annotateTarget` below), and
                 the explorer engine in the right pane finds its own content
                 frame by that attribute — a marked workbench frame earlier in
                 the DOM would win the query, a cross-origin frame the file
                 chat can neither read nor pin into. */
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
            display: "flex",
            flexDirection: "column",
            pointerEvents: dragging ? "none" : "auto",
          }}
        >
          {chatHere && dir && node ? (
            // A node is selected: the explorer, open on the node's file —
            // its preview (the code template, writable) and its Claude / Git
            // sidebar, per-file sessions and all. Keyed per file so a node
            // switch is a fresh open.
            <div
              key={nodeFile(dir, node)}
              className="canvas-node-file"
              style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}
            >
              {/* The framed embed's content mode, as two tabs — the same shadcn
                  line strip the explorer's own sidebar uses (SideChrome). The
                  strip is this page's; everything under it is the explorer's. */}
              <div className="preview-side-header">
                <span className="canvas-node-side-name" title={nodeFile(dir, node)}>
                  {node.udfName}
                </span>
                <div className="side-header-tail">
                  <Tabs
                    value={nodeTab}
                    onValueChange={(v) => {
                      if (v === "claude" || v === "code") setNodeTab(v);
                    }}
                    className="side-tabs self-stretch"
                  >
                    <TabsList
                      variant="line"
                      aria-label="Node panel"
                      className="h-full justify-start gap-3 rounded-none border-b-0 p-0"
                    >
                      <TabsTrigger value="claude" className="flex-none px-2 text-[12px]">
                        Claude
                      </TabsTrigger>
                      <TabsTrigger value="code" className="flex-none px-2 text-[12px]">
                        Code
                      </TabsTrigger>
                    </TabsList>
                  </Tabs>
                </div>
              </div>
              <iframe
                // Keyed on the mode so a tab switch is a fresh document: the
                // chat's own `session_id` lives on the frame URL, and the
                // explorer's content-mode swap is for its own switcher.
                key={nodeTab}
                className="preview-side-frame"
                src={nodeEmbedSrc(nodeFile(dir, node), nodeTab)}
                title={`${node.udfName} — ${nodeTab === "code" ? "Code" : "Claude"}`}
              />
            </div>
          ) : chatHere && dir && !node ? (
            // key forces a REMOUNT when a fix run starts: the run id is read at
            // BOOT (the chat adopts the session and clears the param), so an
            // already-running chat has to be rebooted to see it — the "have to
            // reload the page to see the fix session" bug.
            //
            // `run` and `session_id` live on the `/canvases/<name>` URL, which is
            // why `paramsSource` is "url" and why the effect above writes `run`
            // there. It is also handed over directly as `runId`, so the mount
            // does not depend on that write having landed first.
            //
            // The annotate target is the SIBLING workbench iframe; the host
            // hands the element over. The mark stays on that iframe for PR3 to
            // read (below).
            <ChatMount
              key={fixRunId ?? "chat"}
              file={dir}
              chatOnly
              paramsSource="url"
              {...(fixRunId ? { runId: fixRunId } : {})}
              annotateTarget={() => frameRef.current}
            />
          ) : (
            !error && <p style={{ padding: 16 }}>Loading editor…</p>
          )}
        </div>
      </div>
    </div>
  );
}
