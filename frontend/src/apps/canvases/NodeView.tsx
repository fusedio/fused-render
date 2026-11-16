// The canvas NODE VIEW (SPEC §53.3, §53.4): one node of a cloned canvas,
// rendered live by the hosted workbench in a frame, beside the node file's own
// editor. Mounted by the explorer's preview for the native `udf` mode — the
// `_side` column, the content pane, a layout pane — through the shell's
// native-view registry (platform/ui/NativeView), so this file loads with the
// rest of the canvases chunk and never with the main bundle.
//
// Nothing about how a canvas syncs changes: this is one more frame on the same
// sync engine, with an id of its own (`newFrameId`, FP-1) and a record scoped
// to its node (`frame_scope`, FP-9). It only READS: disk → frame, through the
// read leg of the relay (embed-delta-relay.ts). The frame writes nothing back
// (§53.2 NM-4), and the token message offers it no write channel (`caps: []`).
//
// START (NV-5), with no button — the engine starts because the file was
// opened:
//   GET /api/canvases/node            what this file is (the server parses
//                                     the path; this page never does)
//   not signed in                     the sign-in action; nothing cloned
//   not a live clone (a reference     POST /api/canvases/clone ("Cloning
//   copy, an adoption left half way)  <canvas>…"), then the explorer is told
//                                     files may have changed (the pull may
//                                     have replaced this file's text)
//   POST /api/canvases/sync/start     (`owned` → the take-over notice)
//   GET status + token + whoami       the frame (NV-6)
//
// THE FRAME (NV-6): <base>/workbench/<handle>/<canvas>?fused_embed_auth=1&
// fused_embed_v=3&fused_embed_node=<udfName>, `base` the env's `embed_url`
// (the custom env's loopback proxy when that is on) else its `base_url`, as
// the workspace frames it.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { IS_EMBED, navigateUrl } from "@platform/lib/router";
import type { NativeViewProps } from "@platform/ui/NativeView";
import {
  CanvasOwnedError,
  ENV_NAMES,
  ackDelta,
  canvasPagePath,
  cancelLogin,
  cloneCanvas,
  getAccessToken,
  getCanvasNode,
  getCanvasesStatus,
  getDelta,
  getSyncState,
  getWhoami,
  loginOutcome,
  newFrameId,
  pullCanvas,
  startLogin,
  startSync,
  syncEventsUrl,
  takeoverSync,
  type CanvasNodeInfo,
  type NodeKind,
  type WorkbenchEnv,
} from "./api";
import {
  acceptFrameMessage,
  connectSyncFeed,
  createEmbedDeltaRelay,
  keepNewer,
  type EmbedDeltaRelay,
  type NodePresence,
  type NodeState,
} from "./embed-delta-relay";
import { Dot, SyncFramePoster } from "./SyncStrip";
import {
  mergeEmbedLeg,
  noWatcher,
  ownedText,
  stripText,
  type RelayEntry,
  type SyncOwner,
  type SyncState,
} from "./sync-state";

/** A failed or owned sync/start is asked again this often (the workspace's
 *  OWNED_RETRY_MS), so the pane recovers when the other process quits. */
const START_RETRY_MS = 5000;
/** A state saying no watcher runs here re-arms it at most this often. */
const HEAL_MIN_INTERVAL_MS = 5000;
/** The sign-in's status poll (the catalog's LOGIN_POLL_MS). */
const LOGIN_POLL_MS = 1500;

/** A reading frame posts no snapshots: the page's embed_to_disk leg is idle. */
const NO_QUEUE: readonly RelayEntry[] = [];

const KIND_LABEL: Record<NodeKind, string> = {
  udf: "UDF",
  widget: "Widget",
  html: "HTML",
  note: "Note",
};

type NodeInfo = Extract<CanvasNodeInfo, { kind: NodeKind }>;

const isNode = (i: CanvasNodeInfo | null): i is NodeInfo => !!i && i.enabled && i.node !== null;

/** The canvas page on this node (NV-8). From a layout pane (an embed
 *  document) the whole window goes there; the explorer moves itself. */
function openInCanvas(env: WorkbenchEnv, name: string, file: string): void {
  const href = canvasPagePath(env, name, { file });
  if (IS_EMBED && window.top && window.top !== window) {
    try {
      window.top.location.assign(href);
      return;
    } catch {
      // A cross-origin host: move this document instead.
    }
  }
  void navigateUrl(href);
}

