// The sync log drawer: an inline panel under the sync strip listing the
// canvas's sync/log ring newest-first. Fed by the events socket's `log`
// messages; while open it also polls GET /api/canvases/sync/log every 2 s
// whenever no socket log event has arrived lately (socket down, or a server
// that does not send them).
import { useEffect, useRef, useState } from "react";
import { getSyncLog, type WorkbenchEnv } from "./api";
import type { PullControl } from "./SyncStrip";
import { LEG_LABEL, LEG_ORDER, LEG_SHORT, formatClock, type SyncLogEvent } from "./sync-state";

// The Hard pull's second step, inline (no window.confirm: the native shell's
// web views do not promise one). Pressing "Hard pull" swaps the button for
// this question, where the backup goes, a field to type "pull" into, and
// the two answers; anything else leaves the drawer as is. The typed word is
// the friction that lets the door stay open in the worst states — a live
// Claude session, a push that failed, a frame holding a wrong canvas — the
// server copies the whole folder into the trash before it writes anything.
const HARD_PULL_QUESTION =
  "Replace this canvas folder with the server's copy? Unpublished local edits are kept only in the backup.";
const HARD_PULL_WORD = "pull";

/** The Hard pull door, plus what its confirm step needs to say. */
export interface HardPullControl extends PullControl {
  /** sync/state's `trash_root`: where the backup lands (null = unknown/older server). */
  trashRoot: string | null;
  /** A Claude run is live in the clone right now. */
  agentLive: boolean;
  /** The backup folder the last hard pull answered with. */
  lastBackup: string | null;
}

function LegendItem({ color, text }: { color: string; text: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}>
      <span
        aria-hidden
        style={{ width: 6, height: 6, borderRadius: "50%", background: color, display: "inline-block" }}
      />
      {text}
    </span>
  );
}

const LOG_POLL_MS = 2000;
// A socket log event this recent means the socket is feeding the drawer.
const WS_LOG_FRESH_MS = 5000;

