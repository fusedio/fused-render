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
//
// Focus. The canvas gates pan/zoom on its window being focused (a background
// browser window must not half-react), and a framed document's own
// `hasFocus()` is false until something INSIDE the frame takes focus — so the
// canvas was dead until clicked, and dead again after every click into the
// chat. Two layers fix that:
//   host → { type: "fused-embed-host-focus", focused }  on this window's
//     focus/blur/visibility changes and in reply to -auth-ready and -hello;
//     the canvas counts the host window's focus as its own.
//   The host also hands the frame real focus when the pointer ENTERS the
//     canvas pane (keyboard follows the hover: Space-pan, Delete, Cmd+Z),
//     unless the user is typing somewhere — a wandering mouse must not pull
//     keystrokes out of the chat onto canvas hotkeys. The chat never takes
//     focus on hover: a text surface is clicked into.
//
// The node pane's chat (owner, 2026-10-19: "when I switch from one node to
// another, each time I get the new claude session screen"). Selecting a node
// frames the explorer's embed route on its file, and that chat files its
// `session_id` on the FRAME's URL; the frame goes with the node, so the next
// node, and this node again, booted on a fresh chat. `nodeSessions` below
// remembers, per canvas and node, the session the frame was last on —
// mirrored off the frame's own URL (same origin) as it changes — and the next
// frame on that node boots on it. In memory only: a reload starts over.
import { isBackendOff, useAgentBackends } from "@platform/lib/agent-backends";
import { useCallback, useEffect, useRef, useState } from "react";
import type { SyntheticEvent } from "react";
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
  pullCanvas,
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

// A just-added node's file reaches disk with the next autosave snapshot
// (debounce + relay, normally ~1–2 s). The node pane waits for it this often,
// for at most this long.
const NODE_FILE_PROBE_MS = 400;
const NODE_FILE_WAIT_MS = 20_000;

// A status poll that keeps failing is treated, after this many consecutive
// misses, as a dropped watcher (`watching: false`), which is what re-arms the
// self-heal on the next successful poll.
const FAILED_POLLS_BEFORE_RELEASE = 3;

/** This window's focus as the canvas should count it. */
function hostFocused(): boolean {
  return document.visibilityState === "visible" && document.hasFocus();
}

/**
 * Whether the user is typing: the active element, followed down through
 * same-origin frames (the right pane's explorer embed nests the chat one
 * level deeper), is a text field or contenteditable. A cross-origin frame
 * (the canvas itself) cannot be seen into and reads as not typing.
 *
 * No `instanceof` here: an element from a child document belongs to that
 * frame's realm, where `HTMLIFrameElement`/`HTMLElement` are different
 * constructors — the walk would stop one level down (the explorer embed's
 * own frames, the Code tab's CodeMirror) and read the typing user as idle.
 */
function activeElementIsEditable(doc: Document): boolean {
  let el: Element | null = doc.activeElement;
  for (let depth = 0; el && depth < 8; depth++) {
    if (el.tagName === "IFRAME") {
      let inner: Document | null = null;
      try {
        inner = (el as HTMLIFrameElement).contentDocument;
      } catch {
        inner = null;
      }
      if (!inner) return false;
      el = inner.activeElement;
      continue;
    }
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    return (el as HTMLElement).isContentEditable === true;
  }
  return false;
}


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

/**
 * THE SESSION EACH NODE'S CHAT WAS LAST ON, per canvas: `<canvas>\0<file>` →
 * `session_id`. Module-level so it outlives this component, which App.tsx
 * keys on the canvas slug (a hop to another canvas and back must not forget).
 * The chat inside the node pane runs on the frame's URL (`paramsSource` "url"
 * in the explorer's embed), so the frame's `location.search` IS the chat's
 * state: `session_id` present = that conversation, absent = the chat's home
 * (New chat was pressed, or nothing has been sent). The entry mirrors that —
 * set or deleted — rather than accumulating, so coming back after New chat
 * lands on the fresh chat the user asked for, not the old session.
 */
