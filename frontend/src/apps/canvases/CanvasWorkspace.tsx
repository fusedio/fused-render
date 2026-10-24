// Canvas workspace (/canvases/<env>/<name>): the embedded live workbench
// beside a sync status strip for the local clone.
//
// A canvas is (env, name): its clone is ~/Fused/canvas/<env>/<name>, and
// every call below, the workbench origin and the whoami handle are that
// env's — never the `workbench_env` default — so a prod canvas and an
// unstable canvas can be open and syncing side by side.
//
// The iframe loads the AUTHENTICATED workbench editor
// (<base>/workbench/<handle>/<name>?fused_embed_auth=1). The Auth0 cookie
// never reaches a cross-site iframe, so the workbench's embed-auth mode asks
// its parent (this page) for a Bearer token over postMessage:
//   iframe → { type: "fused-embed-auth-ready" }   (also -refresh on a 401)
//   parent → { type: "fused-embed-auth-token", accessToken, publicOrigin }
//            publicOrigin is the workbench's real origin, for the links it
//            builds (share URLs): under the custom env's loopback proxy its
//            own origin is 127.0.0.1:<port>.
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
//   iframe → { type: "fused-embed-open-code", udfName, backendId? }  the node
//            toolbar's code button in embed mode. A TOGGLE: with that node's
//            Code tab already up, the right pane shuts; otherwise this page
//            selects the node (fused-embed-select first when it is not the
//            one shown), opens the right pane if it was hidden, and switches
//            it to Code. Accepted only from the workbench frame's own window.
//   iframe → { type: "fused-embed-delete", id, name }  the canvas menu's
//            "Delete canvas", after the frame's own two confirmations: the
//            host deletes (POST /api/canvases/delete — sync stopped, server
//            delete, clone removed) and leaves for the canvas list. The frame
//            writes nothing itself (embed-canvas-gates).
//   parent → { type: "fused-embed-delete-rejected", reason }  the host did
//            not delete (a name that is not this canvas's, or the delete
//            failed): the frame is still on the canvas and toasts it.
//   iframe → { type: "fused-embed-open-side" } | { type: "fused-embed-close-side" }
//            the workbench's right-edge chips (its own right pane never shows
//            embedded): open or shut the right pane here. Accepted only from
//            the workbench frame's own window.
//   parent → { type: "fused-embed-side-panel", open, code, width }  whether the
//            right pane is up, whether it is up on the shown node's Code tab,
//            and its width in px over the frame — always 0: the pane shares
//            the row with the frame, it never covers it — on every change and
//            on every frame load: the frame's expand chip shows while the pane
//            is shut, its collapse chip while it is up, and the toolbar's code
//            button reads "Hide" / "Show code panel".
//   parent → { type: "fused-embed-pan", dx, dy }  on every tick of a seam drag,
//            half the pane's width change: the canvas pans to keep its visible
//            centre put, as the workbench's own sidebar drags do.
// Keyed by UDF NAME, never a React Flow node id (those are regenerated every
// load): the node's source is `<clone>/<udfName>.py` (widgets: `.json`).
//
// Sync (embed v3, canvas_snapshot.py): the embedded workbench never saves to
// the server. On its autosave cadence it posts the body it would have saved,
// minus code, as
//   iframe → { type: "fused-embed-snapshot", gen, seq, body }
// which this page relays to POST /api/canvases/snapshot; the answer goes back
// as fused-embed-snapshot-applied / -rejected. A relay the page ABANDONS
// because a newer snapshot superseded it while the clone was busy (the newer
// one carries the full state) is answered
//   parent → { type: "fused-embed-snapshot-applied", gen, seq, push_seq, stale: true }
// so the embed's bridge settles that seq instead of holding it forever. One
// whose busy window ran out with nothing newer is -rejected: it is not on disk.
// The two sync loops' state (sync-state.ts: the four legs, the host's own
// embed_to_disk leg merged in, the worst leg precomputed) goes to the frame
// on every change, ≤ 10/s, for the embed's save chip:
//   parent → { type: "fused-embed-sync-state", state }
// The clone watcher is then the ONE writer to the server, publishing the
// folder exactly as it publishes a Claude edit.
//
// Source texts (the body channel, SyncManager.apply_body): a workbench whose
// editors are live in the frame posts each file's text on its own, by
// compare-and-swap on content — never inside a snapshot:
//   iframe → { type: "fused-embed-body", seq, name, file, base_hash, text }
//   parent → { type: "fused-embed-body-applied", seq, name, hash }
//   parent → { type: "fused-embed-body-rejected", seq, name, code, reason,
//              disk_text?, disk_hash? }   code "stale": disk moved under the
//              frame's base (Claude wrote the file) — the frame rebases its
//              edits onto `disk_text` and posts again; "busy": the clone stayed
//              locked, post again later; anything else: dropped, logged.
// Relayed on the SAME ordered chain as snapshots and rename hints: a body for
// a UDF's old file name must not overtake the rename that moves the file.
// The token message tells the frame this host speaks the channel
// (`caps: ["body"]`); without it the frame keeps its editors hidden and code
// disk-owned, as before.
//
// The other direction (canvas_snapshot.py, "the disk → embed leg"): this page
// never reloads the workbench iframe and never asks it to re-fetch. When the
// clone moves on disk (`disk_rev` on the sync events socket's `state`
// message — or the 2 s sync/state poll while the socket is down) it fetches
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
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { SyntheticEvent } from "react";
import { createPortal } from "react-dom";
import { addCurrentApp } from "@platform/lib/api";
import { CURRENT_APPS_CHANGED_EVENT } from "@platform/lib/tasksChanged";
import { ChatMount, ShotViewer, type CanvasShot, type CanvasShotAnswer, type CanvasShotWhat, type Viewable } from "@apps/claude";
import { rawUrl, statPath, type TemplateEntry } from "@platform/lib/api";
import { EMBED_PREFIX, NAV_EVENT, VIEW_PREFIX, embedUrlForFsPath, navigateUrl } from "@platform/lib/router";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  CanvasOwnedError,
  ackDelta,
  applyRename,
  applySnapshot,
  catalogPath,
  canvasProjectPath,
  fixWithClaude,
  deleteCanvas,
  getDelta,
  getAccessToken,
  getCanvasesStatus,
  getSyncState,
  getWhoami,
  applyBody,
  checkRemote,
  pullCanvas,
  startSync,
  syncEventsUrl,
  type WorkbenchEnv,
  canvasNameFromPath,
} from "./api";
import { SyncDrawer } from "./SyncDrawer";
import { SyncFramePoster, SyncStrip } from "./SyncStrip";
import {
  mergeLogEvents,
  noWatcher,
  type EmbedFailure,
  type MergedSyncState,
  type RelayEntry,
  type SyncLogEvent,
  type SyncOwner,
  type SyncState,
} from "./sync-state";

// How long a canvas screenshot waits for the frame's answer. html-to-image
// over a big graph takes seconds; a frame without the handler never answers,
// and the chat's camera must come back.
const THUMBNAIL_TIMEOUT_MS = 45_000;
// How long a screenshot request waits for the workbench frame's first hello
// before giving up: a page still framing the workbench (a fresh open, a
// slow `fused whoami`) answers within this on an ordinary day.
const FRAME_UP_WAIT_MS = 40_000;

// Fallback cadence for GET sync/state while the events socket is down.
const SYNC_POLL_MS = 2000;

// Events socket reconnect backoff: doubles from MIN to MAX.
const WS_RETRY_MIN_MS = 1000;
const WS_RETRY_MAX_MS = 15_000;

// While another process owns the clone (sync/start 409 `owned`), sync/start
// is retried this often so the page recovers when that process quits.
const OWNED_RETRY_MS = 5000;

// Re-arming a dropped watcher (a state that says none runs here) at most
// this often.
const HEAL_MIN_INTERVAL_MS = 5000;

// A snapshot that met a busy clone (409 `busy`: a force pull or a pause holds
// the clone; `no_watcher`: the watcher is being re-armed) is retried this far
// apart, for up to this long. Past the window, with nothing newer queued, it
// is rejected to the embed (see `abandon` in relaySnapshot): not on disk.
const SNAPSHOT_BUSY_WINDOW_MS = 120_000;
const SNAPSHOT_BUSY_RETRY_MS = 750;
// A body post that meets a locked clone retries this long before the frame
// is told `busy` and keeps the text pending on its side (it retries too).
const BODY_BUSY_WINDOW_MS = 10_000;

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

// A fallback state poll that keeps failing is treated, after this many
// consecutive misses, as a dropped watcher: sync/start is asked again.
const FAILED_POLLS_BEFORE_RELEASE = 3;

// A focus-triggered server check at most this often: a window that is
// clicked in and out of repeatedly must not become a probe per click.
const FOCUS_CHECK_MIN_MS = 3000;

// The node pane's Code tab saves on blur / Cmd+Enter (`_saveon=blur`, the code
// template), and a blur save can still be in flight when the frame goes away:
// anything that unmounts or remounts it asks the frame to flush first, waiting
// at most this long (a hung write must not wedge the canvas).
const NODE_CODE_FLUSH_MS = 10_000;

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

/** Whether the right pane (canvas chat or node pane) is up. Shut rides this
 *  page's URL as `side=off` — the explorer's own word for a shut companion
 *  column (`_side=off`, apps/explorer/lib/preview-side.ts) — so a reload keeps
 *  it; absent means open, the default. */
const SIDE_OFF = "off";

function sideOpenFromUrl(): boolean {
  return new URLSearchParams(window.location.search).get("side") !== SIDE_OFF;
}

/** A node tab's icon: the mode's own template icon.svg off the node file's
 *  stat, drawn exactly as the explorer's sidebar tabs draw theirs
 *  (apps/explorer/ModeSwitcher's templateModeIcon, which an app may not
 *  import): a currentColor mask, else the mode's first letter in a box. */
