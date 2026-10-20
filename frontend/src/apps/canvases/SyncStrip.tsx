// The canvas workspace's sync strip: one line under the title bar, coloured
// by the worst of the four sync legs (sync-state.ts), with the server_to_disk
// leg's Pull button inside it. Clicking it opens the log drawer.
//
// Also `SyncFramePoster`: the render-nothing component that posts the merged
// state to the workbench frame (`fused-embed-sync-state`). Both own their own
// clock tick so the ageing embed_to_disk leg and "Published 12 s ago" never
// re-render the whole workspace (and its two iframes' parents) every second.
import { ArrowLeftIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  LEG_LABEL,
  legVerdicts,
  mergeEmbedLeg,
  ownedText,
  serverChangesText,
  stripText,
  type EmbedFailure,
  type MergedSyncState,
  type RelayEntry,
  type SyncLevel,
  type SyncOwner,
  type SyncState,
} from "./sync-state";

/** Re-render every `ms` while `ms` is non-null; returns Date.now(). */
function useNow(ms: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (ms === null) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return ms === null ? Date.now() : now;
}

/** How often the merged state needs re-deriving: fast while a snapshot is
 *  in flight (its age crosses the 100 ms budget), once a second while the
 *  strip shows "Published N s ago", otherwise never. */
function tickFor(queue: readonly RelayEntry[], server: SyncState | null, ageText: boolean): number | null {
  if (queue.length > 0) return 100;
  if (ageText && server?.legs.disk_to_server.last_push_at != null) return 1000;
  return null;
}

const LEVEL_STYLE: Record<SyncLevel, { border: string; bg: string; dot: string }> = {
  ok: { border: "rgba(46,160,90,0.30)", bg: "rgba(46,160,90,0.06)", dot: "rgb(46,160,90)" },
  warn: { border: "rgba(200,140,30,0.4)", bg: "rgba(200,140,30,0.10)", dot: "rgb(200,140,30)" },
  error: { border: "rgba(220,60,60,0.35)", bg: "rgba(220,60,60,0.08)", dot: "rgb(220,60,60)" },
};

function Dot({ level, size = 8 }: { level: SyncLevel; size?: number }) {
  return (
    <span
      aria-hidden
      style={{
        display: "inline-block",
        flex: "none",
        width: size,
        height: size,
        borderRadius: "50%",
        background: LEVEL_STYLE[level].dot,
      }}
    />
  );
}

export interface PullControl {
  busy: boolean;
  disabled: boolean;
  error: string | null;
  onPull: () => void;
}

export function SyncStrip({
  name,
  server,
  queue,
  failure,
  owned,
  live,
  pull,
  open,
  onToggle,
  onBack,
  workbenchUrl,
}: {
  name: string;
  server: SyncState | null;
  queue: readonly RelayEntry[];
  failure: EmbedFailure | null;
  /** Another process owns the clone (sync/start 409 `owned`). */
  owned: SyncOwner | null;
  /** The events socket is up (else the page is polling). */
  live: boolean;
  pull: PullControl;
  open: boolean;
  onToggle: () => void;
  /** Back to the canvases listing (/canvases). The strip is the page's only
   *  top chrome, so the way out lives at its left end. */
  onBack: () => void;
  /** The hosted workbench editor for this canvas (no embed flags); null
   *  until the base URL and handle are known. Shown as "Open in workbench"
   *  while every leg is green. */
  workbenchUrl: string | null;
}) {
  const now = useNow(tickFor(queue, server, true));
  const merged: MergedSyncState = mergeEmbedLeg(server, name, queue, failure, now);
  const level: SyncLevel = owned ? "error" : merged.worst.level;
  const text = owned ? ownedText(owned) : server ? stripText(merged, now) : "Connecting…";
  const style = LEVEL_STYLE[level];
  const s2d = merged.legs.server_to_disk;
  const verdicts = legVerdicts(merged);
  const pulling = pull.busy || s2d.state === "pulling";
  const pullFailed = s2d.state === "error" && s2d.changed.length > 0;
  const pushState = merged.legs.disk_to_server.state;
  const inFlight =
    pushState === "pending" || pushState === "pushing" || s2d.state === "pulling" || queue.length > 0;
  const showPull = s2d.state === "stale" || s2d.state === "pulling" || pullFailed || pull.busy;
  // This page's own pull error first (it is the click the user just made),
  // else the server's reason for the failed pull leg.
  const pullErrorText = pulling
    ? null
    : (pull.error ?? (pullFailed ? (s2d.reason ?? "Pull failed") : null));
  return (
    <div
      role="button"
      tabIndex={0}
      aria-expanded={open}
      onClick={onToggle}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onToggle();
        }
      }}
      title={open ? "Hide the sync log" : "Show the sync log"}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        minWidth: 0,
        padding: "4px 12px",
        minHeight: 28,
        borderBottom: `1px solid ${style.border}`,
        background: style.bg,
        fontSize: 12,
        cursor: "pointer",
        userSelect: "none",
      }}
    >
      <button
        type="button"
        style={{
          display: "inline-flex",
          alignItems: "center",
          flex: "none",
          padding: 0,
          margin: 0,
          border: "none",
          background: "none",
          color: "inherit",
          cursor: "pointer",
          opacity: 0.7,
        }}
        aria-label="Back to canvases"
        title="Back to canvases"
        onClick={(e) => {
          e.stopPropagation();
          onBack();
        }}
        onKeyDown={(e) => e.stopPropagation()}
      >
        <ArrowLeftIcon size={14} />
      </button>
      <Dot level={level} />
      <span
        style={{
          flex: "1 1 auto",
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          fontWeight: level === "ok" ? 400 : 600,
        }}
        title={text}
      >
        {text}
      </span>
      {!owned && server && showPull && (
        // The server_to_disk segment: what changed, and the user's Pull —
        // for as long as the server-changed flag is up: stale, mid-pull
        // ("Pulling…", disabled) and after a FAILED pull (the push stays
        // held until a pull lands, so the button must stay).
        <span
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
          style={{ display: "flex", alignItems: "center", gap: 8, flex: "0 1 auto", minWidth: 0, cursor: "default" }}
          title={
            serverChangesText(s2d) +
            ". Nothing here is published until you pull. Pull merges them with your local edits " +
            "where it can; anything it has to overwrite is kept in the canvas trash."
          }
        >
          {pullErrorText && (
            <span
              style={{
                color: "rgb(200,60,60)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                minWidth: 0,
              }}
              title={pullErrorText}
            >
              {pullErrorText}
            </span>
          )}
          <button
            className="btn btn-primary"
            style={{ padding: "1px 10px", fontSize: 12 }}
            disabled={pull.disabled || pulling}
            onClick={() => pull.onPull()}
            title="Bring the server's changes into this canvas; anything it overwrites is kept in the canvas trash"
          >
            {pulling ? "Pulling…" : "Pull"}
          </button>
        </span>
      )}
      {!owned && server && level === "ok" && !inFlight && workbenchUrl && (
        // All green and nothing moving: the canvas on the server is what
        // this folder holds, so the hosted editor is worth a look. A plain
        // link in a new tab. (A pending/pushing push is "ok" to worstLeg but
        // reads "Publishing…", so it is held out here.)
        <a
          href={workbenchUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="btn"
          style={{ padding: "1px 10px", fontSize: 12, flex: "none", textDecoration: "none" }}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
          title="Open this canvas in the Fused workbench, in a new tab"
        >
          Open in workbench
        </a>
      )}
      {!owned && server && (
        // The four legs, local first, as bare dots (each names itself in its
        // tooltip; the drawer's legend line spells out the order).
        <span style={{ display: "flex", alignItems: "center", gap: 6, flex: "none" }}>
          {verdicts.map((v) => (
            <span
              key={v.leg}
              title={`${LEG_LABEL[v.leg]}: ${merged.legs[v.leg].state}${v.reason ? ` — ${v.reason}` : ""}`}
              style={{ display: "inline-flex" }}
            >
              <Dot level={v.level} size={6} />
            </span>
          ))}
        </span>
      )}
      {!live && server && (
        <span style={{ flex: "none", color: "var(--fg-muted)" }} title="Polling every 2 s">
          · polling
        </span>
      )}
    </div>
  );
}