export function SyncDrawer({
  env,
  name,
  epoch,
  events,
  wsLogAtRef,
  onEvents,
  hardPull,
  onClose,
}: {
  env: WorkbenchEnv;
  name: string;
  /** sync/state's `watcher_epoch`: a new watcher's ring counts from 0
   *  again, so the cursor restarts with it (the workspace drops the buffer). */
  epoch: string | null;
  /** Newest first. */
  events: readonly SyncLogEvent[];
  /** Date.now() of the last `log` message on the events socket (0 = none). */
  wsLogAtRef: React.MutableRefObject<number>;
  onEvents: (events: SyncLogEvent[]) => void;
  /** The "Hard pull" door: discard the clone and take the server's copy. */
  hardPull: HardPullControl;
  onClose: () => void;
}) {
  const nextRef = useRef(0);
  const onEventsRef = useRef(onEvents);
  onEventsRef.current = onEvents;
  const [confirmHard, setConfirmHard] = useState(false);
  const [typed, setTyped] = useState("");
  // The question drops once the pull is under way (or the canvas changes).
  useEffect(() => {
    if (hardPull.busy) setConfirmHard(false);
  }, [hardPull.busy]);
  useEffect(() => setConfirmHard(false), [name]);
  useEffect(() => {
    if (!confirmHard) setTyped("");
  }, [confirmHard]);
  const typedOk = typed.trim().toLowerCase() === HARD_PULL_WORD;

  useEffect(() => {
    // A new canvas or a new watcher is a new ring: read it from the start.
    // `SyncLog.since` only rewinds an `after` BEYOND the new ring's last seq;
    // one the new ring has already caught up to would silently skip
    // everything before it.
    nextRef.current = 0;
    let cancelled = false;
    let inFlight = false;
    const fetchLog = async (force: boolean) => {
      if (inFlight) return;
      if (!force && Date.now() - wsLogAtRef.current < WS_LOG_FRESH_MS) return;
      inFlight = true;
      try {
        const r = await getSyncLog(env, name, nextRef.current);
        if (cancelled) return;
        // A new watcher's ring restarts below where we were: read it whole.
        if (r.next < nextRef.current) {
          nextRef.current = 0;
          inFlight = false;
          return await fetchLog(true);
        }
        nextRef.current = r.next;
        if (r.events.length) onEventsRef.current(r.events);
      } catch {
        // The drawer just shows what it has; the next tick asks again.
      } finally {
        inFlight = false;
      }
    };
    // Opening always reads the ring once: the socket only carries what
    // happens from now on.
    void fetchLog(true);
    const id = window.setInterval(() => void fetchLog(false), LOG_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [env, name, epoch, wsLogAtRef]);

  const cell: React.CSSProperties = { padding: "3px 8px", verticalAlign: "top" };
  return (
    <div
      style={{
        maxHeight: "40vh",
        overflowY: "auto",
        overflowX: "hidden",
        borderBottom: "1px solid var(--border)",
        background: "var(--bg-alt)",
        fontSize: 12,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "4px 12px",
          position: "sticky",
          top: 0,
          background: "var(--bg-alt)",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <strong>Sync log</strong>
        <span style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
          {confirmHard ? (
            <>
              <span style={{ minWidth: 0, overflowWrap: "anywhere", display: "flex", flexDirection: "column", gap: 2 }}>
                <span>{HARD_PULL_QUESTION}</span>
                <span style={{ color: "var(--fg-muted)", fontSize: 11 }}>
                  Backup first:{" "}
                  <code style={{ fontSize: 11 }}>
                    {hardPull.trashRoot ? `${hardPull.trashRoot}/<timestamp>/` : "the canvas trash (.sync/trash)"}
                  </code>
                </span>
                {hardPull.agentLive && (
                  <span style={{ color: "rgb(200,120,30)", fontSize: 11 }}>
                    A Claude session is editing this folder. The pull runs over it; anything it writes
                    after the backup is taken is not in the backup.
                  </span>
                )}
              </span>
              <input
                className="field-control"
                autoFocus
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && typedOk && !hardPull.disabled) hardPull.onPull();
                  if (e.key === "Escape") setConfirmHard(false);
                }}
                placeholder={`Type ${HARD_PULL_WORD} to confirm`}
                aria-label={`Type ${HARD_PULL_WORD} to confirm the hard pull`}
                style={{ width: 150, height: 24, padding: "0 6px", fontSize: 12, flex: "none" }}
              />
              <button
                className="btn btn-primary"
                style={{ padding: "0 8px", fontSize: 12, flex: "none" }}
                disabled={hardPull.disabled || !typedOk}
                title={typedOk ? undefined : `Type ${HARD_PULL_WORD} to enable`}
                onClick={() => hardPull.onPull()}
              >
                Discard and pull
              </button>
              <button
                className="btn"
                style={{ padding: "0 8px", fontSize: 12, flex: "none" }}
                onClick={() => setConfirmHard(false)}
              >
                Cancel
              </button>
            </>
          ) : (
            <>
              {hardPull.error && (
                <span
                  style={{
                    color: "rgb(200,60,60)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    minWidth: 0,
                    maxWidth: 420,
                  }}
                  title={hardPull.error}
                >
                  {hardPull.error}
                </span>
              )}
              {!hardPull.error && hardPull.lastBackup && (
                <span
                  style={{
                    color: "var(--fg-muted)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    minWidth: 0,
                    maxWidth: 420,
                    fontSize: 11,
                  }}
                  title={hardPull.lastBackup}
                >
                  Backed up to <code style={{ fontSize: 11 }}>{hardPull.lastBackup}</code>
                </span>
              )}
              <button
                className="btn"
                style={{ padding: "0 8px", fontSize: 12, flex: "none" }}
                disabled={hardPull.disabled}
                onClick={() => setConfirmHard(true)}
                title={
                  "Replace this canvas folder with a fresh copy from the server, no merge. The whole folder is backed up first" +
                  (hardPull.trashRoot ? ` under ${hardPull.trashRoot}` : " to the canvas trash") +
                  (hardPull.agentLive ? ". A Claude session is live here; the pull runs over it" : "")
                }
              >
                {hardPull.busy ? "Pulling…" : "Hard pull"}
              </button>
              <button className="btn" style={{ padding: "0 8px", fontSize: 12, flex: "none" }} onClick={onClose}>
                Close
              </button>
            </>
          )}
        </span>
      </div>
      {/* What the strip's dots mean: their order, left to right, and their
          colours. */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: "2px 10px",
          padding: "4px 12px",
          borderBottom: "1px solid var(--border)",
          color: "var(--fg-muted)",
        }}
      >
        <span>{LEG_ORDER.map((l) => LEG_SHORT[l]).join(" · ")}</span>
        <span aria-hidden>|</span>
        <LegendItem color="rgb(46,160,90)" text="green within budget" />
        <LegendItem color="rgb(200,140,30)" text="amber waiting/held, reason shown" />
        <LegendItem color="rgb(220,60,60)" text="red error" />
      </div>
      {events.length === 0 ? (
        <p style={{ padding: "8px 12px", margin: 0, color: "var(--fg-muted)" }}>No sync events yet.</p>
      ) : (
        <table style={{ width: "100%", tableLayout: "fixed", borderCollapse: "collapse" }}>
          <colgroup>
            <col style={{ width: 96 }} />
            <col style={{ width: 104 }} />
            <col style={{ width: 120 }} />
            <col style={{ width: 64 }} />
            <col style={{ width: 28 }} />
            <col />
          </colgroup>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--fg-muted)" }}>
              <th style={cell}>Time</th>
              <th style={cell}>Leg</th>
              <th style={cell}>Action</th>
              <th style={{ ...cell, textAlign: "right" }}>ms</th>
              <th style={cell} aria-label="ok" />
              <th style={cell}>Detail</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.seq} style={{ borderTop: "1px solid var(--border)" }}>
                <td style={{ ...cell, fontVariantNumeric: "tabular-nums", whiteSpace: "nowrap" }}>
                  {formatClock(e.t)}
                </td>
                <td style={{ ...cell, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {LEG_LABEL[e.leg] ?? e.leg}
                </td>
                <td style={{ ...cell, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={e.action}>
                  {e.action}
                </td>
                <td style={{ ...cell, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                  {e.ms ?? ""}
                </td>
                <td style={{ ...cell, color: e.ok ? "rgb(46,160,90)" : "rgb(220,60,60)" }}>
                  {e.ok ? "✓" : "✗"}
                </td>
                <td style={{ ...cell, whiteSpace: "pre-wrap", overflowWrap: "anywhere", color: "var(--fg-muted)" }}>
                  {e.detail}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
