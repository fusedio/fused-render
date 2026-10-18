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
// speaks v2 or later (`fused_embed_v=3` on the frame URL — the flag also hides the
// workbench's own code editor, so an OLD host keeps that editor and an old
// WORKBENCH simply never sends a selection):
//   iframe → { type: "fused-embed-selection", udfName, backendId, kind }
//            on every change and once after ready; a null name (pane click,
//            folder node, multi-select) returns this pane to the canvas chat
//            (owner call: the right pane follows the canvas)
//   parent → { type: "fused-embed-select", udfName }  (restore ?node= on
//            load; null from the sidebar's close button, so both sides agree)
//   parent → { type: "fused-embed-pushed", push_seq }  after a push, for the
//            workbench's save chip ("Published"); it triggers no reload.
//            (`fused-embed-remote-changed` is still sent beside it for a
//            workbench build from before the delta leg — see the effect.)
// Keyed by UDF NAME, never a React Flow node id (those are regenerated every
// load): the node's source is `<clone>/<udfName>.py` (widgets: `.json`).
//
// Sync (embed v3, canvas_snapshot.py): the embedded workbench never saves to
// the server. On its autosave cadence it posts the body it would have saved,
// minus code, as
//   iframe → { type: "fused-embed-snapshot", gen, seq, body }
// which this page relays to POST /api/canvases/snapshot; the answer goes back
// as fused-embed-snapshot-applied / -rejected. The clone watcher is then the
// ONE writer to the server, publishing the folder exactly as it publishes a
// Claude edit — so there is no second writer to lock out, and the left-pane
// lock this page used to run (decideLock, the ack handshake, the scrim, the
// grace window) is gone with it.
//
// The other direction (canvas_snapshot.py, "the disk → embed leg"): this page
// never reloads the workbench iframe and never asks it to re-fetch. When the
// clone moves on disk (the status poll's `disk_rev`) it fetches
// GET /api/canvases/delta — diff(disk, what the server believes the frame
// holds) — and posts it as
//   parent → { type: "fused-embed-delta", rev, base_rev, full, ops }
//   iframe → { type: "fused-embed-delta-applied", rev } | "-rejected"
// relaying the ack to POST /api/canvases/delta/ack. The frame patches its
// nodes in place: camera, selection, results and the right pane's session
// all survive. A frame that just hydrated says
//   iframe → { type: "fused-embed-hello" }
// and gets a full state (`full=1`). The frame's own edits never come back
// this way: after a snapshot lands the server re-reads disk as what the
// frame holds, so the diff is empty.
import { isBackendOff, useAgentBackends } from "@platform/lib/agent-backends";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChatMount } from "@apps/claude";
import { statPath } from "@platform/lib/api";
import { EMBED_PREFIX, VIEW_PREFIX, embedUrlForFsPath, navigateUrl } from "@platform/lib/router";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  ackDelta,
  applyRename,
  applySnapshot,
  fixWithClaude,
  getDelta,
  getAccessToken,
  getCanvasesStatus,
  getSyncStatus,
  getWhoami,
  startSync,
  type SyncStatus,
} from "./api";

const SYNC_POLL_MS = 2000;

// A snapshot that met a busy clone (409) is retried this many times, this far
// apart — long enough to outlast a push, short enough that "Saving…" on the
// workbench's chip stays honest.
// A 409 (`busy`: a push or pull holds the clone's lock; `no_watcher`: the
// watcher is being re-armed) is retried for up to this long. A snapshot the
// embed sees REJECTED is dropped on its side, so a busy spell must never end
// in a rejection; past the window the relay gives up silently and the embed
// keeps the snapshot queued for its next change or reload.
const SNAPSHOT_BUSY_WINDOW_MS = 120_000;
const SNAPSHOT_BUSY_RETRY_MS = 750;

// A delta the server HELD (the clone mid-write: a row whose file is not there
// yet) or could not cut (409 busy) is asked for again this far apart, for up
// to this long; past that the next disk move or hello asks afresh.
const DELTA_RETRY_MS = 1_000;
const DELTA_RETRY_WINDOW_MS = 30_000;

