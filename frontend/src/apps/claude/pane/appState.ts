// LIVE APP STATE: what the agent can SEE of the running app (T:4663-5218).
//
// Without this the agent is editing a page it cannot look at: it has no way to
// tell whether its own change rendered, whether the console filled with errors,
// or whether the page went blank. Two channels close that, and both are served
// from one snapshot function:
//
//   push — rides along with each message (`block()` → the `<live-app-state>`
//          block composeOutgoing prepends).
//   pull — the agent's `app_state` MCP tool, answered from the poll loop
//          (useAppStateResponder), for after it edits something and the pushed
//          snapshot has gone stale.
//
// The two disagree on purpose, in three places — see `push`, `pull` and the
// console trim for what about. The push side is a block nobody asked for and
// pays for itself on every later turn; the pull side is an answer to a tool call
// and hands over everything it has.
//
// It is deliberately TEXT ONLY — structure, own-text, params, console lines —
// for the same reason the annotation anchors are (#372): a description Claude
// can map straight back to the app's HTML source beats pixels it has to guess
// from. Nothing here may break the chat: the framed document can be
// mid-navigation, absent, or cross-origin, so every access is guarded and a
// failure degrades to LESS state, never to a thrown send.
//
// SAME-ORIGIN DIRECT ACCESS, never postMessage (D3/D4, T:5247): the pane is
// `/render?path=…` on our own origin, so `frame.contentWindow.document` IS the
// app's document. Every read is inside a try/catch exactly as T's is.
import { uploadFile } from "@platform/lib/api";
import { APP_STATE_UNREADABLE } from "./paneUrl";

// ── caps (T:4686-4699) ──
/** Ring buffer: a live-reloading page logs forever. */
export const APP_STATE_MAX_LOGS = 50;
/** How many of those 50 ride a PUSHED block. The buffer is deliberately deeper
 *  than the wire: it answers "was that error already there before my edit",
 *  which reaches back further than one turn, and the pull channel (a tool call
 *  that asked) hands over all of it. */
export const APP_STATE_WIRE_LOGS = 12;
/** Per console line / text snippet. */
export const APP_STATE_MAX_TEXT = 300;
/** Elements in the DOM outline. */
export const APP_STATE_MAX_NODES = 60;
/** How deep the outline descends. */
export const APP_STATE_MAX_DEPTH = 4;
/** Text per OUTLINED element, deliberately tighter than a console line: this one
 *  is paid 60 times over, and the whole snapshot is meant to be a few KB rather
 *  than a transcription of the page's copy. */
export const APP_STATE_MAX_NODE_TEXT = 120;
/** The block's delimiter, duplicated in agent.py (which strips it back out of
 *  everything user-facing) — a drift is invisible until the chat starts showing
 *  the user a screenful of JSON they never typed (D146, T:4767). */
export const APP_STATE_TAG = "live-app-state";
/** Elements whose text is code, not content: listed, never quoted from (T:4974). */
const QUIET_TAGS = new Set(["script", "style", "noscript", "template"]);

/** Keys never reported to the model as app params (T:4961-4964). Each is this
 *  chat's own bookkeeping or /render's plumbing; telling the model the app is
 *  "running with" `session_id` and `split` is worse than saying nothing. */
export const CHAT_PARAMS = new Set([
  "_file",
  "_mode",
  "session_id",
  "run",
  "split",
  "model",
  "effort",
  "permission",
  "annotations",
  "annmode",
  "leftmode",
  "paneview",
  "chat_only",
  "compact",
  "path",
  "msg",
]);

export interface AppLogEntry {
  level: string;
  text: string;
  source?: string;
  line?: number;
  ts: number;
}

/** One node of the structural outline. `path` is annPathOf's identifier — the
 *  SAME one the annotation anchors use, so a pin and an outline node name the
 *  same element (T:4977-4990). */
export interface OutlineNode {
  tag: string;
  id?: string;
  path?: string;
  class?: string;
  text?: string;
  /** Elision, always REPORTED: truncation the agent cannot see is a lie about
   *  the page, since it reads an elided element as an absent one. */
  truncated?: string;
  children?: OutlineNode[];
}

