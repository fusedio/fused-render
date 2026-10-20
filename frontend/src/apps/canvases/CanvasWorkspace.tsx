// Canvas workspace: the embedded live workbench, READ ONLY, for the local
// clone — the canvas page's OVERVIEW tab (shell/CanvasPage.tsx, which owns
// the route /canvases/<env>/<name> and keeps this panel mounted behind its
// other tabs, so the frame below boots once per visit). The frame takes the
// whole panel: no chat, no node pane, no sync chrome (owner, 2026-10-20:
// "overview just shows the canvas … no claude, no sync … cannot make any
// changes to the canvas in the overview"). The workbench is told so with
// `fused_embed_readonly=1` on its URL (application#8312): no drag, connect,
// add, rename or delete, no autosave, no snapshot to this host.
//
// What still runs here is the SYNC that keeps the view current and the
// clone published: this process's watcher (sync/start) pushes a Claude
// task's edits upstream, and the disk → embed delta leg below patches the
// frame in place as the clone moves. Its chrome — the sync strip, the log
// drawer, the failed-push banner with "Fix with Claude" — is rendered
// through a PORTAL into the page's own slot under the header (`syncSlot`),
// so it is on every tab and never in the Overview.
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
//   parent → { type: "fused-embed-auth-token", accessToken }
// The token is the CLI's own `fused login` JWT (GET /api/canvases/token) and
// is posted with an EXACT targetOrigin — never "*" — so it cannot leak to a
// frame that navigated elsewhere.
//
// Node selection: the same listener (the workbench is told this host speaks
// v3 with `fused_embed_v=3`, which also hides its own code editor):
//   iframe → { type: "fused-embed-selection", udfName, backendId, kind }
//            on every change and once after ready — read for nothing here
//            since the node pane went (2026-10-20); kept in the protocol.
//   parent → { type: "fused-embed-select", udfName }  highlight the node a
//            link names (`?node=`, the Tasks page's door on a canvas task),
//            on load and on every hop onto this canvas; null deselects.
//   parent → { type: "fused-embed-pushed", push_seq }  after a push, for the
//            workbench's save chip ("Published"); it triggers no reload.
// Keyed by UDF NAME, never a React Flow node id (those are regenerated every
// load).
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
// The other direction (canvas_snapshot.py, "the disk → embed leg"): this page
// never reloads the workbench iframe and never asks it to re-fetch. When the
// clone moves on disk (`disk_rev` on the sync events socket's `state`
// message — or the 2 s sync/state poll while the socket is down) it fetches
// GET /api/canvases/delta — diff(disk, what the server believes the frame
// holds) — and posts it as
//   parent → { type: "fused-embed-delta", rev, base_rev, full, ops }
//   iframe → { type: "fused-embed-delta-applied", rev } | "-rejected"
// relaying the ack to POST /api/canvases/delta/ack. The frame patches its
// nodes in place: camera, selection and results all survive. A frame that
// just hydrated says
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
//     canvas pane (keyboard follows the hover: Space-pan, zoom keys), unless
//     the user is typing somewhere — a wandering mouse must not pull
//     keystrokes out of a text field onto canvas hotkeys.
import { isBackendOff, useAgentBackends } from "@platform/lib/agent-backends";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { NAV_EVENT, navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import {
  CanvasOwnedError,
  ackDelta,
  applyRename,
  applySnapshot,
  canvasPagePath,
  fixWithClaude,
  getDelta,
  getAccessToken,
  getCanvasesStatus,
  getSyncState,
  getWhoami,
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

// A delta the server HELD (the clone mid-write: a row whose file is not there
// yet) or could not cut (409 busy) is asked for again this far apart, for up
// to this long; past that the next disk move or hello asks afresh.
const DELTA_RETRY_MS = 1_000;
const DELTA_RETRY_WINDOW_MS = 30_000;

// A fallback state poll that keeps failing is treated, after this many
// consecutive misses, as a dropped watcher: sync/start is asked again.
const FAILED_POLLS_BEFORE_RELEASE = 3;

// A focus-triggered server check at most this often: a window that is
// clicked in and out of repeatedly must not become a probe per click.
const FOCUS_CHECK_MIN_MS = 3000;

/** This window's focus as the canvas should count it. */
function hostFocused(): boolean {
  return document.visibilityState === "visible" && document.hasFocus();
}

/**
 * Whether the user is typing: the active element, followed down through
 * same-origin frames (the Tasks tab's framed pages nest their fields a level
 * deeper), is a text field or contenteditable. A cross-origin frame (the
 * canvas itself) cannot be seen into and reads as not typing.
 *
 * No `instanceof` here: an element from a child document belongs to that
 * frame's realm, where `HTMLIFrameElement`/`HTMLElement` are different
 * constructors — the walk would stop one level down and read the typing
 * user as idle.
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

/** The node a link names for this page (`?node=`), by UDF name, or null. */
function nodeNameFromUrl(): string | null {
  return new URLSearchParams(window.location.search).get("node") || null;
}

export default function CanvasWorkspace({
  env,
  name,
  onWorkbenchUrl,
  syncSlot,
}: {
  env: WorkbenchEnv;
  name: string;
  /** The hosted workbench editor's URL for this canvas (no embed flags), once
   *  the base URL and handle are known — the canvas page's header draws it as
   *  "Open in Workbench". Null until then. */
  onWorkbenchUrl?: (url: string | null) => void;
  /** Where the sync chrome goes (the strip, the log drawer, the failed-push
   *  banner): the page's slot under its header, so it shows on every tab and
   *  never inside the Overview. Null = not rendered. */
  syncSlot?: HTMLElement | null;
}) {
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [dir, setDir] = useState<string | null>(null);
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
  const [error, setError] = useState<string | null>(null);
  // A "Fix with Claude" click in flight (the run itself shows on the Tasks tab).
  const [fixBusy, setFixBusy] = useState(false);
  const [fixError, setFixError] = useState<string | null>(null);
  // "Server changes · Pull": the hosted workbench's chip, for this tab. The
  // watcher holds its push while the server_to_disk leg is stale; this button
  // is the user's pull. The result reaches the embed as a delta like any other
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
  // Every server state lands through here. A state older than the one held
  // (same watcher, lower disk_rev: a slow poll answer overtaken by the
  // socket) is dropped, so the delta effect never reads it as a new watcher.
  const applyState = useCallback((s: SyncState) => {
    setSync((prev) =>
      prev && prev.watcher_epoch === s.watcher_epoch && s.disk_rev < prev.disk_rev ? prev : s,
    );
  }, []);

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
  const baseOriginRef = useRef<string | null>(null);

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
        // the token is posted only to this exact origin.
        const base = status.envs[env].base_url;
        setBaseUrl(base);
        baseOriginRef.current = new URL(base).origin;
        const who = await getWhoami(env);
        if (cancelled) return;
        if (!who.handle) {
          setError("could not determine your Fused username (fused whoami)");
          return;
        }
        setHandle(who.handle);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [env, name, ensureSync]);

  // The hosted editor's URL, for the page header (see `onWorkbenchUrl`).
  useEffect(() => {
    onWorkbenchUrl?.(
      baseUrl && handle
        ? `${baseUrl}/workbench/${encodeURIComponent(handle)}/${encodeURIComponent(name)}`
        : null,
    );
  }, [baseUrl, handle, name, onWorkbenchUrl]);

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

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== baseOriginRef.current) return;
      const type = event.data?.type;
      if (type === "fused-embed-auth-ready" || type === "fused-embed-auth-refresh") {
        void seedToken();
        if (type === "fused-embed-auth-ready") {
          sendHostFocus();
          setRepostSeq((n) => n + 1); // a fresh frame has no sync state yet
        }
        // Highlight the node the URL names (a link's `?node=`, a reload, or
        // a frame that reloaded).
        if (type === "fused-embed-auth-ready") {
          const wanted = nodeNameFromUrl();
          if (wanted) sendSelect(wanted);
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
      if (type === "fused-embed-hello") {
        // The frame (re)hydrated from the server: bring it to disk with a
        // full state, whatever the server thought it held before.
        relayDelta(true);
        sendHostFocus();
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
      // `fused-embed-selection` arrives too and is read for nothing: there
      // is no node pane to follow it any more (header).
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [seedToken, sendSelect, sendHostFocus, relaySnapshot, relayDelta, enqueueRelay, env, name]);

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

  useAgentBackends();
  const claudeOff = isBackendOff("claude");
  const onFix = async () => {
    setFixBusy(true);
    setFixError(null);
    try {
      await fixWithClaude(env, name);
      // Don't wait for the next sync state to reflect that a fix is now
      // running: the gap would let a double-click through to a second POST
      // (the server answers it 409, a confusing thing to show right after a
      // successful click).
      setFixActive(true);
      // The fix is a Claude session on the CLONE (canvases.py spawns on the
      // dir), and the page has no canvas chat of its own any more: it is a
      // task under the clone, so the Tasks tab is where to watch it.
      navigateUrl(canvasPagePath(env, name, { tab: "tasks", search: window.location.search }));
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
  // `fused_embed_readonly=1`: the viewer (header; application#8312).
  const frameSrc =
    baseUrl && handle && dir && !owned
      ? `${baseUrl}/workbench/${encodeURIComponent(handle)}/${encodeURIComponent(name)}?fused_embed_auth=1&fused_embed_v=3&fused_embed_readonly=1`
      : null;

  // A HOP ONTO THIS CANVAS WHILE IT IS MOUNTED (a Tasks-page door naming a
  // node, Back/Forward between two pushes on the same canvas) changes the
  // URL and nothing else: App.tsx keys this component on (env, name). The
  // router announces every push on NAV_EVENT and the browser Back/Forward on
  // `popstate`; re-read `?node=` there and tell the workbench. Only this
  // canvas's own URL is ours to read — hops AWAY announce here too.
  useEffect(() => {
    const onNav = () => {
      const here = canvasNameFromPath(window.location.pathname);
      if (!here || here.env !== env || here.name !== name) return;
      sendSelect(nodeNameFromUrl());
    };
    window.addEventListener(NAV_EVENT, onNav);
    window.addEventListener("popstate", onNav);
    return () => {
      window.removeEventListener(NAV_EVENT, onNav);
      window.removeEventListener("popstate", onNav);
    };
  }, [env, name, sendSelect]);

  const s2dState = sync?.legs.server_to_disk.state;
  const pushLeg = sync?.legs.disk_to_server;

  // The sync chrome, into the page's slot (header). Always up ("Connecting…"
  // before the first state).
  const chrome = (
    <>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {startError && !error && (
        <ErrorBanner>{`Could not start syncing this canvas (retrying): ${startError}`}</ErrorBanner>
      )}
      <SyncStrip
        name={name}
        env={env}
        server={sync}
        queue={relayQueue}
        failure={relayFailure}
        owned={owned}
        live={live}
        open={drawerOpen}
        // No drawer to open before the first state: arming it now would pop
        // it unprompted when that state lands.
        onToggle={() => {
          if (sync || owned) setDrawerOpen((o) => !o);
        }}
        pull={{
          busy: pullBusy,
          error: pullError,
          disabled: pullBusy || s2dState === "pulling" || pushLeg?.state === "pushing",
          onPull: () => void onPull(),
        }}
      />
      {drawerOpen && (sync || owned) && (
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
          onClose={() => setDrawerOpen(false)}
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
            {!claudeOff && (
              <button
                className="btn btn-primary"
                disabled={fixBusy || fixActive}
                onClick={() => void onFix()}
                title="Start a Claude session on the local clone, primed with these errors"
              >
                {fixActive
                  ? "Claude is on it — see Tasks →"
                  : fixBusy
                    ? "Starting…"
                    : "Fix with Claude"}
              </button>
            )}
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
    <div
      // The wrapper, not the iframe: a frame swallows pointer events, but
      // the wrapper's mouseenter still fires as the pointer crosses in.
      onMouseEnter={focusCanvasOnEnter}
      style={{ flex: 1, minHeight: 0, minWidth: 0, position: "relative" }}
    >
      {syncSlot ? createPortal(chrome, syncSlot) : null}
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
      {frameSrc ? (
        <iframe
          ref={frameRef}
          src={frameSrc}
          title={`Workbench: ${name}`}
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
  );
}