/** Posts `{ type: "fused-embed-sync-state", state }` to the frame whenever
 *  the merged state changes, at most every 100 ms (trailing edge: the last
 *  state always lands), and re-posts an unchanged one at least every 10 s
 *  (heartbeat). `repost` forces a post (the frame just loaded or
 *  re-hydrated and has nothing). Renders nothing. */
export function SyncFramePoster({
  name,
  server,
  queue,
  failure,
  repost,
  post,
}: {
  name: string;
  server: SyncState | null;
  queue: readonly RelayEntry[];
  failure: EmbedFailure | null;
  repost: number;
  post: (state: MergedSyncState) => void;
}) {
  const now = useNow(tickFor(queue, server, false));
  const merged = mergeEmbedLeg(server, name, queue, failure, now);
  const json = JSON.stringify(merged);
  const lastJsonRef = useRef<string | null>(null);
  const lastRepostRef = useRef(repost);
  const lastAtRef = useRef(0);
  const pendingRef = useRef<MergedSyncState | null>(null);
  const timerRef = useRef<number | null>(null);
  const postRef = useRef(post);
  postRef.current = post;

  useEffect(() => {
    const forced = lastRepostRef.current !== repost;
    lastRepostRef.current = repost;
    if (!server) return; // nothing true to say yet
    if (!forced && json === lastJsonRef.current) return;
    lastJsonRef.current = json;
    pendingRef.current = merged;
    const flush = () => {
      timerRef.current = null;
      const m = pendingRef.current;
      pendingRef.current = null;
      if (!m) return;
      lastAtRef.current = Date.now();
      postRef.current(m);
    };
    const wait = 100 - (Date.now() - lastAtRef.current);
    if (wait <= 0 || forced) {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      flush();
    } else if (timerRef.current === null) {
      timerRef.current = window.setTimeout(flush, wait);
    }
    // `merged` is derived from `json`'s inputs; keyed on the string.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [json, repost, server]);

  // HEARTBEAT: the frame drops a host state older than 30 s (so a dead host
  // cannot pin its chip red), so an unchanged state — a steady "held" or
  // "error" — is re-posted every FRAME_HEARTBEAT_MS. The frame skips
  // identical states, so a repeat costs it nothing.
  const latestRef = useRef<MergedSyncState | null>(null);
  latestRef.current = server ? merged : null;
  useEffect(() => {
    const id = window.setInterval(() => {
      const m = latestRef.current;
      if (!m || Date.now() - lastAtRef.current < FRAME_HEARTBEAT_MS) return;
      lastAtRef.current = Date.now();
      postRef.current(m);
    }, FRAME_HEARTBEAT_MS / 2);
    return () => {
      window.clearInterval(id);
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);
  return null;
}

/** Re-post an unchanged state at least this often (the frame's staleness
 *  cutoff is 30 s; this stays well inside it). */
const FRAME_HEARTBEAT_MS = 10_000;