export interface AppStateSnapshot {
  entry?: string;
  unreadable?: string;
  title?: string;
  url?: string;
  params?: Record<string, string>;
  dom?: OutlineNode;
  /** Set by `offloadDom` when the outline was written to a file instead. */
  dom_path?: string;
  console?: AppLogEntry[];
  consoleTruncated?: string;
}

export function clipText(value: unknown, max: number): string {
  const s = value === null || value === undefined ? "" : String(value);
  return s.length > max ? s.slice(0, max) + "…" : s;
}

/** console.error's arguments are anything at all, including cross-realm objects
 *  (the frame's Error is not this realm's Error, so `instanceof` is useless
 *  here) (T:4845-4856). */
export function fmtLogArg(v: unknown): string {
  if (typeof v === "string") return v;
  // A cross-realm error: duck-typed on `message` for the reason above.
  const maybe = v as { message?: unknown } | null;
  if (maybe && typeof maybe.message === "string") return maybe.message;
  try {
    const json = JSON.stringify(v);
    return json === undefined ? String(v) : json;
  } catch {
    return String(v); // circular, or a throwing toJSON
  }
}

/** The app's params from its URL, for before its runtime has booted (T:4943). */
export function searchParamsOf(search: string): Record<string, string> {
  const out: Record<string, string> = {};
  new URLSearchParams(search || "").forEach((v, k) => {
    out[k] = v;
  });
  return out;
}

/** Drop this chat's own bookkeeping and clip the rest (T:4972). */
export function appParamsOf(all: Record<string, string> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of Object.keys(all ?? {})) {
    if (!CHAT_PARAMS.has(key)) out[key] = clipText(all?.[key], 200);
  }
  return out;
}

/** How the outline names an element. PR3 (ann/**) owns `annPathOf`; until it
 *  lands the outline simply carries no `path` key, which is the honest absence:
 *  a second path builder is exactly what D146 forbids (T:4985). */
export type PathOf = (el: Element, doc: Document) => string | null;

interface Budget {
  left: number;
}

/**
 * A structural outline of the body — tag, id/class, own text — NOT outerHTML,
 * which for a real app is tens of KB of markup that would dominate every turn
 * (T:4997-5049).
 *
 * `budget` is shared across the whole walk so the TOTAL node count is capped,
 * not just each level's, and any elision is reported.
 */
export function outlineNode(
  el: Element,
  depth: number,
  budget: Budget | null,
  doc: Document | null,
  pathOf?: PathOf,
): OutlineNode {
  const bud: Budget = budget ?? { left: APP_STATE_MAX_NODES };
  const out: OutlineNode = { tag: String(el.tagName || "?").toLowerCase() };
  if (el.id) out.id = clipText(el.id, 80);
  const path = doc && pathOf ? pathOf(el, doc) : null;
  if (path) out.path = clipText(path, 400);
  const cls = typeof el.className === "string" ? el.className.trim() : "";
  if (cls) out.class = clipText(cls, 120);
  const kids = el.children || ([] as unknown as HTMLCollection);
  // Own text only (the element's direct text nodes): textContent on a container
  // is the whole subtree, which would repeat the entire page at every level.
  // Script/style bodies are excluded: their "text" is source the agent can read
  // in the file properly, and a clipped 120 chars of it in a structural outline
  // is noise at best and a misleading fragment at worst.
  const own: string[] = [];
  if (!QUIET_TAGS.has(out.tag)) {
    for (const node of Array.from(el.childNodes || [])) {
      if (node.nodeType === 3 && node.nodeValue && node.nodeValue.trim()) {
        own.push(node.nodeValue.trim());
      }
    }
  }
  let text = own.join(" ");
  if (!text && !kids.length && !QUIET_TAGS.has(out.tag) && el.textContent) {
    text = String(el.textContent).trim();
  }
  if (text) out.text = clipText(text, APP_STATE_MAX_NODE_TEXT);
  if (!kids.length) return out;
  if (depth >= APP_STATE_MAX_DEPTH) {
    out.truncated = kids.length + " deeper element(s) not shown";
    return out;
  }
  out.children = [];
  for (const kid of Array.from(kids)) {
    // Our own pin layer is not part of the app: an element describing the
    // reader's annotations, presented to the agent as part of the page it is
    // editing, is a lie about the source. Skipped without spending budget. The
    // attribute is spelled out rather than imported from the annotation layer
    // because this walk has no business depending on that module (T:5023-5031).
    if (kid.hasAttribute && kid.hasAttribute("data-fused-annotate")) continue;
    if (bud.left <= 0) {
      out.truncated = kids.length - out.children.length + " sibling(s) not shown";
      break;
    }
    bud.left--;
    out.children.push(outlineNode(kid, depth + 1, bud, doc, pathOf));
  }
  return out;
}

