// AN EARLIER SESSION OF THE ONE CHAT, folded under a marker line (PRODUCT §2
// "Clear", §8): "Monday · Set up the Q4 canvas · 22 messages · Show". Show
// reads that session's transcript (`/api/claude-sessions/history`, the chat's
// own read) and draws it read-only as chat: the user's turns as bubbles, the
// specialist's replies through the chat's own markdown renderer. The whole
// fold sits in a framed, dimmed box so it never reads as the live thread.
import { lazy, Suspense, useState } from "react";
import { fetchHistory } from "@apps/claude/protocol/history";
import type { FoldedSession as Folded } from "@platform/lib/specialists-api";
import { clip } from "../lib/api-extra";

// The chat's markdown (marked → DOMPurify). Lazy: the chat itself loads it
// lazily too, so a page that never opens a fold never pays for it.
const MarkdownView = lazy(() => import("@apps/claude/ui/MarkdownView").then((m) => ({ default: m.MarkdownView })));

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
  if (f.title) parts.push(clip(f.title, 60));
  if (typeof f.messages === "number") parts.push(`${f.messages} ${f.messages === 1 ? "message" : "messages"}`);
  return parts.join(" · ");
}

type Row = { role: string; text: string; key: string };

/** A marker: one unbroken rule with the label and its control sitting on it. */
export function Marker({ label, now = false, children }: { label: string; now?: boolean; children?: React.ReactNode }) {
  return (
    <div className={"spx-marker" + (now ? " is-now" : "")}>
      <span className="spx-marker-label" title={label}>
        <span className="spx-marker-text">{label}</span>
        {children}
      </span>
    </div>
  );
}

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
  const bodyId = `spx-fold-${folded.session_id}`;

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
    <div className={"spx-folded" + (open ? " is-open" : "")} data-session={folded.session_id}>
      <Marker label={foldedLabel(folded)}>
        <button type="button" className="spx-linkbtn" aria-expanded={open} aria-controls={bodyId} onClick={() => void toggle()}>
          {open ? "Hide" : "Show"}
        </button>
      </Marker>
      {open ? (
        <div className="spx-folded-body" id={bodyId} aria-label="Earlier chat, read-only">
          {error ? (
            <div className="spx-error" role="alert">
              {error}
            </div>
          ) : rows === null ? (
            <div className="spx-muted">Loading…</div>
          ) : rows.length ? (
            rows.map((r) =>
              r.role === "user" ? (
                <div key={r.key} className="spx-folded-turn is-user">
                  {r.text}
                </div>
              ) : (
                <div key={r.key} className="spx-folded-turn is-assistant">
                  <Suspense fallback={<div className="spx-plain">{r.text}</div>}>
                    <MarkdownView text={r.text} className="spx-md" />
                  </Suspense>
                </div>
              ),
            )
          ) : (
            <div className="spx-muted">Nothing was said in this session.</div>
          )}
        </div>
      ) : null}
    </div>
  );
}