// A status poll that keeps failing is treated, after this many consecutive
// misses, as a dropped watcher (`watching: false`), which is what re-arms the
// self-heal on the next successful poll.
const FAILED_POLLS_BEFORE_RELEASE = 3;


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
function nodeEmbedSrc(file: string, tab: NodeTab, sessionId: string | null): string {
  // `_noann=1` on the chat: its Screenshot / Annotate seats and view toggle
  // would open the chat's own preview of the file, which is the Code tab's
  // job here (owner, 2026-10-17). `session_id` when a Recent row on the
  // canvas chat hopped here: the frame's chat resumes that conversation.
  return embedUrlForFsPath(
    file,
    "?_mode=" + tab + "&_side=off" +
      (tab === "claude" ? "&_noann=1" : "") +
      (sessionId ? "&session_id=" + encodeURIComponent(sessionId) : ""),
  );
}

/** The clone file a chat hop names, when it is one of this canvas's nodes.
 *  A Recent row on the canvas chat that is a FILE chat navigates to the
 *  explorer's view of that file (`paneChatUrl`: `/explorer/view/<path>?_side=
 *  claude&session_id=…`); here that file is a node, so the hop becomes
 *  "select the node, open that chat". Null for anything else — a folder chat,
 *  a file outside the clone, a nested path — which navigates as it always did. */