export interface AppStateWatcherOptions {
  /** What to call the pane's document in the block's preamble ("app"/"preview",
   *  paneUrl's `paneNoun`). Read at block time so a noun resolving after boot is
   *  reflected without rebuilding the watcher. */
  paneNoun?: () => string;
  /** The DOM-path builder (PR3). */
  pathOf?: PathOf;
  /** Where the offloaded outline goes: `agent.py {action:"shots_dir"}` (T:5175). */
  shotsDir?: () => Promise<string>;
  /** Injected for tests; defaults to platform's `/api/fs/upload` wrapper. */
  upload?: (path: string, blob: Blob) => Promise<unknown>;
  now?: () => number;
  /**
   * Bytes of serialized outline above which `offloadDom` moves it to a file.
   * T has NO threshold — it offloads whenever there is an outline at all
   * (T:5177-5218), which is what `0` (the default) reproduces. The knob exists
   * so a host with no shots dir can keep small outlines inline without a failed
   * round trip, and so the threshold is testable.
   */
  domOffloadMinBytes?: number;
}

export interface AppStateWatcher {
  /** Call from the iframe's `load` AND once at boot (T:4924-4940). */
  watchApp(): void;
  /** Add a line to the PARENT-side ring buffer. */
  pushLog(level: string, text: unknown, source?: string, line?: number): void;
  /** `appEntry` — the entry html the pane is rendering (paneUrl's `entry`). */
  setEntry(entry: string): void;
  /** Read-only view of the buffer, for tests and for the receipt viewer. */
  logs(): AppLogEntry[];
  /** The raw snapshot; `null` when we learned NOTHING (T:5060). */
  snapshot(): AppStateSnapshot | null;
  /** The PUSH channel's view: console trimmed to the newest 12 (T:5124-5158). */
  push(): AppStateSnapshot | null;
  /** The PULL channel's view: never `null` (T:5094-5121). */
  pull(): AppStateSnapshot;
  /** `<live-app-state>` … `</live-app-state>`, or `""` for a null state. */
  block(state: AppStateSnapshot | null): string;
  /** The snapshot with its outline moved OUT to a file, or unchanged (T:5177). */
  offloadDom(state: AppStateSnapshot | null): Promise<AppStateSnapshot | null>;
  /** The push block for a send: push → offload → block, in that order. */
  blockForSend(): Promise<string>;
  /** Un-wrap every document this watcher patched. The chat can go while the
   *  framed app lives on, and the app's console has to be its own again. */
  dispose(): void;
}

/** Distinct names within the same millisecond; two sends cannot share a file. */
let appStateSeq = 0;

/** `dir` + "/" + `name`, T's own join (T:9039). */
function shotJoin(dir: string, name: string): string {
  return dir.replace(/[\\/]+$/, "") + "/" + name;
}

/** Marker on the framed DOCUMENT, not its window: a same-origin navigation keeps
 *  the window proxy but replaces the global, so a flag there would survive as
 *  little as the wrapper itself does — and re-wrapping is exactly what a reload
 *  needs (T:4885-4890). */
interface WatchedDocument extends Document {
  __fusedAppWatched?: boolean;
}

/** The framed window as this module reads it. `console` is not on the DOM lib's
 *  `Window` (it is a global, not a member), and the app's `fused` runtime is a
 *  page-side object — so the two are declared here rather than cast at each use. */