const nodeSessions = new Map<string, string>();

function nodeSessionKey(canvas: string, file: string): string {
  return canvas + "\0" + file;
}

/** Mirror the node frame's current `session_id` into `nodeSessions`. The
 *  chat's URL store coalesces history writes (≤1 per 400 ms) but announces
 *  every write as `fused:urlchange`, and the flush itself goes through the
 *  shell's wrapped `replaceState`, which announces again — so listening on
 *  the frame window and reading its location converges on what landed,
 *  including the `pagehide` flush as the frame navigates away. */
function recordNodeSession(win: Window, key: string, pathname: string): void {
  let search: string;
  try {
    // Only while the frame is still on the node's own document: the embed
    // shell routes client-side, and a chat that had navigated the frame
    // elsewhere would file that page's session under this node.
    if (win.location.pathname !== pathname) return;
    search = win.location.search;
  } catch {
    return;
  }
  const sid = new URLSearchParams(search).get("session_id");
  if (sid) nodeSessions.set(key, sid);
  else nodeSessions.delete(key);
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
  // Bumped when the node pane must REBOOT on a session for the node already
  // shown (a Recent row's hop onto the selected node, below): the frame's src
  // is otherwise frozen per node and tab (`nodeBootSrc`).
  const [nodeBootSeq, setNodeBootSeq] = useState(0);
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
  // "Server changes · Pull": the hosted workbench's chip, for this tab. The
  // watcher holds its push while `sync.server_stale` is set; this button is
  // the user's pull. The result reaches the embed as a delta like any other
  // disk change.
  const [pullBusy, setPullBusy] = useState(false);
  const [pullError, setPullError] = useState<string | null>(null);

  // The node pane frames the node's FILE, and a node the user just added in
  // the embed is selected the instant it exists there — a second or two
  // before the autosave snapshot creates its file on disk. Framing it then
  // shows the explorer's "Failed to stat" until the user re-selects. So the
  // pane mounts only once the file is there: probed on selection and on every
  // disk move (`disk_rev`), retried briefly in between. After a generous wait
  // the frame is shown anyway, so a file that is genuinely missing still
  // reports the real error instead of a spinner forever.
  const [nodeFileReady, setNodeFileReady] = useState<string | null>(null);
  const nodePath = node && dir ? nodeFile(dir, node) : null;
  useEffect(() => {
    if (!nodePath || nodeFileReady === nodePath) return;
    let cancelled = false;
    let timer: number | null = null;
    const started = Date.now();
    const probe = async () => {
      try {
        await statPath(nodePath);
        if (!cancelled) setNodeFileReady(nodePath);
      } catch {
        if (cancelled) return;
        if (Date.now() - started > NODE_FILE_WAIT_MS) {
          setNodeFileReady(nodePath); // give up waiting: let the frame say so
          return;
        }
        timer = window.setTimeout(() => void probe(), NODE_FILE_PROBE_MS);
      }
    };
    void probe();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [nodePath, nodeFileReady, sync?.disk_rev]);
  const onPull = useCallback(async () => {
    setPullBusy(true);
    setPullError(null);
    try {
      const s = await pullCanvas(name);
      setSync(s);
      if (!s.ok) setPullError(s.pull_error ?? s.error ?? "pull failed");
    } catch (e) {
      setPullError((e as Error).message);
    } finally {
      setPullBusy(false);
    }
  }, [name]);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const baseOriginRef = useRef<string | null>(null);

  // The node frame's src, COMPUTED ONCE per node, tab and boot: it reads the
  // remembered session at that moment, and must not be re-derived on every
  // host render — the frame moves on to new sessions on its own, so a src
  // re-read later would differ from the one it booted on and React would set
  // the attribute, reloading the frame onto the session it is already on.
  const nodeBootRef = useRef<{ key: string; src: string } | null>(null);
  const nodeBootSrc = (path: string, file: string, tab: NodeTab): string => {
    const key = path + "\0" + tab + "\0" + nodeBootSeq;
    if (nodeBootRef.current?.key !== key) {
      nodeBootRef.current = {
        key,
        src: nodeEmbedSrc(
          path,
          tab,
          tab === "claude" ? (nodeSessions.get(nodeSessionKey(name, file)) ?? null) : null,
        ),
      };
    }
    return nodeBootRef.current.src;
  };
  // …and forgotten with the frame: deselecting unmounts the node pane, and
  // the same node selected again must boot on where its chat got to since,
  // not on the src the pane was last given.
  useEffect(() => {
    if (!node) nodeBootRef.current = null;
  }, [node]);
  // Mirror the frame's chat session as it changes (see `nodeSessions`). Bound
  // on every load — a new src and the tab remount both replace the window —
  // and only for the Claude tab: the Code tab's URL carries no session.
  const onNodeFrameLoad = useCallback(
    (e: SyntheticEvent<HTMLIFrameElement>) => {
      const frame = e.currentTarget;
      if (frame.dataset.tab !== "claude" || !frame.dataset.file) return;
      let win: Window | null = null;
      try {
        win = frame.contentWindow;
      } catch {
        win = null;
      }
      if (!win) return;
      const key = nodeSessionKey(name, frame.dataset.file);
      const pathname = new URL(frame.src, window.location.origin).pathname;
      const record = () => recordNodeSession(win!, key, pathname);
      record();
      win.addEventListener("fused:urlchange", record);
      win.addEventListener("popstate", record);
      win.addEventListener("pagehide", record);
    },
    [name],
  );

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

  // Tell the canvas whether THIS window is focused (see the header): on every
  // change, and whenever the frame (re)loads or re-hydrates, since it starts
  // from "not focused" and a page that loaded while the window was already
  // focused would never hear otherwise.
  const sendHostFocus = useCallback(() => {
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    frame.contentWindow.postMessage(
      { type: "fused-embed-host-focus", focused: hostFocused() },
      origin,
    );
  }, []);
  useEffect(() => {
    // Focus moving INTO the frame blurs this window too, but `hasFocus()`
    // stays true while a descendant frame holds focus — so that blur reports
    // focused, as it should.
    window.addEventListener("focus", sendHostFocus);
    window.addEventListener("blur", sendHostFocus);
    document.addEventListener("visibilitychange", sendHostFocus);
    return () => {
      window.removeEventListener("focus", sendHostFocus);
      window.removeEventListener("blur", sendHostFocus);
      document.removeEventListener("visibilitychange", sendHostFocus);
    };
  }, [sendHostFocus]);

  // Pointer enters the canvas pane: give the frame focus, unless the user is
  // mid-drag from elsewhere or typing (header). `preventScroll`: a focus
  // must never shift the page.
  const focusCanvasOnEnter = useCallback((e: React.MouseEvent) => {
    if (e.buttons !== 0) return;
    if (activeElementIsEditable(document)) return;
    frameRef.current?.focus({ preventScroll: true });
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
            // Assigned, not max'd: the counter is per watcher and starts
            // over with a new one (see the effect below).
            lastDeltaRevRef.current = delta.rev;
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
  const watcherEpochRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const rev = sync?.disk_rev;
    if (typeof rev !== "number") return;
    const epoch = sync?.watcher_epoch ?? null;
    if (lastDeltaRevRef.current === null) {
      // First sight of the counter: nothing to diff against yet. The frame's
      // hello (after it hydrates) asks for the full state.
      lastDeltaRevRef.current = rev;
      watcherEpochRef.current = epoch;
      return;
    }
    if (epoch !== watcherEpochRef.current || rev < lastDeltaRevRef.current) {
      // A DIFFERENT watcher (a stop/start, a server restart; none at all
      // reads null and 0). It counts from 0 again — possibly landing on the
      // very number the old one left — and knows nothing of the frame, while
      // its disk may have moved meanwhile: ask now (it answers with a full
      // state) instead of waiting for its counter to pass the old mark. At 0
      // there is no watcher to ask (or one that has not looked yet); the
      // next counter move is asked here.
      watcherEpochRef.current = epoch;
      lastDeltaRevRef.current = rev;
      if (rev === 0) return;
    } else if (rev === lastDeltaRevRef.current) {
      return;
    }
    relayDelta(false);
  }, [sync?.disk_rev, sync?.watcher_epoch, relayDelta]);

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
        if (type === "fused-embed-auth-ready") sendHostFocus();
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
        sendHostFocus();
      }
      if (type === "fused-embed-delta-applied" || type === "fused-embed-delta-rejected") {
        const rev = event.data?.rev;
        if (typeof rev !== "number") return;
        // Through the SNAPSHOT chain, not fire-and-forget: the frame posts a
        // snapshot before it applies a delta, and the server adopts the
        // acked reading as "what the frame holds". An ack that overtook that
        // snapshot (the snapshot route answers busy and sleeps while a push
        // holds the lock; the ack route waits for it) would make the server
        // read a node the delta added as one the snapshot deleted.
        const ok = type === "fused-embed-delta-applied";
        relayChainRef.current = relayChainRef.current.then(() =>
          ackDelta(name, rev, ok).then(() => undefined, () => undefined),
        );
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
  }, [seedToken, sendSelect, sendHostFocus, relaySnapshot, relayDelta, name]);

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
      {sync?.server_stale && (
        <div
          style={{
            padding: "8px 12px",
            borderBottom: "1px solid rgba(200,140,30,0.4)",
            background: "rgba(200,140,30,0.10)",
            fontSize: 13,
            display: "flex",
            alignItems: "center",
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <strong>Server changes</strong>
          <span style={{ opacity: 0.85 }}>
            {sync.server_stale.udfs.length > 0
              ? sync.server_stale.udfs.map((u) => `${u.slug} (${u.kind})`).join(", ")
              : "the canvas layout"}
            {" "}changed on the server. Nothing here is published until you pull. Pull
            merges them with your local edits where it can; anything it has to
            overwrite is kept in the canvas trash.
          </span>
          <button
            className="btn btn-primary"
            disabled={pullBusy || sync.pulling || sync.push_state === "pushing"}
            onClick={() => void onPull()}
            title="Bring the server's changes into this canvas; anything it overwrites is kept in the canvas trash"
          >
            {pullBusy ? "Pulling…" : "Pull"}
          </button>
          {pullError && <span style={{ color: "rgb(200,60,60)" }}>{pullError}</span>}
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
          // The wrapper, not the iframe: a frame swallows pointer events, but
          // the wrapper's mouseenter still fires as the pointer crosses in.
          onMouseEnter={focusCanvasOnEnter}
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
              {nodeFileReady === nodeFile(dir, node) ? (
                <iframe
                  // Keyed on the mode so a tab switch is a fresh document: the
                  // chat's own `session_id` lives on the frame URL, and the
                  // explorer's content-mode swap is for its own switcher.
                  key={nodeTab}
                  className="preview-side-frame"
                  src={nodeBootSrc(nodeFile(dir, node), node.file, nodeTab)}
                  data-tab={nodeTab}
                  data-file={node.file}
                  onLoad={onNodeFrameLoad}
                  title={`${node.udfName} — ${nodeTab === "code" ? "Code" : "Claude"}`}
                />
              ) : (
                <p style={{ padding: 16, opacity: 0.7 }}>Saving {node.file}…</p>
              )}
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
                // The hop's session is what the node pane boots on — the same
                // memory the frame's own moves feed, so one source of truth.
                const key = nodeSessionKey(name, hop.node.file);
                if (hop.sessionId) nodeSessions.set(key, hop.sessionId);
                else nodeSessions.delete(key);
                setNodeBootSeq((n) => n + 1);
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