function nodeHopFromUrl(url: string, dir: string): { node: NodeSel; sessionId: string | null } | null {
  let parsed: URL;
  try {
    parsed = new URL(url, window.location.origin);
  } catch {
    return null;
  }
  const prefix = [VIEW_PREFIX, EMBED_PREFIX].find((p) => parsed.pathname.startsWith(p));
  if (!prefix) return null;
  const fsPath =
    "/" +
    parsed.pathname
      .slice(prefix.length)
      .split("/")
      .filter((seg) => seg.length > 0)
      .map(decodeURIComponent)
      .join("/");
  const base = dir.replace(/\/+$/, "") + "/";
  if (!fsPath.startsWith(base)) return null;
  const file = fsPath.slice(base.length);
  if (!file || file.includes("/")) return null;
  const dot = file.lastIndexOf(".");
  const udfName = dot > 0 ? file.slice(0, dot) : file;
  return {
    node: { udfName, kind: file.endsWith(".py") ? "udf" : "widget", file },
    sessionId: parsed.searchParams.get("session_id"),
  };
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
  // A session the node pane's chat should resume (a Recent row's hop, below),
  // keyed to the file it belongs to: it applies only while that node is the
  // one shown, so no clearing is needed when the node changes.
  const [nodeSession, setNodeSession] = useState<{ file: string; sessionId: string } | null>(null);
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

  // Ask the workbench to select a node (or, with null, to deselect). Exact
  // targetOrigin, like every message to the frame.
  const sendSelect = useCallback((udfName: string | null) => {
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    frame.contentWindow.postMessage({ type: "fused-embed-select", udfName }, origin);
  }, []);

  // One embed snapshot → the clone, answered to the frame. A 409 is retried
  // here (SNAPSHOT_BUSY_WINDOW_MS), because the embed only re-posts on its
  // next change or reload and would otherwise sit on "Saving…" for a
  // snapshot nothing was wrong with; a retry is abandoned the moment a newer
  // relay is queued behind it, since that one carries the full state anyway.
  // Anything else is a rejection the embed drops, reason attached.
  // Relays run strictly IN ORDER through one promise chain: the frame posts
  // seq 1, 2, 3 as it autosaves, and a 409 retry on seq 1 must not let seq 2
  // reach the clone first — the server would then read seq 1's older full
  // state as deletions of seq 2's nodes. Same chain for the rename hint,
  // which must land before the snapshot carrying the new name.
  const relayChainRef = useRef<Promise<void>>(Promise.resolve());
  const relayTicketRef = useRef(0);
  const relaySnapshot = useCallback(
    async (
      gen: number,
      seq: number,
      body: Record<string, unknown> | null,
      rename: { from: string; to: string; file_from?: string | null; file_to?: string | null } | null = null,
    ) => {
      const post = (msg: Record<string, unknown>) => {
        const frame = frameRef.current;
        const origin = baseOriginRef.current;
        if (!frame?.contentWindow || !origin) return;
        frame.contentWindow.postMessage(msg, origin);
      };
      const ticket = relayTicketRef.current;
      const started = Date.now();
      for (;;) {
        try {
          const ack = rename
            ? await applyRename(name, gen, seq, rename)
            : await applySnapshot(name, gen, seq, body ?? {});
          post({ type: "fused-embed-snapshot-applied", gen, seq, push_seq: ack.push_seq });
          return;
        } catch (e) {
          const status = (e as { status?: number }).status;
          if (status === 409) {
            if (relayTicketRef.current !== ticket) return; // a newer relay waits
            if (Date.now() - started > SNAPSHOT_BUSY_WINDOW_MS) return; // still queued on the embed
            await new Promise((r) => window.setTimeout(r, SNAPSHOT_BUSY_RETRY_MS));
            continue;
          }
          post({
            type: "fused-embed-snapshot-rejected",
            gen,
            seq,
            reason: (e as Error).message || "snapshot rejected",
          });
          return;
        }
      }
    },
    [name],
  );

  // The delta leg: disk moved → fetch diff(disk, what the frame holds) → post
  // it to the frame. One relay at a time, in order, through a promise chain
  // (a full state must not overtake the delta cut before it). A held or busy
  // answer is retried on a timer; the frame's hello asks for a full state.
  const deltaChainRef = useRef<Promise<void>>(Promise.resolve());
  const lastDeltaRevRef = useRef<number | null>(null);
  const relayDelta = useCallback(
    (full: boolean) => {
      deltaChainRef.current = deltaChainRef.current.then(async () => {
        const started = Date.now();
        for (;;) {
          try {
            const delta = await getDelta(name, full);
            if (delta.held !== undefined) {
              if (Date.now() - started > DELTA_RETRY_WINDOW_MS) return;
              await new Promise((r) => window.setTimeout(r, DELTA_RETRY_MS));
              continue;
            }
            lastDeltaRevRef.current = Math.max(lastDeltaRevRef.current ?? 0, delta.rev);
            const ops = delta.ops ?? [];
            // Nothing to apply against a known base: the server moved its
            // picture itself; nothing to post.
            if (!delta.full && ops.length === 0) return;
            const frame = frameRef.current;
            const origin = baseOriginRef.current;
            if (!frame?.contentWindow || !origin) {
              // No frame to tell: drop the server's picture so the next ask
              // (the frame's hello) is a full state.
              void ackDelta(name, delta.rev, false).catch(() => undefined);
              return;
            }
            frame.contentWindow.postMessage(
              {
                type: "fused-embed-delta",
                rev: delta.rev,
                base_rev: delta.base_rev ?? null,
                full: Boolean(delta.full),
                ops,
              },
              origin,
            );
            return;
          } catch (e) {
            const status = (e as { status?: number }).status;
            if (status === 409 && Date.now() - started <= DELTA_RETRY_WINDOW_MS) {
              await new Promise((r) => window.setTimeout(r, DELTA_RETRY_MS));
              continue;
            }
            return;
          }
        }
      });
    },
    [name],
  );
  useEffect(() => {
    const rev = sync?.disk_rev;
    if (typeof rev !== "number") return;
    if (lastDeltaRevRef.current !== null && rev <= lastDeltaRevRef.current) return;
    if (lastDeltaRevRef.current === null) {
      // First sight of the counter: nothing to diff against yet. The frame's
      // hello (after it hydrates) asks for the full state.
      lastDeltaRevRef.current = rev;
      return;
    }
    relayDelta(false);
  }, [sync?.disk_rev, relayDelta]);

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
        // Restore the node this page is on (a reload, or a frame that
        // reloaded): the workbench answers with a selection message, which is
        // what keeps the two sides in step — see the handler below.
        if (type === "fused-embed-auth-ready" && nodeRef.current) {
          sendSelect(nodeRef.current.udfName);
        }
      }
      if (type === "fused-embed-snapshot") {
        const { gen, seq, body } = (event.data ?? {}) as {
          gen?: unknown; seq?: unknown; body?: unknown;
        };
        if (typeof gen !== "number" || typeof seq !== "number" || !body || typeof body !== "object") return;
        relayTicketRef.current += 1;
        relayChainRef.current = relayChainRef.current.then(() =>
          relaySnapshot(gen, seq, body as Record<string, unknown>),
        );
      }
      if (type === "fused-embed-rename") {
        // The rename hint: posted the moment a UDF is renamed, before any
        // snapshot carries the new name, so disk follows the name (file, row,
        // edges) instead of reading the next snapshot as delete + add.
        const { gen, seq, from, to, file_from, file_to } = (event.data ?? {}) as {
          gen?: unknown; seq?: unknown; from?: unknown; to?: unknown;
          file_from?: unknown; file_to?: unknown;
        };
        if (typeof gen !== "number" || typeof seq !== "number") return;
        if (typeof from !== "string" || typeof to !== "string" || !from || !to) return;
        relayTicketRef.current += 1;
        relayChainRef.current = relayChainRef.current.then(() =>
          relaySnapshot(gen, seq, null, {
            from,
            to,
            file_from: typeof file_from === "string" ? file_from : null,
            file_to: typeof file_to === "string" ? file_to : null,
          }),
        );
      }
      if (type === "fused-embed-hello") {
        // The frame (re)hydrated from the server: bring it to disk with a
        // full state, whatever the server thought it held before.
        relayDelta(true);
      }
      if (type === "fused-embed-delta-applied" || type === "fused-embed-delta-rejected") {
        const rev = event.data?.rev;
        if (typeof rev !== "number") return;
        void ackDelta(name, rev, type === "fused-embed-delta-applied").catch(() => undefined);
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
  }, [seedToken, sendSelect, relaySnapshot, relayDelta, name]);

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

  // After every push, tell the workbench's save chip it is published. This
  // is informational only — the frame never reloads or re-fetches on it (the
  // delta leg below is how disk changes reach it).
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
    frame.contentWindow.postMessage({ type: "fused-embed-pushed", push_seq: seq }, origin);
    // COMPAT: a workbench build from before the delta leg (application
    // #8298) has no other way to learn about a push — without this it would
    // sit on stale state for good. A build with the delta leg does not relay
    // `remote-changed` at all, so sending both is harmless. Drop this once
    // #8298 is live on every environment this app points at.
    frame.contentWindow.postMessage(
      { type: "fused-embed-remote-changed", push_seq: seq },
      origin,
    );
  }, [sync?.push_seq]);


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
          // After a few misses, treat the canvas the same as a dropped
          // watcher so the next successful poll's `if (!s.watching)` re-arms
          // sync the normal way.
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
      ? `${baseUrl}/workbench/${encodeURIComponent(handle)}/${encodeURIComponent(name)}?fused_embed_auth=1&fused_embed_v=3`
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
                src={nodeEmbedSrc(
                  nodeFile(dir, node),
                  nodeTab,
                  nodeSession && nodeSession.file === node.file ? nodeSession.sessionId : null,
                )}
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
              // A Recent row that is a FILE chat on one of this canvas's nodes
              // does not leave for the explorer: it selects the node in the
              // workbench and opens that chat in the node pane. Everything
              // else hops as the chat always did.
              onNavigate={(url) => {
                const hop = nodeHopFromUrl(url, dir);
                if (!hop) {
                  navigateUrl(url);
                  return;
                }
                sendSelect(hop.node.udfName);
                setNodeTab("claude");
                setNodeSession(hop.sessionId ? { file: hop.node.file, sessionId: hop.sessionId } : null);
                setNode(hop.node);
              }}
            />
          ) : (
            !error && <p style={{ padding: 16 }}>Loading editor…</p>
          )}
        </div>
      </div>
    </div>
  );
}