export default function NodeView({ path }: NativeViewProps) {
  // --- what this file is ---------------------------------------------------
  const [infoSeq, setInfoSeq] = useState(0);
  const [info, setInfo] = useState<CanvasNodeInfo | null>(null);
  const [infoError, setInfoError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setInfoError(null);
    getCanvasNode(path).then(
      (i) => {
        if (!cancelled) setInfo(i);
      },
      (e) => {
        if (!cancelled) setInfoError((e as Error).message);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [path, infoSeq]);
  const node = isNode(info) ? info : null;
  const env = node?.env ?? null;
  const name = node?.name ?? null;
  const udfName = node?.node ?? null;
  const signedIn = !!node?.signed_in;

  // One frame id for the life of this mount (FP-1).
  const frameId = useRef(newFrameId()).current;

  // --- start: clone if needed, then the watcher ----------------------------
  const [cloning, setCloning] = useState(false);
  const [cloneError, setCloneError] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  const [owned, setOwned] = useState<SyncOwner | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [takingOver, setTakingOver] = useState(false);
  const [takeoverError, setTakeoverError] = useState<string | null>(null);

  const ensureSync = useCallback(async (): Promise<void> => {
    if (!env || !name) return;
    try {
      await startSync(env, name);
      setOwned(null);
      setStartError(null);
      setStarted(true);
    } catch (e) {
      if (e instanceof CanvasOwnedError) {
        setOwned(e.owner);
        return;
      }
      setStartError((e as Error).message);
    }
  }, [env, name]);

  const [cloneSeq, setCloneSeq] = useState(0);
  useEffect(() => {
    if (!node || !node.signed_in) return;
    let cancelled = false;
    void (async () => {
      if (!node.live) {
        // A reference copy is adopted in place (`_adopt_reference_copy`); a
        // folder with no clone at all is pulled. Either way the pull may
        // replace this very file's text.
        setCloning(true);
        setCloneError(null);
        try {
          await cloneCanvas(node.env, node.name);
        } catch (e) {
          if (!cancelled) setCloneError((e as Error).message);
          return;
        } finally {
          if (!cancelled) setCloning(false);
        }
        // The editor beside this pane watches its own file and rebases onto
        // what the pull wrote; the listings re-read through this hook.
        window._fusedFsChanged?.();
      }
      if (!cancelled) await ensureSync();
    })();
    return () => {
      cancelled = true;
    };
    // Re-run on a new answer for the file (a sign-in) or a Retry, not on
    // every render of the same answer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node?.env, node?.name, node?.live, node?.signed_in, cloneSeq, ensureSync]);

  const retryStart = !!owned || startError !== null;
  useEffect(() => {
    if (!retryStart) return;
    const id = window.setInterval(() => void ensureSync(), START_RETRY_MS);
    return () => window.clearInterval(id);
  }, [retryStart, ensureSync]);

  const takeOver = useCallback(async () => {
    if (!env || !name) return;
    setTakingOver(true);
    setTakeoverError(null);
    try {
      await takeoverSync(env, name);
      setOwned(null);
      setStartError(null);
      setStarted(true);
    } catch (e) {
      if (e instanceof CanvasOwnedError) setOwned(e.owner);
      setTakeoverError((e as Error).message);
    } finally {
      setTakingOver(false);
    }
  }, [env, name]);

  // --- the workbench's base and the handle (the workspace's own sources) ---
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  const [handle, setHandle] = useState<string | null>(null);
  const [frameError, setFrameError] = useState<string | null>(null);
  const baseOriginRef = useRef<string | null>(null);
  const workbenchBaseRef = useRef<string | null>(null);
  useEffect(() => {
    if (!env || !signedIn) return;
    let cancelled = false;
    void (async () => {
      try {
        // Status first: it is also what brings the custom env's embed proxy
        // up (canvases.py `_apply_embed_proxy`).
        const status = await getCanvasesStatus(env);
        if (cancelled) return;
        const base = status.envs[env].embed_url ?? status.envs[env].base_url;
        baseOriginRef.current = new URL(base).origin;
        workbenchBaseRef.current = status.envs[env].base_url;
        setBaseUrl(base);
        let h = node?.handle ?? null;
        if (!h) {
          const who = await getWhoami(env);
          h = who.handle;
        }
        if (cancelled) return;
        if (!h) {
          setFrameError("could not determine your Fused username (fused whoami)");
          return;
        }
        setHandle(h);
      } catch (e) {
        if (!cancelled) setFrameError((e as Error).message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [env, signedIn, node?.handle]);

  // --- the frame and its relay ---------------------------------------------
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const [nodeState, setNodeState] = useState<NodeState | null>(null);
  const [presence, setPresence] = useState<NodePresence>("pending");
  const [noNodeMode, setNoNodeMode] = useState(false);
  const [repostSeq, setRepostSeq] = useState(0);
  const relayRef = useRef<EmbedDeltaRelay | null>(null);
  useEffect(() => {
    if (!env || !name || !udfName) return;
    const post = (message: Record<string, unknown>): boolean => {
      const win = frameRef.current?.contentWindow;
      const origin = baseOriginRef.current;
      if (!win || !origin) return false;
      // Exact targetOrigin, never "*": the token rides this channel.
      win.postMessage(message, origin);
      return true;
    };
    const relay = createEmbedDeltaRelay(
      {
        getDelta: (full) => getDelta(env, name, full, frameId, udfName),
        ackDelta: (rev, ok, reason, skipped) => ackDelta(env, name, rev, ok, reason, skipped, frameId, udfName),
        getToken: async () => (await getAccessToken(env)).access_token,
        post,
      },
      {
        node: udfName,
        publicOrigin: () => {
          const wb = workbenchBaseRef.current;
          if (!wb) return undefined;
          try {
            return new URL(wb).origin;
          } catch {
            return undefined;
          }
        },
        onAuthReady: () => setRepostSeq((n) => n + 1),
        onHello: () => setRepostSeq((n) => n + 1),
        // The strip's status; NV-9 reads `onPresence`, which waits out a
        // `present: false` until 2 s after the first delta ack.
        onNodeState: setNodeState,
        onPresence: setPresence,
        onNoNodeMode: () => setNoNodeMode(true),
        onError: (m) => setFrameError(m),
      },
    );
    relayRef.current = relay;
    const onMessage = (event: MessageEvent) => {
      if (!acceptFrameMessage(event, { origin: baseOriginRef.current, window: frameRef.current?.contentWindow ?? null })) {
        return;
      }
      relay.handleMessage(event.data);
    };
    window.addEventListener("message", onMessage);
    return () => {
      window.removeEventListener("message", onMessage);
      relay.dispose();
      if (relayRef.current === relay) relayRef.current = null;
    };
  }, [env, name, udfName, frameId]);

  // --- the sync state (socket, poll while it is down) ----------------------
  const [sync, setSync] = useState<SyncState | null>(null);
  const [live, setLive] = useState(false);
  const syncRef = useRef<SyncState | null>(null);
  const lastHealRef = useRef(0);
  const heal = useCallback(() => {
    if (Date.now() - lastHealRef.current < HEAL_MIN_INTERVAL_MS) return;
    lastHealRef.current = Date.now();
    void ensureSync();
  }, [ensureSync]);
  const syncLive = started && !owned && !!env && !!name;
  useEffect(() => {
    if (!syncLive || !env || !name) return;
    syncRef.current = null;
    return connectSyncFeed({
      url: syncEventsUrl(env, name, frameId),
      poll: () => getSyncState(env, name, frameId),
      onState: (s) => {
        const kept = keepNewer(syncRef.current, s);
        if (kept !== s) return;
        syncRef.current = s;
        setSync(s);
        relayRef.current?.onSyncState(s);
      },
      onLive: setLive,
      onHeal: heal,
    });
  }, [syncLive, env, name, frameId, heal]);
  useEffect(() => {
    if (sync && noWatcher(sync)) heal();
  }, [sync, heal]);

  const frameSrc =
    baseUrl && handle && name && udfName && started && !owned
      ? `${baseUrl}/workbench/${encodeURIComponent(handle)}/${encodeURIComponent(name)}` +
        `?fused_embed_auth=1&fused_embed_v=3&fused_embed_node=${encodeURIComponent(udfName)}`
      : null;
  // A new document owes its node state again.
  useEffect(() => {
    setNodeState(null);
    setPresence("pending");
    setNoNodeMode(false);
  }, [frameSrc]);

  const postSyncState = useCallback((state: unknown) => relayRef.current?.postSyncState(state), []);

  // --- sign-in (the catalog's flow, for this canvas's env) -----------------
  const [loggingIn, setLoggingIn] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const loginStampRef = useRef<number | null>(null);
  const loginStartedRef = useRef<number | null>(null);
  useEffect(() => {
    if (!loggingIn || !env) return;
    let cancelled = false;
    const id = window.setInterval(() => {
      void getCanvasesStatus(env).then((s) => {
        if (cancelled) return;
        if (s.logged_in && s.creds_stamp !== loginStampRef.current) {
          setLoggingIn(false);
          setLoginError(null);
          setInfoSeq((n) => n + 1);
        } else if (!s.login_in_flight) {
          setLoggingIn(false);
          setLoginError(loginOutcome(loginStartedRef.current));
        }
      }, () => undefined);
    }, LOGIN_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [loggingIn, env]);
  const onLogin = async () => {
    if (!env) return;
    setLoginError(null);
    try {
      const s = await getCanvasesStatus(env);
      loginStampRef.current = s.creds_stamp;
      await startLogin(env);
      loginStartedRef.current = Date.now();
      setLoggingIn(true);
    } catch (e) {
      setLoginError((e as Error).message);
    }
  };
  const onCancelLogin = async () => {
    setLoggingIn(false);
    setLoginError(null);
    try {
      await cancelLogin();
    } catch (e) {
      setLoginError((e as Error).message);
    }
  };

  // --- pull (the server moved under a held push) ---------------------------
  const [pullBusy, setPullBusy] = useState(false);
  const [pullError, setPullError] = useState<string | null>(null);
  const onPull = async () => {
    if (!env || !name) return;
    setPullBusy(true);
    setPullError(null);
    try {
      const s = await pullCanvas(env, name);
      if (!s.ok) setPullError(s.pull_error ?? s.error ?? "pull failed");
    } catch (e) {
      setPullError((e as Error).message);
    } finally {
      setPullBusy(false);
    }
  };

  // --- render ----------------------------------------------------------------
  if (infoError) {
    return (
      <div className="node-view">
        <p className="node-view-message" role="alert">
          Could not read this node: {infoError}
        </p>
      </div>
    );
  }
  if (info === null) {
    return (
      <div className="node-view">
        <div className="preview-resolving">
          <span className="mode-icon-spinner" />
          Loading…
        </div>
      </div>
    );
  }
  if (!node || !env || !name || !udfName) {
    // The gate should have kept this mode off: Canvases is off, or the file
    // is not a node of a canvas folder under `user/`.
    return (
      <div className="node-view">
        <p className="node-view-message">
          {info.enabled ? "This file is not a node of a canvas you work on." : "Canvases is turned off in Preferences."}
        </p>
      </div>
    );
  }

  const openCanvas = () => openInCanvas(env, name, node.file);
  const openButton = (
    <button type="button" className="btn" onClick={openCanvas} title={`Open ${name} with this node selected`}>
      Open in canvas
    </button>
  );

  const strip = (
    <NodeStrip
      info={node}
      sync={sync}
      owned={owned}
      live={live}
      nodeState={nodeState}
      pull={{ busy: pullBusy, error: pullError, onPull: () => void onPull() }}
      onOpenCanvas={openCanvas}
    />
  );

  let body: ReactNode;
  if (!node.signed_in) {
    body = (
      <div className="node-view-message">
        <p>
          Sign in to {ENV_NAMES[env].label} to show <strong>{node.title}</strong> live. Nothing is cloned or
          synced until you do.
        </p>
        <div className="node-view-actions">
          <button type="button" className="btn btn-primary" onClick={() => void onLogin()} disabled={loggingIn}>
            {loggingIn
              ? "Waiting for browser sign-in…"
              : env === "custom"
                ? "Sign in to your Fused deployment"
                : "Sign in to Fused"}
          </button>
          {loggingIn && (
            <button type="button" className="btn" onClick={() => void onCancelLogin()}>
              Cancel
            </button>
          )}
        </div>
        {loggingIn && <p className="canvases-sub">Complete the sign-in in the browser window that just opened.</p>}
        {loginError && (
          <p role="alert" className="node-view-error-text">
            {loginError}
          </p>
        )}
      </div>
    );
  } else if (cloning) {
    body = (
      <div className="preview-resolving">
        <span className="mode-icon-spinner" />
        Cloning {name}…
      </div>
    );
  } else if (cloneError) {
    body = (
      <div className="node-view-message">
        <p role="alert" className="node-view-error-text">
          Could not clone {name}: {cloneError}
        </p>
        <div className="node-view-actions">
          <button type="button" className="btn btn-primary" onClick={() => setCloneSeq((n) => n + 1)}>
            Try again
          </button>
          {openButton}
        </div>
      </div>
    );
  } else if (owned) {
    // The workspace's notice for a clone another fused-render syncs, and its
    // way out (CanvasWorkspace's owned card; Force quit stays the canvas
    // page's).
    body = (
      <div className="node-view-message">
        <p>
          Another Fused process (pid {owned.pid}, port {owned.port}) is syncing this canvas. Close it there and
          this pane picks the canvas up on its own, or take it over here.
        </p>
        <div className="node-view-actions">
          <button type="button" className="btn btn-primary" onClick={() => void takeOver()} disabled={takingOver}>
            {takingOver ? "Taking over…" : "Take over here"}
          </button>
          {openButton}
        </div>
        {takeoverError && (
          <p role="alert" className="node-view-error-text">
            {takeoverError}
          </p>
        )}
      </div>
    );
  } else if (startError && !started) {
    body = (
      <div className="node-view-message">
        <p role="alert" className="node-view-error-text">
          Could not start syncing this canvas (retrying): {startError}
        </p>
        <div className="node-view-actions">{openButton}</div>
      </div>
    );
  } else if (frameError) {
    body = (
      <div className="node-view-message">
        <p role="alert" className="node-view-error-text">
          {frameError}
        </p>
        <div className="node-view-actions">{openButton}</div>
      </div>
    );
  } else if (noNodeMode) {
    // NM-7: a workbench without node mode drew the whole canvas.
    body = (
      <div className="node-view-message">
        <p>This environment&rsquo;s workbench cannot show a single node yet.</p>
        <div className="node-view-actions">{openButton}</div>
      </div>
    );
  } else if (!frameSrc) {
    body = (
      <div className="preview-resolving">
        <span className="mode-icon-spinner" />
        Loading workbench…
      </div>
    );
  } else {
    // NV-9: "absent" only once a `present: false` outlived the grace after
    // the first delta ack; before that, a `present: false` is a node the
    // first full delta has not brought yet — the loading state.
    const missing = presence === "absent";
    const waiting = !missing && nodeState !== null && !nodeState.present;
    body = (
      <div className="node-view-stage">
        <iframe
          ref={frameRef}
          src={frameSrc}
          title={`Workbench: ${node.title}`}
          // Cross-origin frame: the workbench's clipboard calls need it.
          allow="clipboard-read; clipboard-write"
          // The custom env's embed proxy admits the load only with this
          // app's origin as its Referer (canvas_embed_proxy.py).
          referrerPolicy="origin"
          // NV-6 / NM-1: the workbench keys its stored node flag by its
          // window name, so this frame's flag is never another frame's.
          name={`fused-node-${frameId}`}
          className="node-view-frame"
          style={missing || waiting ? { visibility: "hidden" } : undefined}
        />
        {waiting && (
          <div className="preview-resolving node-view-overlay">
            <span className="mode-icon-spinner" />
            Loading {udfName}…
          </div>
        )}
        {missing && (
          // NV-9: kept mounted underneath — a delta that brings the node
          // back (a rename undone, the first push of a new node) shows it.
          <div className="node-view-message node-view-overlay">
            <p>
              <strong>{udfName}</strong> is not on {name} in the workbench. It may have been deleted or renamed, or
              it has not reached the server yet.
            </p>
            <div className="node-view-actions">{openButton}</div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="node-view">
      {strip}
      {frameSrc && !noNodeMode && (
        <SyncFramePoster
          name={name}
          server={sync}
          queue={NO_QUEUE}
          failure={null}
          repost={repostSeq}
          post={postSyncState}
        />
      )}
      {body}
    </div>
  );
}

// The header strip (NV-8): the node's title and kind, the canvas, the sync
// chip in the workspace's words, the node's own status, "Open in canvas", and
// the error text under it.
function NodeStrip({
  info,
  sync,
  owned,
  live,
  nodeState,
  pull,
  onOpenCanvas,
}: {
  info: NodeInfo;
  sync: SyncState | null;
  owned: SyncOwner | null;
  live: boolean;
  nodeState: NodeState | null;
  pull: { busy: boolean; error: string | null; onPull: () => void };
  onOpenCanvas: () => void;
}) {
  const [errorOpen, setErrorOpen] = useState(false);
  const error = nodeState?.status === "error" ? nodeState.error : null;
  useEffect(() => {
    if (!error) setErrorOpen(false);
  }, [error]);
  const s2d = sync?.legs.server_to_disk.state;
  const showPull = !owned && (s2d === "stale" || pull.busy);
  return (
    <div className="node-view-head">
      <div className="node-view-strip">
        <span className="node-view-kind" title={`${KIND_LABEL[info.kind]} node`}>
          {KIND_LABEL[info.kind]}
        </span>
        <span className="node-view-title" title={info.title === info.node ? info.node : `${info.title} (${info.node})`}>
          {info.title}
        </span>
        <span className="node-view-canvas" title={`On canvas ${info.name}`}>
          {info.name}
        </span>
        <span className="canvases-env-chip" title={`${info.name} syncs with ${ENV_NAMES[info.env].label}`}>
          {ENV_NAMES[info.env].tag}
        </span>
        <span className="node-view-fill" />
        {nodeState?.status && <NodeStatus status={nodeState.status} />}
        <SyncChip name={info.name} sync={sync} owned={owned} live={live} />
        {showPull && (
          <button
            type="button"
            className="btn btn-primary node-view-small-btn"
            onClick={pull.onPull}
            disabled={pull.busy || s2d === "pulling"}
            title="Bring the server's changes into this canvas; anything it overwrites is kept in the canvas trash"
          >
            {pull.busy || s2d === "pulling" ? "Pulling…" : "Pull"}
          </button>
        )}
        <button type="button" className="btn node-view-small-btn" onClick={onOpenCanvas}>
          Open in canvas
        </button>
      </div>
      {pull.error && (
        <p role="alert" className="node-view-error-line node-view-error-text">
          {pull.error}
        </p>
      )}
      {error && (
        <button
          type="button"
          className={"node-view-error-line" + (errorOpen ? " is-open" : "")}
          aria-expanded={errorOpen}
          onClick={() => setErrorOpen((o) => !o)}
          title={errorOpen ? "Fold the error" : "Show the whole error"}
        >
          {error.line !== null && <span className="node-view-error-lineno">Line {error.line}</span>}
          <span className="node-view-error-text">{errorOpen ? error.text : lastLine(error.text)}</span>
        </button>
      )}
    </div>
  );
}

/** The line an error ends on — the exception, for a Python traceback. */
function lastLine(text: string): string {
  const lines = text.split("\n").filter((l) => l.trim());
  return lines[lines.length - 1] ?? text;
}

// The workbench's status dot words (§53.2 NM-6: "loading", "error",
// "running", "ready", "paused", "stale"), on the strip's three levels.
function NodeStatus({ status }: { status: string }) {
  const level = status === "error" ? "error" : status === "stale" || status === "paused" ? "warn" : "ok";
  return (
    <span className="node-view-status" title={`Node status: ${status}`}>
      <Dot level={level} size={6} />
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
}

/** The workspace's strip, folded to its line and its dot: the same states,
 *  the same words (sync-state.ts `stripText`). Its own clock, so "Published
 *  12 s ago" ticks without re-rendering the frame's parent. */
function SyncChip({
  name,
  sync,
  owned,
  live,
}: {
  name: string;
  sync: SyncState | null;
  owned: SyncOwner | null;
  live: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  const ticking = !!sync?.legs.disk_to_server.last_push_at;
  useEffect(() => {
    if (!ticking) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [ticking]);
  const merged = useMemo(() => (sync ? mergeEmbedLeg(sync, name, NO_QUEUE, null, now) : null), [sync, name, now]);
  const level = owned ? "error" : (merged?.worst.level ?? "ok");
  const text = owned ? ownedText(owned) : merged ? stripText(merged, now) : "Connecting…";
  return (
    <span className="node-view-sync" title={live || !sync ? text : `${text} · polling every 2 s`}>
      <Dot level={level} size={7} />
      <span className="node-view-sync-text">{text}</span>
    </span>
  );
}