interface FramedWindow extends Window {
  console: Record<string, unknown>;
  fused?: { params?: { getAll?: () => Record<string, string> } };
}

export function createAppStateWatcher(
  getFrame: () => HTMLIFrameElement | null,
  opts: AppStateWatcherOptions = {},
): AppStateWatcher {
  const now = opts.now ?? (() => Date.now());
  const upload = opts.upload ?? ((path: string, blob: Blob) => uploadFile(path, blob, "appstate.json"));
  const offloadMin = opts.domOffloadMinBytes ?? 0;

  // PARENT-side, deliberately: the buffer has to outlive the framed app's own
  // reloads, which is exactly what makes "that error was already there before my
  // edit" answerable (T:4823-4828).
  const appLogs: AppLogEntry[] = [];
  let appEntry = "";
  /** One entry per document this watcher has wrapped — see `dispose`. */
  const unpatchers: (() => void)[] = [];
  let appLoads = 0; // loads seen, so a RELOAD can be told apart

  function pushLog(level: string, text: unknown, source?: string, line?: number): void {
    const entry: AppLogEntry = {
      level,
      text: clipText(text, APP_STATE_MAX_TEXT),
      ts: Math.round(now() / 1000),
    };
    if (source) entry.source = clipText(source, 200);
    if (line) entry.line = line;
    appLogs.push(entry);
    while (appLogs.length > APP_STATE_MAX_LOGS) appLogs.shift();
  }

  /**
   * The window the app runs in — the pane frame's OWN, with no descent. #372
   * moved the pane from `/embed/<app>` (the React shell, with the app nested one
   * iframe deeper) to `/render?path=<entry>`, the raw rendered document: the
   * frame's contentDocument IS the app's document now (T:4858-4877).
   *
   * Returns null — never this chat's own window — in the three cases that are
   * genuinely "no app": the frame is gone (the error path removed it, or the
   * target has no pane), `about:blank` (the placeholder before the first real
   * document), or unreachable (cross-origin, or mid-navigation).
   */
  function appWindow(): FramedWindow | null {
    try {
      const frame = getFrame();
      if (!frame || !frame.isConnected) return null;
      const win = frame.contentWindow as FramedWindow | null;
      if (!win || !win.document) return null;
      if (String(win.location.href) === "about:blank") return null;
      return win;
    } catch {
      return null;
    }
  }

  /** Wrap one window's console + error events. Returns true when THIS document
   *  was newly wrapped, which is also how a reload is detected (T:4879-4922). */
  function watchWindow(win: FramedWindow | null): boolean {
    try {
      if (!win) return false;
      const doc = win.document as WatchedDocument | null;
      if (!doc || doc.__fusedAppWatched) return false;
      doc.__fusedAppWatched = true;
      // Recorded so `dispose` can put the app's own console back: the chat can
      // be unmounted while the framed document lives on (a held-frame swap, a
      // pane that outlives one chat), and leaving `console.error` pointing at a
      // dead watcher's ring buffer means the app's own logging goes nowhere.
      const undo: (() => void)[] = [];
      unpatchers.push(() => {
        doc.__fusedAppWatched = false;
        for (const fn of undo) {
          try {
            fn();
          } catch {
            /* the document went away first; nothing to restore */
          }
        }
      });
      // console.log is deliberately NOT wrapped: an app's chatter is not news,
      // and 50 ring-buffer slots spent on it would push out the errors that are.
      for (const level of ["error", "warn"] as const) {
        const console_ = win.console;
        const original = console_?.[level];
        if (typeof original !== "function") continue;
        const call = original as (...args: unknown[]) => unknown;
        undo.push(() => {
          console_[level] = call as typeof console_[typeof level];
        });
        console_[level] = function patched(...args: unknown[]): unknown {
          try {
            pushLog(level, args.map(fmtLogArg).join(" "));
          } catch {
            /* never */
          }
          // Call through: the app's own logging (and the devtools output the
          // user may be reading) must behave exactly as it did uninstrumented.
          return call.apply(win.console, args);
        };
      }
      win.addEventListener("error", (ev: ErrorEvent) => {
        pushLog("error", (ev && (ev.message || ev.error?.message)) || "script error", ev?.filename, ev?.lineno);
      });
      win.addEventListener("unhandledrejection", (ev: PromiseRejectionEvent) => {
        pushLog("error", "unhandled promise rejection: " + fmtLogArg(ev?.reason));
      });
      return true;
    } catch {
      // Cross-origin, navigating, or already torn down. The snapshot simply has
      // no console entries to report; the chat is unaffected.
      return false;
    }
  }

  function watchApp(): void {
    const win = appWindow();
    if (!win) return;
    if (!watchWindow(win)) return;
    appLoads++;
    // Mark the reload IN the buffer rather than clearing it: "these three errors
    // were there before, this one appeared after your edit" is the single most
    // useful thing the buffer can tell the agent, and clearing it destroys that.
    if (appLoads > 1) pushLog("reload", "the pane reloaded (a file it uses changed)");
  }

  function snapshot(): AppStateSnapshot | null {
    const state: AppStateSnapshot = {};
    if (appEntry) state.entry = appEntry;
    let known = false;
    // Resolved fresh on every call: the pane reloads on its own, so the document
    // that was the app a moment ago may be a different one now.
    const win = appWindow();
    if (!win) {
      // No document to describe. `unreadable` is set but does NOT count as
      // knowledge on its own: a frame that simply has not loaded yet is not
      // worth a block. It DOES ride along once there is something else to say.
      state.unreadable = APP_STATE_UNREADABLE;
    } else {
      try {
        const doc = win.document;
        if (doc.title) state.title = clipText(doc.title, 200);
        if (doc.body) state.dom = outlineNode(doc.body, 0, null, doc, opts.pathOf);
        state.url = clipText(String(win.location.pathname + win.location.search), 500);
        // The app's OWN view of its params (its runtime does the merging and
        // defaulting), falling back to its query string before it has booted.
        const runtime = win.fused;
        const params = appParamsOf(
          runtime?.params?.getAll ? runtime.params.getAll() : searchParamsOf(win.location.search),
        );
        if (Object.keys(params).length) state.params = params;
        known = true;
      } catch (err) {
        state.unreadable = "could not read the pane's document: " + (err as Error).message;
      }
    }
    // The WHOLE buffer, because this function serves both channels and the PULL
    // one is a tool call that asked for the app's state — see `push`, which is
    // where the pushed copy is trimmed and why.
    if (appLogs.length) {
      state.console = appLogs.slice();
      known = true;
    }
    return known ? state : null;
  }

  /**
   * The PULL channel's view. Push and pull disagree about what `null` means, on
   * purpose (T:5084-5121):
   *
   *   push — null is "not worth a block". A turn taken while the pane reloads
   *          must look exactly like a turn from before this feature.
   *   pull — null is not an answer at all. The MODEL asked, its tool call is
   *          BLOCKED on the reply, and agent.py turns a non-dict into the
   *          permanent decision "the window could not read the app's state".
   *
   * So the pull side never hands `null` down: the responder retries a null for a
   * bounded number of polls (the mid-reload case, which resolves) and then falls
   * back to THIS — an explicit, non-fatal sentence the model can act on.
   *
   * `entry` rides along when known; `title`/`url`/`params`/`dom` deliberately do
   * NOT. We could not read the app's window, so there is no honest source for
   * them — and the one wrong answer here is describing THIS chat's window as the
   * app's.
   */
  function pull(): AppStateSnapshot {
    const state = snapshot();
    if (state) return state;
    const out: AppStateSnapshot = { unreadable: APP_STATE_UNREADABLE };
    if (appEntry) out.entry = appEntry;
    return out;
  }

  /**
   * The PUSH channel's view: the snapshot with its console trimmed to the newest
   * `APP_STATE_WIRE_LOGS` lines (T:5124-5158).
   *
   * Trimmed to the TAIL, because a console is read newest-first and the line that
   * explains the screen the user is annotating is the last one. The elision is
   * REPORTED for outlineNode's reason, and it names the tool, which is where the
   * rest of the buffer honestly is.
   *
   * A COPY, never the snapshot in place: the object is also what `offloadDom`
   * forwards and what the send path holds, and trimming a shared field in place
   * is how the pull channel would start answering with push's twelve lines.
   */
  function push(): AppStateSnapshot | null {
    const state = snapshot();
    if (!state || !state.console) return state;
    const lines = state.console;
    if (lines.length <= APP_STATE_WIRE_LOGS) return state;
    return {
      ...state,
      console: lines.slice(-APP_STATE_WIRE_LOGS),
      consoleTruncated:
        lines.length -
        APP_STATE_WIRE_LOGS +
        " older console line(s) not shown — call the app_state tool for the whole buffer",
    };
  }

  /** The block prepended to the user's message (T:5160-5173). Delimited and
   *  labelled, because the model has to be able to tell the user's words from a
   *  machine-written description of their screen. No trailing separator —
   *  composeOutgoing owns the joining, so exactly one place decides the
   *  message's shape. */
  function block(state: AppStateSnapshot | null): string {
    if (!state) return "";
    const paneNoun = opts.paneNoun?.() || "preview";
    // `dom_path` where the outline was written to a file, `dom` where it had to
    // stay inline. The preamble has to describe whichever one actually arrived,
    // or the model goes looking for a key that is not there.
    const outline = state.dom_path
      ? "The DOM outline is the JSON file at `dom_path` — read it when you need the structure. "
      : "";
    return (
      "<" +
      APP_STATE_TAG +
      ">\n" +
      "A snapshot of the " +
      paneNoun +
      " the user is looking at in the left pane, taken as " +
      "they sent this message. " +
      outline +
      "`path` on each node is the same anchorPath the " +
      "annotations use, so a pin and an outline node name the same element. It " +
      "goes stale the moment you edit anything — call the app_state tool for a " +
      "fresh read.\n" +
      JSON.stringify(state) +
      "\n</" +
      APP_STATE_TAG +
      ">"
    );
  }

  /**
   * The snapshot with its DOM outline moved OUT to a file, or unchanged if that
   * could not be done (T:5177-5218).
   *
   * The outline is by far the largest thing the page sends, and composeOutgoing
   * puts it in the MESSAGE — so the CLI keeps it in its own session transcript
   * and re-reads it on every later turn. A ten-message conversation carried ten
   * full outlines in context permanently, all but the newest already stale. A
   * path costs the model one Read when it wants the tree and nothing when it
   * does not.
   *
   * It goes in the screenshot directory, the same kind of artifact under the
   * same argument: 0700-enforced, pruned on a TTL and a count, and already the
   * one path `--allowed-tools` lets Read touch without raising a card.
   *
   * Falls back to the inline outline rather than dropping it: no directory, or a
   * failed write, must not leave the agent knowing LESS about the user's screen.
   */
  async function offloadDom(state: AppStateSnapshot | null): Promise<AppStateSnapshot | null> {
    if (!state || !state.dom) return state;
    const json = JSON.stringify(state.dom);
    if (json.length < offloadMin) return state;
    try {
      const dir = await opts.shotsDir?.();
      if (!dir) throw new Error("no screenshot directory");
      const path = shotJoin(dir, "appstate-" + now() + "-" + ++appStateSeq + ".json");
      await upload(path, new Blob([json], { type: "application/json" }));
      const out: AppStateSnapshot = { ...state, dom_path: path };
      delete out.dom;
      return out;
    } catch (err) {
      // OUR console, not the app's ring buffer: a line pushed there would be
      // read by the model as something the app logged (T:5215).
      console.warn("app-state outline kept inline:", (err as Error)?.message ?? err);
      return state;
    }
  }

  return {
    watchApp,
    pushLog,
    /** Put every watched document's console back. Called when the chat that
     *  owns this watcher unmounts (ClaudeChat). */
    dispose() {
      for (const undo of unpatchers.splice(0)) undo();
    },
    setEntry(entry: string) {
      appEntry = entry;
    },
    logs: () => appLogs.slice(),
    snapshot,
    push,
    pull,
    block,
    offloadDom,
    async blockForSend() {
      return block(await offloadDom(push()));
    },
  };
}
