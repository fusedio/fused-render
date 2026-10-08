// The ephemeral status pill (OpenBot core.js showToast): one box under the preview screen (#toast) and one floating
// over the live view stage (#ftoast) render the same store list. Hovering a pill holds it; leaving re-arms the timer.
import { fmtTime } from "../lib/format";
import { armToast, holdToast, useBotsSelector } from "../state/store";

/** #ctoast: the chat column's copy, shown only while the preview column is hidden (bots.css), so a "needs you" pill
 *  still lands somewhere in chat-primary with .rcol / .rfit. */
export function Toast({ id }: { id: "toast" | "ftoast" | "ctoast" }) {
  const toasts = useBotsSelector((s) => s.toasts);
  return (
    <div className="toast" id={id} aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`pill${t.out ? " out" : ""}`} title={t.text} onMouseEnter={holdToast} onMouseLeave={armToast}>
          <span className={`dot ${t.dot}`} /><span>{t.label}</span><time>{fmtTime(t.ts)}</time>
        </div>
      ))}
    </div>
  );
}