function nodeTabIcon(mode: NodeTab, templates: readonly TemplateEntry[]): React.ReactNode {
  const entry = templates.find((t) => t.mode === mode);
  if (!entry) return null;
  if (entry.icon === null) {
    return <span className="mode-icon-placeholder">{mode.charAt(0).toUpperCase()}</span>;
  }
  const mask = `url("${rawUrl(entry.icon)}")`;
  return <span className="mode-icon-mask" style={{ WebkitMaskImage: mask, maskImage: mask }} />;
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
// The last `fused whoami` handle per env (localStorage): see the whoami
// step in the boot effect. A bare string, no JSON — nothing to parse wrong.
const HANDLE_KEY = "canvases:handle:";
function rememberedHandle(env: string): string | null {
  try {
    const v = localStorage.getItem(HANDLE_KEY + env);
    return v && v.trim() ? v : null;
  } catch {
    return null;
  }
}
function forgetHandle(env: string): void {
  try {
    localStorage.removeItem(HANDLE_KEY + env);
  } catch {
    // nothing to forget
  }
}
function rememberHandle(env: string, handle: string): void {
  try {
    localStorage.setItem(HANDLE_KEY + env, handle);
  } catch {
    // private mode / quota: the next open just asks whoami again.
  }
}

function nodeEmbedSrc(file: string, tab: NodeTab, sessionId: string | null): string {
  // `_noann=1` on the chat: its Screenshot / Annotate seats and view toggle
  // would open the chat's own preview of the file, which is the Code tab's
  // job here (owner, 2026-10-17). It also pins the chat to its narrow CHAT
  // view at every width (useNarrowView `chatViewOnly`): the chat's own file
  // pane stays mounted — it is what files the session under the file — but
  // never on screen, so a wide node pane is the chat alone, not code + chat. `session_id` when a Recent row on the
  // canvas chat hopped here: the frame's chat resumes that conversation.
  return embedUrlForFsPath(
    file,
    "?_mode=" + tab + "&_side=off" +
      // `_canvasshot=1`: the chat's Screenshot seat asks THIS page (its
      // parent) for a picture of the canvas — see the `fused-canvas-shot`
      // handler below.
      (tab === "claude" ? "&_noann=1&_canvasshot=1" : "") +
      // `_saveon=blur`: the code template saves when the editor loses focus
      // or on Cmd+Enter, not 250 ms after every edit — each save of a node
      // file reaches the canvas as a delta that RUNS the node (owner,
      // 2026-10-24: "when I keep typing it should not evaluate").
      (tab === "code" ? "&_saveon=blur" : "") +
      (sessionId ? "&session_id=" + encodeURIComponent(sessionId) : ""),
  );
}

/**
 * THE SESSION EACH NODE'S CHAT WAS LAST ON, per canvas: `<env>\0<canvas>\0
 * <file>` → `session_id` (env included: a prod and an unstable canvas may
 * share a name, and their nodes are different files). Module-level so it
 * outlives this component, which App.tsx keys on (env, name) (a hop to
 * another canvas and back must not forget).
 * The chat inside the node pane runs on the frame's URL (`paramsSource` "url"
 * in the explorer's embed), so the frame's `location.search` IS the chat's
 * state: `session_id` present = that conversation, absent = the chat's home
 * (New chat was pressed, or nothing has been sent). The entry mirrors that —
 * set or deleted — rather than accumulating, so coming back after New chat
 * lands on the fresh chat the user asked for, not the old session.
 */
const nodeSessions = new Map<string, string>();

function nodeSessionKey(env: WorkbenchEnv, canvas: string, file: string): string {
  return env + "\0" + canvas + "\0" + file;
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

/** The chat this page's URL names for its node — `?node=…&session_id=…`, a
 *  reload or the Tasks page's door on a canvas task (`canvasPagePath`) —
 *  remembered under the node so the pane boots on it. The node frame's src
 *  reads `nodeSessions` alone (`nodeBootSrc`), and that map is empty on a
 *  fresh mount: without this the URL would select the node and land its
 *  pane on the chat's home, not the conversation the link named. Must run
 *  BEFORE the first render that computes the src, hence the state
 *  initializer below rather than an effect. Answers whether a session was
 *  named, for the nav listener that reboots the pane on one. */
function seedNodeSessionFromUrl(env: WorkbenchEnv, canvas: string, node: NodeSel): string | null {
  const sid = new URLSearchParams(window.location.search).get("session_id");
  if (!sid) return null;
  nodeSessions.set(nodeSessionKey(env, canvas, node.file), sid);
  return sid;
}

export default function CanvasWorkspace({
  env,
  name,
  embedded = false,
  onWorkbenchUrl,
  syncSlot = null,
}: {
  env: WorkbenchEnv;
  name: string;
  /** FRAMED BY THE PROJECT PAGE (shell/CanvasPage.tsx, /canvases-project/…)
   *  as its read-only Overview: the workbench is loaded as a VIEWER
   *  (`fused_embed_readonly=1` — no drag, connect, add, rename, delete, no
   *  autosave; the host's deltas still patch it in place, so a Claude task
   *  editing the clone shows live), there is no right pane at all (no canvas
   *  chat, no node pane — a selection is ignored), and the sync strip drops
   *  the back arrow, env chip and workbench link, which the page's own
   *  header carries. The sync loops run as they do in the editor: the
   *  watcher is still this process's. Off (the default) this is the editor
   *  at /canvases/<env>/<name>: two panes, canvas + Claude. */
  embedded?: boolean;
  /** The hosted workbench editor's URL for this canvas (no embed flags), once
   *  the base URL and handle are known; null until then. The project page's
   *  header draws it as "Open in Workbench". */
  onWorkbenchUrl?: (url: string | null) => void;
  /** `embedded` only: the project page's SYNC TAB panel. The sync chrome —
   *  the strip, the log (pinned open, no Close), Pull / Hard pull, a failed
   *  push's detail with Fix with Claude — is portalled INTO it instead of
   *  drawn over the canvas (owner, 2026-10-20: "remove the sync row from the
   *  overview. Add a sync tab with all the sync panel details"). The state
   *  stays here, with the sync loops, which the Overview keeps running
   *  behind every tab; the panel is only where it is shown. Null while the
   *  Sync tab is not the one on screen: nothing is drawn, nothing stops. */
  syncSlot?: HTMLElement | null;
}) {
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  // The workbench's own origin, for links a person opens: `baseUrl` is the
  // embed proxy's loopback origin while that runs, which changes port on
  // every restart and is gone once it stops.
  const [workbenchBase, setWorkbenchBase] = useState<string | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [dir, setDir] = useState<string | null>(null);
  // The claude template's path, from the clone dir's own stat — the same
  // resolution the explorer's sidebar uses, so a user override (§16) wins here
  // too. Needed because the chat is framed DIRECTLY via /render (below), not
  // through /explorer/embed.
  const [chatTpl, setChatTpl] = useState<string | null>(null);
  // The clone dir's templates, off the same stat: the node tabs' icons until
  // the shown node's own stat answers (a template's icon is the template's,
  // not the file's, so the dir's entries draw the same glyphs).
  const [dirTemplates, setDirTemplates] = useState<TemplateEntry[] | null>(null);
  // The selected node (null = the canvas chat). Seeded from `?node=` so a
  // reload keeps the node; written back below.
  const [node, setNode] = useState<NodeSel | null>(() => {
    const seeded = nodeFromUrl();
    if (seeded) seedNodeSessionFromUrl(env, name, seeded);
    return seeded;
  });
  const [nodeTab, setNodeTab] = useState<NodeTab>(() => nodeTabFromUrl());
  // The right pane shown or hidden (the sync strip's panel toggle). Hiding it
  // leaves the selection alone: the canvas goes on selecting, and the pane
  // opens on whatever node is selected by then.
  const [sideOpen, setSideOpen] = useState<boolean>(() => sideOpenFromUrl());
  // Bumped when the node pane must REBOOT on a session for the node already
  // shown (a Recent row's hop onto the selected node, below): the frame's src
  // is otherwise frozen per node and tab (`nodeBootSrc`).
  const [nodeBootSeq, setNodeBootSeq] = useState(0);
  // The server's sync state (sync/state, or the events socket's `state`):
  // disk_to_embed / disk_to_server / server_to_disk, disk_rev, watcher_epoch.
  const [sync, setSync] = useState<SyncState | null>(null);
  // Another process is this clone's one writer (sync/start 409 `owned`):
  // the page shows who and loads nothing that would write.
  const [owned, setOwned] = useState<SyncOwner | null>(null);
  // sync/start failed for another reason (the server still coming up, a
  // transient 5xx): shown, and retried like an owned clone.
  const [startError, setStartError] = useState<string | null>(null);
  // The embed_to_disk leg, the page's own: every snapshot / rename the frame
  // posted that has not yet been acked, rejected or abandoned.
  const [relayQueue, setRelayQueue] = useState<RelayEntry[]>([]);
  const relayIdRef = useRef(0);
  // Why the last relay did not land — refused, or busy-window expiry with
  // nothing newer behind it (cleared by the next one applied). See
  // EmbedFailure: a relay superseded by a newer one is NOT a failure.
  const [relayFailure, setRelayFailure] = useState<EmbedFailure | null>(null);
  // Mirrors sync/state's `fix_active`; set at once by a Fix click.
  const [fixActive, setFixActive] = useState(false);
  // Events socket up (else the page is polling sync/state).
  const [live, setLive] = useState(false);
  // The sync log drawer and its buffer (newest first).
  const [drawerOpen, setDrawerOpen] = useState(false);
  // The drawer is gated on (sync || owned). A drawer opened under `owned`
  // whose ownership then clears before the first sync state would otherwise
  // stay armed and pop open by itself when that state lands.
  useEffect(() => {
    if (!sync && !owned) setDrawerOpen(false);
  }, [sync, owned]);
  const [logEvents, setLogEvents] = useState<SyncLogEvent[]>([]);
  const wsLogAtRef = useRef(0);
  // Bumped to make SyncFramePoster re-post the state (frame (re)loaded).
  const [repostSeq, setRepostSeq] = useState(0);
  // Splitter position: workbench pane width as a fraction of the row.
  const [leftFrac, setLeftFrac] = useState(0.55);
  const leftFracRef = useRef(leftFrac);
  useLayoutEffect(() => {
    leftFracRef.current = leftFrac;
  }, [leftFrac]);
  const [dragging, setDragging] = useState(false);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A "Fix with Claude" run in flight: the editor iframe attaches to it via
  // its ?run= param, so the user watches the fix happen in the chat they
  // already have. Kept after the push recovers — the session may still be
  // finishing its final message.
  const [fixRunId, setFixRunId] = useState<string | null>(null);
  // Bumped when a hop lands on the CANVAS chat with a `session_id` other
  // than the one it mounted on — another root task of the same canvas from
  // the Tasks page, or Back to the bare canvas from one (none) — the chat
  // reads `session_id` at boot only, so it is remounted. `canvasChatSidRef`
  // is what the mounted chat was keyed on; the chat moving on by itself
  // (replaceState, no event here) can leave it behind, which costs at most
  // one needless remount onto the session the URL names.
  const [canvasChatSeq, setCanvasChatSeq] = useState(0);
  const canvasChatSidRef = useRef<string | null>(
    new URLSearchParams(window.location.search).get("session_id"),
  );
  const [fixBusy, setFixBusy] = useState(false);
  const [fixError, setFixError] = useState<string | null>(null);
  // "Server changes · Pull": the hosted workbench's chip, for this tab. The
  // watcher holds its push while the server_to_disk leg is stale; this button
  // is the user's pull. The result reaches the embed as a delta like any other
  // disk change.
  const [pullBusy, setPullBusy] = useState(false);
  // A PICTURE FROM THE WORKBENCH, on request from a chat's Screenshot seat
  // (the in-page canvas chat, or the node pane's framed one, `_canvasshot=1`).
  // The frame is cross-origin, so nothing here can photograph it; the frame
  // captures with its own tools — the graph as the viewport shows it, or one
  // node's output — uploads nothing, and answers `fused-embed-capture-done`
  // with the Blob, which goes on to the chat as a pane attachment. One request
  // at a time, matched to its answer by `req`; a frame that never answers (an
  // older workbench build without the handler) is released by the timer.
  type ShotAnswer = CanvasShotAnswer;
  const shotPendingRef = useRef<{ req: number; resolve: (a: ShotAnswer) => void } | null>(null);
  const shotTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settleShot = useCallback((answer: ShotAnswer) => {
    if (shotTimerRef.current) clearTimeout(shotTimerRef.current);
    shotTimerRef.current = null;
    const pending = shotPendingRef.current;
    shotPendingRef.current = null;
    pending?.resolve(answer);
  }, []);
  // Whether the workbench frame has hydrated (its first hello). A request that
  // arrives before that — the agent's tool fires the moment its turn starts,
  // often before the page has framed the workbench — waits for it rather than
  // failing with "not loaded yet" at a moment that fixes itself in seconds.
  const frameUpRef = useRef(false);
  const frameUpWaitersRef = useRef<Array<() => void>>([]);
  const awaitFrameUp = useCallback((timeoutMs: number): Promise<boolean> => {
    if (frameUpRef.current && frameRef.current?.contentWindow && baseOriginRef.current) {
      return Promise.resolve(true);
    }
    return new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => {
        frameUpWaitersRef.current = frameUpWaitersRef.current.filter((w) => w !== wake);
        resolve(false);
      }, timeoutMs);
      const wake = () => {
        clearTimeout(timer);
        resolve(true);
      };
      frameUpWaitersRef.current.push(wake);
    });
  }, []);
  // One request at a time, claimed BEFORE the frame wait: two asks landing
  // during that wait would otherwise both pass the check, and the second's
  // `shotPendingRef` would bury the first, whose promise then never settled.
  const shotBusyRef = useRef(false);
  const requestCanvasShot = useCallback(async (what: CanvasShotWhat, udf?: string): Promise<ShotAnswer> => {
    if (shotBusyRef.current || shotPendingRef.current) {
      return { ok: false, error: "a canvas screenshot is already being taken" };
    }
    shotBusyRef.current = true;
    try {
      if (!(await awaitFrameUp(FRAME_UP_WAIT_MS))) {
        return { ok: false, error: "the workbench has not loaded in this canvas page yet — reload the canvas page and try again" };
      }
    } finally {
      shotBusyRef.current = false;
    }
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) {
      return { ok: false, error: "the workbench is not loaded yet" };
    }
    const win = frame.contentWindow;
    return new Promise<ShotAnswer>((resolve) => {
      const req = Date.now();
      shotPendingRef.current = { req, resolve };
      win.postMessage(
        // The named node when the asker names one (the agent's tool may ask
        // for any UDF on the canvas); else the node this page shows.
        { type: "fused-embed-capture", req, what, udfName: udf || nodeRef.current?.udfName || null },
        origin,
      );
      shotTimerRef.current = setTimeout(() => {
        if (shotPendingRef.current?.req === req) {
          settleShot({ ok: false, error: "the workbench did not answer — it may still be drawing a slow capture (map nodes); wait a minute before asking again" });
        }
      }, THUMBNAIL_TIMEOUT_MS);
    });
  }, [settleShot, awaitFrameUp]);
  // The in-page canvas chat's contract: the canvas view only (no node is
  // selected while that chat shows).
  const canvasShot = useMemo<CanvasShot>(
    () => ({ capture: requestCanvasShot, targets: ["canvas"] }),
    [requestCanvasShot],
  );
  useEffect(
    () => () => {
      // A request still out when the page goes: answer it, so the asking
      // tray's `capture()` resolves and its seat comes back.
      settleShot({ ok: false, error: "the canvas page closed" });
    },
    [settleShot],
  );
  // THE FRAMED CHAT'S PICTURE, shown here: the node pane's chat posts
  // `fused-shot-view` instead of opening its own viewer, which an iframe
  // would box into the pane (ClaudeChat `hoistViewer`). Same origin only.
  const [hostViewing, setHostViewing] = useState<Viewable | null>(null);
  useEffect(() => {
    const onShotView = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== "fused-shot-view") return;
      // From one of THIS page's frames only (the node pane), and the picture
      // is a blob: URL of that frame's or a same-origin path — never a URL
      // pointing elsewhere.
      const fromChild = [...document.querySelectorAll("iframe")].some(
        (f) => f.contentWindow === event.source,
      );
      if (!fromChild) return;
      const shot = event.data.shot;
      if (!shot || typeof shot !== "object" || typeof shot.kind !== "string") return;
      const src = typeof shot.src === "string" ? shot.src : undefined;
      if (src !== undefined) {
        let ok = src.startsWith("blob:");
        if (!ok) {
          try {
            ok = new URL(src, window.location.href).origin === window.location.origin;
          } catch {
            ok = false;
          }
        }
        if (!ok) return;
      }
      setHostViewing(shot as Viewable);
    };
    window.addEventListener("message", onShotView);
    return () => window.removeEventListener("message", onShotView);
  }, []);
  // The picture is the node frame's blob: URL, which dies with that
  // document: a node switch or tab switch remounts the frame, so the viewer
  // shuts rather than show a broken image.
  useEffect(() => {
    setHostViewing(null);
  }, [node, nodeTab]);
  // The framed chat's ask, from a same-origin child frame (the node pane). The
  // answer goes back to the asking window — `event.source` — so a pane that
  // swapped frames meanwhile never receives a picture it did not ask for.
  useEffect(() => {
    const onShotAsk = (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== "fused-canvas-shot") return;
      const source = event.source as Window | null;
      const req = event.data.req;
      const what = event.data.what;
      const udf = typeof event.data.udf === "string" ? event.data.udf : undefined;
      if (!source) return;
      if (what !== "canvas" && what !== "udf") return;
      void requestCanvasShot(what, udf).then((answer) => {
        source.postMessage(
          { type: "fused-canvas-shot-done", req, ...answer },
          window.location.origin,
        );
      });
    };
    window.addEventListener("message", onShotAsk);
    return () => window.removeEventListener("message", onShotAsk);
  }, [requestCanvasShot]);
  const [pullError, setPullError] = useState<string | null>(null);

  // The node pane frames the node's FILE, and a node the user just added in
  // the embed is selected the instant it exists there — a second or two
  // before the autosave snapshot creates its file on disk. Framing it then
  // shows the explorer's "Failed to stat" until the user re-selects. So the
  // pane mounts only once the file is there: probed on selection and on every
  // disk move (`disk_rev`), retried briefly in between. After a generous wait
  // the frame is shown anyway, so a file that is genuinely missing still
  // reports the real error instead of a spinner forever.
  // Every server state lands through here. A state older than the one held
  // (same watcher, lower disk_rev: a slow poll answer overtaken by the
  // socket) is dropped, so the delta effect never reads it as a new watcher.
  const applyState = useCallback((s: SyncState) => {
    setSync((prev) =>
      prev && prev.watcher_epoch === s.watcher_epoch && s.disk_rev < prev.disk_rev ? prev : s,
    );
  }, []);

  const [nodeFileReady, setNodeFileReady] = useState<string | null>(null);
  // The node file's templates, off the same stat, for the tab icons.
  const [nodeTemplates, setNodeTemplates] = useState<{ path: string; templates: TemplateEntry[] } | null>(null);
  const nodePath = node && dir ? nodeFile(dir, node) : null;
  useEffect(() => {
    if (!nodePath || nodeFileReady === nodePath) return;
    let cancelled = false;
    let timer: number | null = null;
    const started = Date.now();
    const probe = async () => {
      try {
        const st = await statPath(nodePath);
        if (cancelled) return;
        setNodeTemplates({ path: nodePath, templates: st.templates ?? [] });
        setNodeFileReady(nodePath);
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
  const onPull = useCallback(async (hard = false) => {
    setPullBusy(true);
    setPullError(null);
    try {
      // The answer is the old sync/status shape: read only its verdict. The
      // new state follows on the events socket (or the next poll).
      const s = await pullCanvas(env, name, hard);
      if (!s.ok) setPullError(s.pull_error ?? s.error ?? "pull failed");
      void getSyncState(env, name).then(applyState, () => undefined);
    } catch (e) {
      setPullError((e as Error).message);
    } finally {
      setPullBusy(false);
    }
  }, [env, name]);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  // The node pane's frame (the explorer embed on the node's file).
  const nodeFrameRef = useRef<HTMLIFrameElement | null>(null);
  // Re-select the shown node in the workbench (set below, once `sendSelect`
  // and `nodeRef` exist): what a refused flush falls back to.
  const reselectShownNodeRef = useRef<() => void>(() => {});
  /** Run `fn` once the Code tab's buffer is on disk — at once when no Code
   *  tab is up (nothing to flush: the order of what follows must not change),
   *  else after the frame's flush relay (explorer Preview → code template)
   *  answers or NODE_CODE_FLUSH_MS passes. A flush the template REFUSES
   *  (`{ok:false}`: its conflict banner is up, or the write failed) keeps the
   *  pane — the banner is the user's only way to resolve it, and the buffer
   *  would die with the frame — and re-selects the node so the canvas
   *  agrees with the pane again. A hung flush (the timer) lets `fn` run. */
  const afterNodeCodeFlush = useCallback((fn: () => void) => {
    const frame = nodeFrameRef.current;
    const flush =
      frame && frame.dataset.tab === "code" && frame.contentWindow
        ? (frame.contentWindow as unknown as { __fusedFlushEdits?: () => Promise<unknown> })
            .__fusedFlushEdits
        : undefined;
    if (typeof flush !== "function") {
      fn();
      return;
    }
    void Promise.race([
      flush().catch(() => undefined),
      new Promise((r) => window.setTimeout(r, NODE_CODE_FLUSH_MS)),
    ]).then((res) => {
      if (res && (res as { ok?: boolean }).ok === false) {
        reselectShownNodeRef.current();
        return;
      }
      fn();
    });
  }, []);
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
          tab === "claude" ? (nodeSessions.get(nodeSessionKey(env, name, file)) ?? null) : null,
        ),
      };
    }
    return nodeBootRef.current.src;
  };
  // …and forgotten with the frame: deselecting (or hiding the right pane)
  // unmounts the node pane, and the same node shown again must boot on where
  // its chat got to since, not on the src the pane was last given.
  useEffect(() => {
    if (!node || !sideOpen) nodeBootRef.current = null;
  }, [node, sideOpen]);
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
      const key = nodeSessionKey(env, name, frame.dataset.file);
      const pathname = new URL(frame.src, window.location.origin).pathname;
      const record = () => recordNodeSession(win!, key, pathname);
      record();
      win.addEventListener("fused:urlchange", record);
      win.addEventListener("popstate", record);
      win.addEventListener("pagehide", record);
    },
    [env, name],
  );

  // Make sure THIS process's watcher runs on the clone. The one place
  // sync/start is called (boot, self-heal, the owned retry): success clears
  // `owned` and gives the clone dir; a 409 `owned` (another fused-render
  // process holds the clone's lock) sets `owned` instead, which keeps the
  // workbench frame, the delta relay and the events socket down until a
  // retry gets the lock. Anything else is thrown to the caller.
  const ensureSync = useCallback(async (): Promise<void> => {
    try {
      const started = await startSync(env, name);
      setOwned(null);
      setStartError(null);
      setDir(started.dir);
    } catch (e) {
      if (e instanceof CanvasOwnedError) {
        setOwned(e.owner);
        return;
      }
      throw e;
    }
  }, [env, name]);

  // Boot: base URL + handle + make sure the watcher runs. sync/start goes
  // out at once, IN PARALLEL with status + whoami (it needs neither): the
  // frame waits for both (it needs the handle for its URL and a running
  // watcher for its snapshots), so its wait is the slower of the two — the
  // whoami chain, as before — never their sum.
  useEffect(() => {
    let cancelled = false;
    void ensureSync().catch((e) => {
      if (!cancelled) setStartError((e as Error).message);
    });
    void (async () => {
      try {
        const status = await getCanvasesStatus();
        if (cancelled) return;
        // THIS canvas's environment's origin and handle, not the default's:
        // the token is posted only to this exact origin. `embed_url` is the
        // origin to frame — the custom env's loopback proxy when that is on
        // (canvas_embed_proxy.py); otherwise the workbench itself.
        const base = status.envs[env].embed_url ?? status.envs[env].base_url;
        setBaseUrl(base);
        setWorkbenchBase(status.envs[env].base_url);
        baseOriginRef.current = new URL(base).origin;
        // The clone onto the sidebar's desk (its Canvases section, the
        // Projects store): opening a canvas is what puts it there, as a task
        // under it does. From STATUS, not sync/start's answer — a clone
        // another process owns answers 409 there and would never register
        // (CodeRabbit on #1795). Idempotent on the server; the section
        // refetches on the window event (it cannot see this POST's answer).
        const cloneDir = status.envs[env].canvases_dir.replace(/\/+$/, "") + "/" + name;
        void addCurrentApp(cloneDir)
          .then(() => window.dispatchEvent(new Event(CURRENT_APPS_CHANGED_EVENT)))
          .catch(() => undefined);
        // THE HANDLE IS REMEMBERED (per env): it is the one fact the frame
        // URL needs, it does not change, and `fused whoami` is a CLI child
        // that can take 20 s+ on a loaded Mac. A remembered handle frames the
        // workbench NOW; whoami still runs and corrects it if it ever differs.
        // Only a first-ever open with no memory waits on it — and a timeout
        // there is retried once before it becomes the page's error.
        const remembered = rememberedHandle(env);
        if (remembered) setHandle(remembered);
        let who: Awaited<ReturnType<typeof getWhoami>> | null = null;
        let lastErr: Error | null = null;
        for (let attempt = 0; attempt < 2 && !who; attempt++) {
          try {
            who = await getWhoami(env);
          } catch (e) {
            lastErr = e as Error;
            if (cancelled) return;
          }
        }
        if (cancelled) return;
        if (!who?.handle) {
          // whoami ANSWERED with no handle: nobody is logged in any more, so
          // the memory is stale — forget it and say so, framed or not. Only
          // a whoami that could not answer at all (the timeout) lets a
          // remembered frame stand.
          if (who) {
            forgetHandle(env);
            setHandle(null);
            setError("could not determine your Fused username (fused whoami)");
            return;
          }
          if (remembered) return; // framed on memory; the correction can wait
          setError(
            lastErr?.message ?? "could not determine your Fused username (fused whoami)",
          );
          return;
        }
        rememberHandle(env, who.handle);
        if (who.handle !== remembered) setHandle(who.handle);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [env, name, ensureSync]);

  // The claude template's path for the clone (see `chatTpl`), once the clone
  // dir is known — at boot, or when an owned canvas is finally released.
  useEffect(() => {
    if (!dir) return;
    let cancelled = false;
    void statPath(dir)
      .then((st) => {
        if (cancelled) return;
        setChatTpl(st.templates?.find((t) => t.mode === "claude")?.path ?? null);
        setDirTemplates(st.templates ?? []);
      })
      .catch((e) => {
        if (!cancelled) setError((e as Error).message);
      });
    return () => {
      cancelled = true;
    };
  }, [dir]);

  // Owned by another process, or sync/start failed: ask again every
  // OWNED_RETRY_MS so the page recovers on its own when that process quits
  // (its lock goes stale) or the server answers again.
  const retryStart = !!owned || startError !== null;
  useEffect(() => {
    if (!retryStart) return;
    const id = window.setInterval(() => {
      void ensureSync().catch((e) => setStartError((e as Error).message));
    }, OWNED_RETRY_MS);
    return () => window.clearInterval(id);
  }, [retryStart, ensureSync]);

  // Token handshake with the embedded workbench.
  const seedToken = useCallback(async () => {
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    try {
      // THIS canvas's env's login, as the origin is its env's: a custom
      // deployment has its own `[auth]` tenant (D1412).
      const { access_token } = await getAccessToken(env);
      // `publicOrigin`: the workbench's REAL origin. Framed through the custom
      // env's loopback proxy (canvas_embed_proxy.py) the workbench's own
      // `window.location.origin` is `http://127.0.0.1:<port>`, and every link
      // it builds from that — the Share dialog's canvas URL above all — would
      // name a port on this machine that nobody else can open. The workbench
      // builds those links from this origin instead (application embed-auth.ts).
      // Parsed apart from the post: a malformed FUSED_RENDER_WORKBENCH_URL
      // override reaches here unvalidated, and it must cost the field, not
      // the token.
      let publicOrigin: string | undefined;
      if (workbenchBase) {
        try {
          publicOrigin = new URL(workbenchBase).origin;
        } catch {
          publicOrigin = undefined;
        }
      }
      frame.contentWindow.postMessage(
        {
          type: "fused-embed-auth-token",
          accessToken: access_token,
          // What this host speaks beyond v3: the body channel (see the
          // module doc). The frame shows its editors only when it hears it.
          caps: ["body"],
          ...(publicOrigin ? { publicOrigin } : {}),
        },
        origin, // exact origin — this message carries a credential
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }, [env, workbenchBase]);

  // Ask the workbench to select a node (or, with null, to deselect). Exact
  // targetOrigin, like every message to the frame.
  const sendSelect = useCallback((udfName: string | null) => {
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    frame.contentWindow.postMessage({ type: "fused-embed-select", udfName }, origin);
  }, []);

  // Whether the right pane is up, and up on the shown node's Code tab (see
  // the header): the frame's expand chip and its toolbar code button act on
  // this pane, so they need the state to show and label themselves. Sent on
  // every change, and on every frame load below.
  // The viewer (`embedded`) renders no pane at all: it reports shut, so the
  // frame draws neither chip and labels its code button "Show".
  const codePanelOpen = !embedded && sideOpen && nodeTab === "code" && node !== null;
  // The pane shares the row with the frame (it never covers it), so the
  // width it reports over the frame is always 0: the frame's collapse chip
  // sits at its own right edge.
  // Written after commit, not during render: a discarded render must not
  // leave a pane state the message handler reads that was never shown.
  const paneOpen = !embedded && sideOpen;
  const sidePanelStateRef = useRef({ open: paneOpen, code: codePanelOpen, width: 0 });
  useLayoutEffect(() => {
    sidePanelStateRef.current = { open: paneOpen, code: codePanelOpen, width: 0 };
  }, [paneOpen, codePanelOpen]);
  const sendCodePanelState = useCallback(() => {
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    frame.contentWindow.postMessage(
      { type: "fused-embed-side-panel", ...sidePanelStateRef.current },
      origin,
    );
  }, []);
  useEffect(() => {
    sendCodePanelState();
  }, [sideOpen, codePanelOpen, embedded, sendCodePanelState]);

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

  // Coming back to this canvas — the native window focused, or its tab shown
  // again — asks the watcher to probe the server now rather than on its
  // next timed poll, so a canvas edited elsewhere meanwhile shows its Pull
  // button as the user looks (the hosted workbench tab does the same on
  // focus). Detect-only; the verdict lands as the usual sync state. At most
  // once per FOCUS_CHECK_MIN_MS, and not while another process owns the clone.
  const lastFocusCheckRef = useRef(0);
  useEffect(() => {
    if (owned) return;
    const check = () => {
      if (!hostFocused()) return;
      const now = Date.now();
      if (now - lastFocusCheckRef.current < FOCUS_CHECK_MIN_MS) return;
      lastFocusCheckRef.current = now;
      void checkRemote(env, name).catch(() => undefined);
    };
    window.addEventListener("focus", check);
    document.addEventListener("visibilitychange", check);
    return () => {
      window.removeEventListener("focus", check);
      document.removeEventListener("visibilitychange", check);
    };
  }, [env, name, owned]);

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
  // relay is queued behind it, since that one carries the full state anyway,
  // and rejected when the window runs out with nothing newer. Anything else
  // is a rejection the embed drops, reason attached.
  // Relays run strictly IN ORDER through one promise chain: the frame posts
  // seq 1, 2, 3 as it autosaves, and a 409 retry on seq 1 must not let seq 2
  // reach the clone first — the server would then read seq 1's older full
  // state as deletions of seq 2's nodes. Same chain for the rename hint,
  // which must land before the snapshot carrying the new name.
  //
  // Every relay is an entry in `relayQueue` (the embed_to_disk leg) from the
  // moment the frame's message arrives until it is applied, rejected or
  // abandoned. A relay SUPERSEDED by a newer one is answered
  // `fused-embed-snapshot-applied` with `stale: true` and the last push_seq
  // this page saw: the newer snapshot carries the full state, so the embed's
  // bridge may settle this seq instead of holding it as pending for good. One
  // whose busy window EXPIRED with nothing newer is rejected (see `abandon`).
  const relayChainRef = useRef<Promise<void>>(Promise.resolve());
  const relayTicketRef = useRef(0);
  const enqueueRelay = useCallback((gen: number, seq: number): number => {
    relayIdRef.current += 1;
    const id = relayIdRef.current;
    setRelayQueue((q) => [...q, { id, gen, seq, at: Date.now(), busy: false }]);
    return id;
  }, []);
  const settleRelay = useCallback((id: number) => {
    setRelayQueue((q) => q.filter((e) => e.id !== id));
  }, []);
  const relaySnapshot = useCallback(
    async (
      qid: number,
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
      // Two different abandonments, two different answers:
      //   superseded — a newer relay is queued behind this one and carries
      //     the FULL state, so nothing is lost: a stale-applied ack settles
      //     it on the embed, strip as is.
      //   expired — 120 s of busy with nothing newer: this change is NOT on
      //     disk. A REJECTION (an applied ack, stale or not, reads as saved
      //     on the embed — its relay drops `stale`), and red on the strip.
      const abandon = (why: "superseded" | "expired") => {
        settleRelay(qid);
        if (why === "expired") {
          setRelayFailure({ kind: "expired", detail: "" });
          post({
            type: "fused-embed-snapshot-rejected",
            gen,
            seq,
            reason: "the clone stayed busy for 2 min; edit again to retry",
          });
          return;
        }
        post({
          type: "fused-embed-snapshot-applied",
          gen,
          seq,
          push_seq: lastPushSeqRef.current ?? 0,
          stale: true,
        });
      };
      const ticket = relayTicketRef.current;
      const started = Date.now();
      let markedBusy = false;
      for (;;) {
        try {
          const ack = rename
            ? await applyRename(env, name, gen, seq, rename)
            : await applySnapshot(env, name, gen, seq, body ?? {});
          settleRelay(qid);
          setRelayFailure(null);
          post({ type: "fused-embed-snapshot-applied", gen, seq, push_seq: ack.push_seq });
          return;
        } catch (e) {
          const status = (e as { status?: number }).status;
          if (status === 409) {
            if (relayTicketRef.current !== ticket) return abandon("superseded"); // a newer relay waits
            if (Date.now() - started > SNAPSHOT_BUSY_WINDOW_MS) return abandon("expired");
            if (!markedBusy) {
              markedBusy = true;
              setRelayQueue((q) => q.map((x) => (x.id === qid ? { ...x, busy: true } : x)));
            }
            await new Promise((r) => window.setTimeout(r, SNAPSHOT_BUSY_RETRY_MS));
            continue;
          }
          const reason = (e as Error).message || "snapshot rejected";
          settleRelay(qid);
          setRelayFailure({ kind: "rejected", detail: reason });
          post({ type: "fused-embed-snapshot-rejected", gen, seq, reason });
          return;
        }
      }
    },
    [env, name, settleRelay],
  );

  // The body channel (module doc): one file's text, compare-and-swap on the
  // hash the frame names. Runs on the snapshot chain for ORDER only — it
  // never touches `relayTicketRef` (a body must not make a busy snapshot read
  // itself as superseded). Busy answers retry inside a short window, then
  // come back as a `busy` rejection the frame retries on its own.
  const relayBody = useCallback(
    async (seq: number, body: { node: string; file: string; base_hash: string; text: string }) => {
      const post = (msg: Record<string, unknown>) => {
        const frame = frameRef.current;
        const origin = baseOriginRef.current;
        if (!frame?.contentWindow || !origin) return;
        frame.contentWindow.postMessage(msg, origin);
      };
      const started = Date.now();
      for (;;) {
        try {
          const ack = await applyBody(env, name, body);
          if (ack.ok) {
            post({ type: "fused-embed-body-applied", seq, name: body.node, hash: ack.hash });
          } else {
            post({
              type: "fused-embed-body-rejected",
              seq,
              name: body.node,
              code: ack.code,
              reason: "the file changed on disk",
              disk_text: ack.disk_text,
              disk_hash: ack.disk_hash,
            });
          }
          return;
        } catch (e) {
          const status = (e as { status?: number }).status;
          if (status === 409 && Date.now() - started < BODY_BUSY_WINDOW_MS) {
            await new Promise((r) => window.setTimeout(r, SNAPSHOT_BUSY_RETRY_MS));
            continue;
          }
          const reason = (e as Error).message || "body rejected";
          post({
            type: "fused-embed-body-rejected",
            seq,
            name: body.node,
            code: status === 409 ? "busy" : "rejected",
            reason,
          });
          return;
        }
      }
    },
    [env, name],
  );

  // The delta leg: disk moved → fetch diff(disk, what the frame holds) → post
  // it to the frame. One relay at a time, in order, through a promise chain
  // (a full state must not overtake the delta cut before it). A held or busy
  // answer is retried on a timer; the frame's hello asks for a full state.
  const deltaChainRef = useRef<Promise<void>>(Promise.resolve());
  const lastDeltaRevRef = useRef<number | null>(null);
  // A full state was sent to heal a refused delta and no delta has been
  // applied since: a second refusal does not heal again (no loop).
  const deltaHealRef = useRef(false);
  const relayDelta = useCallback(
    (full: boolean) => {
      deltaChainRef.current = deltaChainRef.current.then(async () => {
        const started = Date.now();
        for (;;) {
          try {
            const delta = await getDelta(env, name, full);
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
              // No frame to tell: drop the cut, unacked. The frame's hello
              // asks for a full state whatever the server thinks it holds,
              // and a refusal here would paint the strip red ("the workbench
              // refused the delta") for a frame that was simply not there.
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
    [env, name],
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
  reselectShownNodeRef.current = () => {
    if (nodeRef.current) sendSelect(nodeRef.current.udfName);
  };
  // An open-code for a node not yet shown: the pane switches to Code only
  // once the workbench's selection echo has set that node, so the old node's
  // code never shows in between (and a refused select changes nothing).
  const pendingCodeRef = useRef<string | null>(null);

  // A HOP ONTO THIS CANVAS WHILE IT IS MOUNTED — the Tasks page's door on
  // another node of the canvas the reader is already on, a notification —
  // changes the URL and nothing else: App.tsx keys this component on (env,
  // name) alone, so `nodeFromUrl` never re-runs and the new `?node=` +
  // `session_id` would go unread. The router announces every push on
  // NAV_EVENT (the node effect's own replaceState writes never do), so re-read
  // them there: select the node in the workbench, remember the session under
  // it and reboot the pane; the same node with a new session reboots alone.
  // A hop to the bare canvas page deselects, and `navHopRef` keeps the node
  // effect from clearing the `session_id` that hop carries for the canvas
  // chat, which reads it off the URL as it mounts.
  const navHopRef = useRef(false);
  useEffect(() => {
    const onNav = () => {
      // Every push announces here, including the ones that LEAVE: the hop
      // to another canvas, or to /tasks, reaches this still-mounted instance
      // first. Only this canvas's own URL is ours to read — otherwise B's
      // session would be filed under A's node of the same name, and the old
      // frame told to deselect on the way out.
      const here = canvasNameFromPath(window.location.pathname);
      if (!here || here.env !== env || here.name !== name) return;
      const next = nodeFromUrl();
      // A URL naming no node states the pane's own shut/open (`side=off`,
      // sideOpenFromUrl); one naming a node asks to see its pane (below).
      if (!next) {
        // Shutting hides the node pane: a Code buffer is flushed first.
        if (sideOpenFromUrl()) setSideOpen(true);
        else afterNodeCodeFlush(() => setSideOpen(false));
      }
      const had = next ? nodeSessions.get(nodeSessionKey(env, name, next.file)) : undefined;
      const sid = next ? seedNodeSessionFromUrl(env, name, next) : null;
      const cur = nodeRef.current;
      if (!next) {
        if (!cur) {
          // Already on the canvas chat: a URL naming a session it did not
          // mount on — another one, or none after Back — remounts it; the
          // mount reads `session_id` off the URL as it is now.
          const want = new URLSearchParams(window.location.search).get("session_id");
          if (want !== canvasChatSidRef.current) {
            canvasChatSidRef.current = want;
            setCanvasChatSeq((n) => n + 1);
          }
          return;
        }
        canvasChatSidRef.current = new URLSearchParams(window.location.search).get("session_id");
        navHopRef.current = true;
        sendSelect(null);
        afterNodeCodeFlush(() => setNode(null));
        return;
      }
      // A link naming a node is a request to see its pane: a shut one opens.
      setSideOpen(true);
      if (cur && cur.file === next.file) {
        // The same node: the link's tab wins (a Code tab left open would
        // otherwise hide the chat the task named), and a session the pane
        // is not on reboots it.
        afterNodeCodeFlush(() => {
          setNodeTab(nodeTabFromUrl());
          if (sid && sid !== had) setNodeBootSeq((n) => n + 1);
        });
        return;
      }
      // Marked as a hop so the node effect leaves the `session_id` the link
      // carries on the URL: the pane boots off the seeded map either way,
      // but the address should still restore the task's session on reload.
      navHopRef.current = true;
      sendSelect(next.udfName);
      afterNodeCodeFlush(() => {
        setNodeTab(nodeTabFromUrl());
        setNodeBootSeq((n) => n + 1);
        setNode(next);
      });
    };
    // Back / Forward move the URL without a push: `popstate`, which the
    // router does not re-announce, lands on this same reader.
    window.addEventListener(NAV_EVENT, onNav);
    window.addEventListener("popstate", onNav);
    return () => {
      window.removeEventListener(NAV_EVENT, onNav);
      window.removeEventListener("popstate", onNav);
    };
  }, [env, name, sendSelect, afterNodeCodeFlush]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== baseOriginRef.current) return;
      const type = event.data?.type;
      if (type === "fused-embed-auth-ready" || type === "fused-embed-auth-refresh") {
        void seedToken();
        if (type === "fused-embed-auth-ready") {
          sendHostFocus();
          sendCodePanelState();
          setRepostSeq((n) => n + 1); // a fresh frame has no sync state yet
        }
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
        const qid = enqueueRelay(gen, seq);
        relayChainRef.current = relayChainRef.current.then(() =>
          relaySnapshot(qid, gen, seq, body as Record<string, unknown>),
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
        const qid = enqueueRelay(gen, seq);
        relayChainRef.current = relayChainRef.current.then(() =>
          relaySnapshot(qid, gen, seq, null, {
            from,
            to,
            file_from: typeof file_from === "string" ? file_from : null,
            file_to: typeof file_to === "string" ? file_to : null,
          }),
        );
      }
      if (type === "fused-embed-body") {
        // From THE workbench frame only, like the snapshot: it writes a file.
        if (event.source !== frameRef.current?.contentWindow) return;
        const { seq, name: node, file, base_hash, text } = (event.data ?? {}) as {
          seq?: unknown; name?: unknown; file?: unknown; base_hash?: unknown; text?: unknown;
        };
        if (typeof seq !== "number" || typeof node !== "string" || !node) return;
        if (typeof file !== "string" || typeof base_hash !== "string" || typeof text !== "string") return;
        const body = { node, file, base_hash, text };
        relayChainRef.current = relayChainRef.current.then(() => relayBody(seq, body));
      }
      if (type === "fused-embed-capture-done") {
        // From THE workbench frame only: a same-origin window that guessed
        // the pending `req` must not settle it with its own picture.
        if (event.source !== frameRef.current?.contentWindow) return;
        const { req, ok, error, blob } = (event.data ?? {}) as {
          req?: unknown; ok?: unknown; error?: unknown; blob?: unknown;
        };
        // Only the answer to OUR request: a stale one (after the timer gave
        // up) must not settle a later request.
        if (!shotPendingRef.current || req !== shotPendingRef.current.req) return;
        if (ok === true && blob instanceof Blob) settleShot({ ok: true, blob });
        else {
          settleShot({
            ok: false,
            error: typeof error === "string" ? error : "the workbench sent no picture",
          });
        }
      }
      if (type === "fused-embed-hello") {
        frameUpRef.current = true;
        const waiters = frameUpWaitersRef.current;
        frameUpWaitersRef.current = [];
        for (const wake of waiters) wake();
        // The frame (re)hydrated from the server: bring it to disk with a
        // full state, whatever the server thought it held before.
        relayDelta(true);
        sendHostFocus();
        sendCodePanelState();
        setRepostSeq((n) => n + 1);
      }
      if (type === "fused-embed-delta-applied" || type === "fused-embed-delta-rejected") {
        const rev = event.data?.rev;
        if (typeof rev !== "number") return;
        // Through the SNAPSHOT chain, not fire-and-forget: the frame posts a
        // snapshot before it applies a delta, and the server adopts the
        // acked reading as "what the frame holds". An ack that overtook that
        // snapshot (the snapshot route answers busy while a force pull or a
        // pause holds the clone; the ack route waits for it) would make the server
        // read a node the delta added as one the snapshot deleted.
        const ok = type === "fused-embed-delta-applied";
        const said = event.data?.reason;
        const reason = ok ? undefined : typeof said === "string" ? said : "refused";
        // A frame still booting refuses every delta ("not hydrated yet"):
        // its hello asks for the full state once it hydrates, so that is
        // no error and needs no heal (the server ignores the reason too).
        const booting = reason === "not hydrated yet";
        if (ok) deltaHealRef.current = false;
        relayChainRef.current = relayChainRef.current.then(() =>
          ackDelta(env, name, rev, ok, reason).then(() => undefined, () => undefined),
        );
        // A REAL refusal: the server has dropped its picture of the frame
        // and marks the leg red. Send a full state once, right away, so the
        // error heals itself — once only: a full state refused again stays
        // red (until the next applied delta) rather than looping.
        if (!ok && !booting && !deltaHealRef.current) {
          deltaHealRef.current = true;
          relayChainRef.current = relayChainRef.current.then(() => relayDelta(true));
        }
      }
      if (type === "fused-embed-selection") {
        // The viewer has no pane to follow a selection into (see `embedded`);
        // ignoring it here also keeps `?node=` off the project page's URL.
        if (embedded) return;
        const name = event.data?.udfName;
        if (typeof name === "string" && name) {
          const kind = event.data?.kind === "widget" ? "widget" : "udf";
          const stated = event.data?.file;
          const file =
            typeof stated === "string" && stated && !stated.includes("/")
              ? stated
              : `${name}.${kind === "widget" ? "json" : "py"}`;
          // The node already shown: nothing to do — and nothing to FLUSH.
          // The embed re-posts its selection after every delta it applies
          // (endEmbedRehydrate), the one from the Code tab's own save
          // included; flushing on that echo would save (and run) a buffer
          // the user is still typing into.
          const cur = nodeRef.current;
          if (!cur || cur.udfName !== name || cur.file !== file) {
            // Another node: its pane replaces this one, so the Code tab's
            // buffer (a blur save may be in flight) is flushed first.
            afterNodeCodeFlush(() => setNode({ udfName: name, kind, file }));
          }
          if (pendingCodeRef.current === name) {
            pendingCodeRef.current = null;
            setSideOpen(true);
            setNodeTab("code");
          }
        } else {
          pendingCodeRef.current = null;
          // The canvas deselected (pane click, folder node, multi-select, or
          // the node is gone after a re-hydrate): follow it back to the
          // canvas chat. Owner call — the canvas is the one source of truth
          // for what the right pane is about. The Code tab's buffer is
          // flushed first: the click that deselected also blurred the
          // editor, whose save is on its way — the save is what runs the
          // node (owner: "the code is saved when you click outside the
          // node"), and an unmounting frame would drop it.
          afterNodeCodeFlush(() => setNode(null));
        }
      }
      if (type === "fused-embed-open-code") {
        // The node toolbar's "Show code panel" (header). The workbench frame's
        // own window only, on top of the origin check above.
        if (embedded) return;
        if (!frameRef.current || event.source !== frameRef.current.contentWindow) return;
        const name = event.data?.udfName;
        if (typeof name !== "string" || !name) return;
        // That node's Code tab already up: the button is its "Hide".
        if (sidePanelStateRef.current.code && nodeRef.current?.udfName === name) {
          afterNodeCodeFlush(() => setSideOpen(false));
          return;
        }
        // Not the node shown: select it in the workbench, whose selection
        // echo (above) is what sets `node` — it alone states the node's kind
        // and file — and opens the Code tab then (`pendingCodeRef`).
        if (nodeRef.current?.udfName !== name) {
          pendingCodeRef.current = name;
          sendSelect(name);
          return;
        }
        setSideOpen(true);
        setNodeTab("code");
      }
      if (type === "fused-embed-delete") {
        // The canvas menu's confirmed "Delete canvas" (header). The editor
        // frame's own window only; the project page's viewer never sends it.
        if (embedded) return;
        if (!frameRef.current || event.source !== frameRef.current.contentWindow) return;
        const reject = (reason: string) => {
          const origin = baseOriginRef.current;
          if (!origin) return;
          frameRef.current?.contentWindow?.postMessage(
            { type: "fused-embed-delete-rejected", reason },
            origin,
          );
        };
        if (event.data?.name !== name) {
          reject(`this window holds "${name}", not "${String(event.data?.name ?? "")}"`);
          return;
        }
        void deleteCanvas(env, name)
          .then(() => {
            // `replace`: Back must not land on the deleted canvas's editor.
            navigateUrl(catalogPath(env), { replace: true });
          })
          .catch((e) => {
            setError(`Delete canvas failed: ${(e as Error).message}`);
            reject((e as Error).message);
          });
        return;
      }
      if (type === "fused-embed-open-side" || type === "fused-embed-close-side") {
        // The workbench's right-edge chips (header). A close also drops a
        // Code request still waiting on its selection echo: the close wins.
        if (embedded) return;
        if (!frameRef.current || event.source !== frameRef.current.contentWindow) return;
        const open = type === "fused-embed-open-side";
        if (!open) pendingCodeRef.current = null;
        if (open) setSideOpen(true);
        else afterNodeCodeFlush(() => setSideOpen(false));
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [seedToken, sendSelect, sendHostFocus, sendCodePanelState, relaySnapshot, relayBody, relayDelta, enqueueRelay, env, name, embedded, settleShot, afterNodeCodeFlush]);

  // The hosted editor's URL, for the project page's header (`onWorkbenchUrl`).
  const workbenchUrl =
    workbenchBase && handle
      ? `${workbenchBase}/workbench/${encodeURIComponent(handle)}/${encodeURIComponent(name)}`
      : null;
  useEffect(() => {
    onWorkbenchUrl?.(workbenchUrl);
  }, [workbenchUrl, onWorkbenchUrl]);

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
    if (nodeEffectRanRef.current && prevNodeKeyRef.current !== key && !navHopRef.current) {
      url.searchParams.delete("session_id");
      url.searchParams.delete("run");
    }
    navHopRef.current = false;
    nodeEffectRanRef.current = true;
    prevNodeKeyRef.current = key;
    if (url.href !== window.location.href) {
      window.history.replaceState(window.history.state, "", url);
    }
  }, [node, nodeTab]);

  // …and the right pane's shut state (`side=off`, see `sideOpenFromUrl`).
  useEffect(() => {
    if (embedded) return;
    const url = new URL(window.location.href);
    if (sideOpen) url.searchParams.delete("side");
    else url.searchParams.set("side", SIDE_OFF);
    if (url.href !== window.location.href) {
      window.history.replaceState(window.history.state, "", url);
    }
  }, [sideOpen, embedded]);

  // After every push, tell the workbench's save chip it is published. This
  // is informational only — the frame never reloads or re-fetches on it (the
  // delta leg above is how disk changes reach it).
  const lastPushSeqRef = useRef<number | null>(null);
  useEffect(() => {
    const seq = sync?.legs.disk_to_server.push_seq ?? null;
    if (seq === null) return;
    const prev = lastPushSeqRef.current;
    lastPushSeqRef.current = seq;
    if (prev === null || seq <= prev) return;
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    frame.contentWindow.postMessage({ type: "fused-embed-pushed", push_seq: seq }, origin);
  }, [sync?.legs.disk_to_server.push_seq]);

  // Self-heal: a state saying no watcher runs here (a server restart, a
  // stop) re-arms it through sync/start — which answers `owned` instead if
  // another process took the clone meanwhile. Throttled.
  const lastHealRef = useRef(0);
  const heal = useCallback(() => {
    if (Date.now() - lastHealRef.current < HEAL_MIN_INTERVAL_MS) return;
    lastHealRef.current = Date.now();
    void ensureSync().catch(() => undefined);
  }, [ensureSync]);
  useEffect(() => {
    if (sync && noWatcher(sync)) heal();
  }, [sync, heal]);

  // The sync state, live: WS /api/canvases/sync/events pushes a `state` on
  // every change — a disk_rev bump reaches the delta effect above at once —
  // and `log` events for the drawer. While the socket is down the page polls sync/state every
  // SYNC_POLL_MS and reconnects with backoff. Nothing runs while another
  // process owns the clone (or before sync/start has answered).
  const syncLive = !!dir && !owned;
  useEffect(() => {
    if (!syncLive) return;
    let closed = false;
    let sock: WebSocket | null = null;
    let retryTimer: number | null = null;
    let pollTimer: number | null = null;
    let retryMs = 0;
    let failures = 0;
    const pollOnce = () => {
      void getSyncState(env, name).then(
        (s) => {
          failures = 0;
          if (!closed) applyState(s);
        },
        () => {
          // A few misses in a row: treat it as a dropped watcher.
          failures += 1;
          if (!closed && failures >= FAILED_POLLS_BEFORE_RELEASE) {
            failures = 0;
            heal();
          }
        },
      );
    };
    const startPolling = () => {
      if (pollTimer === null) pollTimer = window.setInterval(pollOnce, SYNC_POLL_MS);
    };
    const stopPolling = () => {
      if (pollTimer !== null) window.clearInterval(pollTimer);
      pollTimer = null;
    };
    const scheduleReconnect = () => {
      if (closed || retryTimer !== null) return;
      retryMs = Math.min(WS_RETRY_MAX_MS, retryMs ? retryMs * 2 : WS_RETRY_MIN_MS);
      retryTimer = window.setTimeout(connect, retryMs);
    };
    const connect = () => {
      retryTimer = null;
      let s: WebSocket;
      try {
        s = new WebSocket(syncEventsUrl(env, name));
      } catch {
        startPolling();
        scheduleReconnect();
        return;
      }
      sock = s;
      s.onopen = () => {
        retryMs = 0;
        stopPolling();
        setLive(true);
        pollOnce(); // whatever moved while the socket was down
      };
      s.onmessage = (ev) => {
        let msg: unknown;
        try {
          msg = JSON.parse(String(ev.data));
        } catch {
          return;
        }
        if (!msg || typeof msg !== "object") return;
        const m = msg as { type?: unknown; state?: unknown; event?: unknown };
        if (m.type === "state" && m.state && typeof m.state === "object") {
          applyState(m.state as SyncState);
        } else if (m.type === "log" && m.event && typeof m.event === "object") {
          wsLogAtRef.current = Date.now();
          setLogEvents((buf) => mergeLogEvents(buf, [m.event as SyncLogEvent]));
        }
        // `ping` (heartbeat) and anything unknown: nothing to do.
      };
      // `error` is always followed by `close`, which does the bookkeeping.
      s.onerror = () => {};
      s.onclose = () => {
        if (sock === s) sock = null;
        if (closed) return;
        setLive(false);
        startPolling();
        scheduleReconnect();
      };
    };
    pollOnce(); // first paint does not wait for the socket
    connect();
    return () => {
      closed = true;
      stopPolling();
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      sock?.close();
      setLive(false);
    };
  }, [syncLive, env, name, applyState, heal]);

  // The log ring restarts with a new watcher (its seqs count from 0 again):
  // drop the drawer's buffer when the epoch moves.
  const logEpochRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const epoch = sync?.watcher_epoch;
    if (epoch === undefined) return;
    if (logEpochRef.current !== undefined && logEpochRef.current !== epoch) setLogEvents([]);
    logEpochRef.current = epoch;
  }, [sync?.watcher_epoch]);

  // "Fix with Claude" reads `fix_active` — set server-side the instant a fix
  // spawns, cleared only by that run's own completion (D336 follow-up),
  // never guessed from transcript activity. It rides sync/state; the local
  // copy only bridges the click → next state gap (`onFix` sets it at once).
  const pushError = sync?.legs.disk_to_server.state === "error";
  const stateFixActive = sync?.fix_active;
  useEffect(() => {
    if (stateFixActive !== undefined) setFixActive(stateFixActive);
  }, [stateFixActive]);

  // The merged state to the workbench frame (`fused-embed-sync-state`, the
  // embed's save chip reads it). Exact targetOrigin, like every post.
  const postSyncState = useCallback((state: MergedSyncState) => {
    const frame = frameRef.current;
    const origin = baseOriginRef.current;
    if (!frame?.contentWindow || !origin) return;
    frame.contentWindow.postMessage({ type: "fused-embed-sync-state", state }, origin);
  }, []);
  const onLogEvents = useCallback((events: SyncLogEvent[]) => {
    setLogEvents((buf) => mergeLogEvents(buf, events));
  }, []);

  // Splitter drag: track the pointer over the whole window so the drag
  // survives entering the iframes (which would otherwise swallow mousemove —
  // pointer-events are disabled on both panes while dragging).
  useEffect(() => {
    if (!dragging) return;
    // Seeded from the seam's position as the drag starts, so the first tick
    // pans like every later one.
    let prevFrac: number | null = leftFracRef.current;
    const onMove = (e: MouseEvent) => {
      const row = rowRef.current;
      if (!row) return;
      const rect = row.getBoundingClientRect();
      const frac = Math.min(0.85, Math.max(0.15, (e.clientX - rect.left) / rect.width));
      setLeftFrac(frac);
      // Half the pane's width change, to the canvas (header: fused-embed-pan)
      // — from the drag itself, so a window resize mid-drag never pans twice.
      if (prevFrac !== null && frac !== prevFrac) {
        const frame = frameRef.current;
        const origin = baseOriginRef.current;
        if (frame?.contentWindow && origin) {
          frame.contentWindow.postMessage(
            { type: "fused-embed-pan", dx: (rect.width * (frac - prevFrac)) / 2, dy: 0 },
            origin,
          );
        }
      }
      prevFrac = frac;
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
      const { run_id } = await fixWithClaude(env, name);
      // Don't wait for the next sync state to reflect that a fix is now
      // running: the gap would let a double-click through to a second POST
      // (the server answers it 409, a confusing thing to show right after a
      // successful click).
      setFixActive(true);
      if (embedded) {
        // The project page has no chat to hand the run to: the fix is a
        // task on the clone, so the Tasks tab is where it shows. No `run=`
        // on this page's URL — nothing here reads it (CodeRabbit on #1795).
        navigateUrl(canvasProjectPath(env, name, { tab: "tasks" }));
        return;
      }
      // The fix runs on the CLONE (canvases.py spawns on the dir), so it is
      // the canvas chat's to show: leave the node, on both sides.
      if (nodeRef.current) {
        sendSelect(null);
        afterNodeCodeFlush(() => setNode(null));
      }
      // …and shown: the button now says "see chat →", so a shut pane opens.
      setSideOpen(true);
      setFixRunId(run_id);
    } catch (e) {
      setFixError((e as Error).message);
    } finally {
      setFixBusy(false);
    }
  };

  // The workbench frame loads only once THIS process's watcher runs on the
  // clone (sync/start answered): before that, and while another process owns
  // the clone, every snapshot it posted would meet a 409 and sit in the
  // relay's busy loop — the relay running in all but name.
  const frameSrc =
    baseUrl && handle && dir && !owned
      ? `${baseUrl}/workbench/${encodeURIComponent(handle)}/${encodeURIComponent(name)}?fused_embed_auth=1&fused_embed_v=3` +
        // The viewer flag rides the flagged URL only; a flagged URL without
        // it is the editor, and the workbench records that per load
        // (application embed-auth.ts), so the editor never inherits it.
        (embedded ? "&fused_embed_readonly=1" : "")
      : null;
  // A frame that reloads (new src) is down again until its next hello.
  useEffect(() => {
    frameUpRef.current = false;
  }, [frameSrc]);

  // No header bar (owner call): the workspace is the two panes, full height,
  // under ONE line — the sync strip (SyncStrip.tsx): the worst of the four
  // sync legs, "Published 12 s ago" when all is well, the Pull button when
  // the server moved, and the sync log drawer on click.
  const s2dState = sync?.legs.server_to_disk.state;

  // The node tabs' icons (SideChrome's SideTabs: icon + label): the shown
  // node's own templates once its stat has answered, the clone dir's before
  // that (same glyphs, no empty slot while the probe runs); nothing until
  // either answers.
  const nodeIcons =
    nodeTemplates && nodePath !== null && nodeTemplates.path === nodePath ? nodeTemplates.templates : dirTemplates;
  const pushLeg = sync?.legs.disk_to_server;

  // The sync chrome. In the editor it sits above the panes (the strip, the
  // drawer on click, the failed-push banner). Embedded, it goes to the
  // project page's Sync tab (`syncSlot`) — the log pinned open, since the
  // tab IS the log — and nothing of it over the Overview.
  const pinned = embedded;
  // The page's own errors (no handle, sync/start failing) stay with the
  // frame on EVERY tab, embedded or not: a frame stuck on "Loading
  // workbench…" must say why where the reader is looking, not on the Sync
  // tab (CodeRabbit on #1795).
  const banners = (
    <>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {startError && !error && (
        <ErrorBanner>{`Could not start syncing this canvas (retrying): ${startError}`}</ErrorBanner>
      )}
    </>
  );
  const chrome = (
    <>
      {!embedded && banners}
      {/* Always up ("Connecting…" before the first state): it carries the
          way back to the listing, which must not wait on sync. */}
      <SyncStrip
        name={name}
        env={env}
        server={sync}
        queue={relayQueue}
        failure={relayFailure}
        owned={owned}
        live={live}
        open={pinned || drawerOpen}
        // No drawer to open before the first state: arming it now would pop
        // it unprompted when that state lands. Pinned, there is nothing to
        // toggle.
        onToggle={() => {
          if (!pinned && (sync || owned)) setDrawerOpen((o) => !o);
        }}
        // Back to the environment this canvas lives in, not the default.
        onBack={() => navigateUrl(catalogPath(env))}
        workbenchUrl={workbenchUrl}
        embedded={embedded}
        pull={{
          busy: pullBusy,
          error: pullError,
          disabled: pullBusy || s2dState === "pulling" || pushLeg?.state === "pushing",
          onPull: () => void onPull(),
        }}
      />
      {(pinned || drawerOpen) && (sync || owned) && (
        <SyncDrawer
          env={env}
          name={name}
          epoch={sync?.watcher_epoch ?? null}
          events={logEvents}
          wsLogAtRef={wsLogAtRef}
          onEvents={onLogEvents}
          hardPull={{
            busy: pullBusy,
            error: pullError,
            // Also held while a snapshot relay is queued: applied after the
            // pull it would write the discarded canvas back (the server
            // fences such snapshots too; this keeps the common case clean).
            disabled:
              !!owned ||
              !sync ||
              pullBusy ||
              relayQueue.length > 0 ||
              s2dState === "pulling" ||
              pushLeg?.state === "pushing",
            onPull: () => void onPull(true),
          }}
          {...(pinned ? {} : { onClose: () => setDrawerOpen(false) })}
        />
      )}
      {pushError && pushLeg && sync && (
        // The disk_to_server leg's error, in full: the strip above carries
        // the one-line reason; this is the CLI's per-line detail and the
        // Fix with Claude door.
        <div
          style={{
            padding: "8px 12px",
            borderBottom: "1px solid rgba(220,60,60,0.35)",
            background: "rgba(220,60,60,0.08)",
            fontSize: 13,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
            <strong style={{ minWidth: 0, overflowWrap: "anywhere" }}>
              {pushLeg.reason ?? "Publishing failed"}
            </strong>
            {/* Claude's feature (D17): no door while Claude is switched off. */}
            {!claudeOff && <button
              className="btn btn-primary"
              disabled={fixBusy || fixActive}
              onClick={() => void onFix()}
              title="Start a Claude session on the local clone, primed with these errors"
            >
              {fixActive
                ? embedded
                  ? "Claude is on it — see Tasks →"
                  : "Claude is on it — see chat →"
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
    </>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", minWidth: 0 }}>
      {embedded ? (
        <>
          {banners}
          {syncSlot ? createPortal(chrome, syncSlot) : null}
        </>
      ) : (
        chrome
      )}
      {frameSrc && (
        <SyncFramePoster
          name={name}
          server={sync}
          queue={relayQueue}
          failure={relayFailure}
          repost={repostSeq}
          post={postSyncState}
        />
      )}
      <div
        ref={rowRef}
        // The canvas frame and the right pane share the row: the pane never
        // covers the workbench, so its header, chips and toolbars stay in
        // view whatever the pane's width.
        style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "row" }}
      >
        <div
          // The wrapper, not the iframe: a frame swallows pointer events, but
          // the wrapper's mouseenter still fires as the pointer crosses in.
          onMouseEnter={focusCanvasOnEnter}
          style={{
            // The viewer has no right pane, and a shut pane leaves the row to
            // the canvas; up, the seam's drag sets the split (`leftFrac`).
            flex: embedded || !sideOpen ? "1 1 auto" : `0 0 ${leftFrac * 100}%`,
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
              // Cross-origin frame: without this the workbench's
              // navigator.clipboard calls (Copy link, copy/paste UDFs) reject.
              allow="clipboard-read; clipboard-write"
              // The custom env's embed proxy admits this frame's load only
              // with the app's origin as its Referer (canvas_embed_proxy.py),
              // so the policy is spelled out here rather than inherited.
              referrerPolicy="origin"
              /* NO `data-fused-annotate-target` here, deliberately: the canvas
                 chat gets this frame as a getter (`annotateTarget` below), and
                 the explorer engine in the right pane finds its own content
                 frame by that attribute — a marked workbench frame earlier in
                 the DOM would win the query, a cross-origin frame the file
                 chat can neither read nor pin into. */
              style={{ width: "100%", height: "100%", border: 0 }}
            />
          ) : (
            !error &&
            (owned ? (
              <p style={{ padding: 16, opacity: 0.8 }}>
                Another Fused process (pid {owned.pid}, port {owned.port}) is syncing this
                canvas. Close it there and this page picks the canvas up on its own.
              </p>
            ) : (
              <p style={{ padding: 16 }}>Loading workbench…</p>
            ))
          )}
        </div>
        {!embedded && sideOpen && (
          <div
            // The right pane's column: the seam and the pane, the rest of
            // the row past the canvas's share.
            style={{
              flex: "1 1 auto",
              minWidth: 0,
              display: "flex",
              flexDirection: "row",
              background: "var(--bg)",
            }}
          >
        <div
          // The explorer's preview/side seam, reused as-is: one hairline that
          // lights `--seam-hot` on hover and drag like every other draggable
          // seam in the app. It used to be a 6px grey band that turned blue
          // under the cursor — the only seam styled on its own.
          className={dragging ? "preview-side-divider dragging" : "preview-side-divider"}
          onMouseDown={(e) => {
            e.preventDefault();
            setDragging(true);
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
                      // Keyed on the tab: the switch remounts the frame, so
                      // a Code buffer is flushed first.
                      if (v === "claude" || v === "code") afterNodeCodeFlush(() => setNodeTab(v));
                    }}
                    className="side-tabs self-stretch"
                  >
                    <TabsList
                      variant="line"
                      aria-label="Node panel"
                      className="h-full justify-start gap-3 rounded-none border-b-0 p-0"
                    >
                      <TabsTrigger value="claude" className="flex-none px-2 text-[12px]">
                        {nodeIcons && nodeTabIcon("claude", nodeIcons) && (
                          <span className="mode-menu-icon" data-icon="inline-start">
                            {nodeTabIcon("claude", nodeIcons)}
                          </span>
                        )}
                        Claude
                      </TabsTrigger>
                      <TabsTrigger value="code" className="flex-none px-2 text-[12px]">
                        {nodeIcons && nodeTabIcon("code", nodeIcons) && (
                          <span className="mode-menu-icon" data-icon="inline-start">
                            {nodeTabIcon("code", nodeIcons)}
                          </span>
                        )}
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
                  ref={nodeFrameRef}
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
            // `run` and `session_id` live on the `/canvases/<env>/<name>` URL, which is
            // why `paramsSource` is "url" and why the effect above writes `run`
            // there. It is also handed over directly as `runId`, so the mount
            // does not depend on that write having landed first.
            //
            // The annotate target is the SIBLING workbench iframe; the host
            // hands the element over. The mark stays on that iframe for PR3 to
            // read (below).
            <ChatMount
              key={(fixRunId ?? "chat") + ":" + canvasChatSeq}
              file={dir}
              chatOnly
              paramsSource="url"
              {...(fixRunId ? { runId: fixRunId } : {})}
              annotateTarget={() => frameRef.current}
              // The Screenshot seat photographs the canvas through the frame's
              // own capture (`requestCanvasShot`), not a cross-origin DOM walk.
              canvasShot={canvasShot}
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
                // The hop's session is what the node pane boots on — the same
                // memory the frame's own moves feed, so one source of truth.
                const key = nodeSessionKey(env, name, hop.node.file);
                if (hop.sessionId) nodeSessions.set(key, hop.sessionId);
                else nodeSessions.delete(key);
                afterNodeCodeFlush(() => {
                  setNodeTab("claude");
                  setNodeBootSeq((n) => n + 1);
                  setNode(hop.node);
                });
              }}
            />
          ) : (
            !error && <p style={{ padding: 16 }}>Loading editor…</p>
          )}
        </div>
          </div>
        )}
      </div>
      <ShotViewer shot={hostViewing} paneNoun="canvas" onClose={() => setHostViewing(null)} />
    </div>
  );
}
