// AN EARLIER SESSION OF THE ONE CHAT, folded under a marker line (PRODUCT §2
// "Clear", §8): "Monday · Set up the Q4 canvas · 22 messages · Show". Show
// reads that session's transcript (`/api/claude-sessions/history`, the chat's
// own read) and draws it read-only, as a plain list of turns (PLAN D.1: the
// cheaper equivalent of a read-only chat).
import { useState } from "react";
import { fetchHistory } from "@apps/claude/protocol/history";
import type { FoldedSession as Folded } from "@platform/lib/specialists-api";

/** "Today", "Yesterday", a weekday within the week, else a short date. */
export function dayLabel(epochS: number, now: Date = new Date()): string {
  if (!epochS) return "Earlier";
  const d = new Date(epochS * 1000);
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(now) - start(d)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: "long" });
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function foldedLabel(f: Folded, now?: Date): string {
  const parts = [dayLabel(f.started || f.ended, now)];
  if (f.title) parts.push(f.title);
  if (typeof f.messages === "number") parts.push(`${f.messages} ${f.messages === 1 ? "message" : "messages"}`);
  return parts.join(" · ");
}

type Row = { role: string; text: string; key: string };

export function FoldedSession({
  folded,
  home,
  load = fetchHistory,
}: {
  folded: Folded;
  home: string;
  // Seam for tests.
  load?: (file: string, sid: string) => Promise<{ turns?: unknown[]; error?: string }>;
}) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState("");

  const toggle = async () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (rows) return;
    setError("");
    try {
      const r = await load(home, folded.session_id);
      if (r.error) throw new Error(r.error);
      const turns = (r.turns || []) as { role?: string; text?: string; uuid?: string }[];
      setRows(
        turns
          .filter((t) => (t.role === "user" || t.role === "assistant") && (t.text || "").trim())
          .map((t, i) => ({ role: t.role || "", text: (t.text || "").trim(), key: t.uuid || String(i) })),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="spx-folded" data-session={folded.session_id}>
      <div className="spx-marker">
        <span className="spx-marker-line" aria-hidden="true" />
        <span>{foldedLabel(folded)}</span>
        <button type="button" className="spx-linkbtn" aria-expanded={open} onClick={() => void toggle()}>
          {open ? "Hide" : "Show"}
        </button>
        <span className="spx-marker-line" aria-hidden="true" />
      </div>
      {open ? (
        <div className="spx-folded-body">
          {error ? (
            <div className="spx-error" role="alert">
              {error}
            </div>
          ) : rows === null ? (
            <div className="spx-muted">Loading…</div>
          ) : rows.length ? (
            rows.map((r) => (
              <div key={r.key} className={"spx-folded-turn is-" + r.role}>
                {r.text}
              </div>
            ))
          ) : (
            <div className="spx-muted">Nothing was said in this session.</div>
          )}
        </div>
      ) : null}
    </div>
  );
}
