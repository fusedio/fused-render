// The sync log drawer: an inline panel under the sync strip listing the
// canvas's sync/log ring newest-first. Fed by the events socket's `log`
// messages; while open it also polls GET /api/canvases/sync/log every 2 s
// whenever no socket log event has arrived lately (socket down, or a server
// that does not send them).
import { useEffect, useRef } from "react";
import { getSyncLog } from "./api";
import { LEG_LABEL, LEG_ORDER, LEG_SHORT, formatClock, type SyncLogEvent } from "./sync-state";

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
  name,
  events,
  wsLogAtRef,
  onEvents,
  onClose,
}: {
  name: string;
  /** Newest first. */
  events: readonly SyncLogEvent[];
  /** Date.now() of the last `log` message on the events socket (0 = none). */
  wsLogAtRef: React.MutableRefObject<number>;
  onEvents: (events: SyncLogEvent[]) => void;
  onClose: () => void;
}) {
  const nextRef = useRef(0);
  const onEventsRef = useRef(onEvents);
  onEventsRef.current = onEvents;

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    const fetchLog = async (force: boolean) => {
      if (inFlight) return;
      if (!force && Date.now() - wsLogAtRef.current < WS_LOG_FRESH_MS) return;
      inFlight = true;
      try {
        const r = await getSyncLog(name, nextRef.current);
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
  }, [name, wsLogAtRef]);

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
        <button className="btn" style={{ padding: "0 8px", fontSize: 12 }} onClick={onClose}>
          Close
        </button>
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
