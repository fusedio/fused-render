// A status callout under a detail hero: what pressing the primary action will
// do to what is already on this machine — "Update available", "Your copy has
// changed since it was installed", "A different app is installed under this
// name". The `.fused` install page states every install state with one; the
// Marketplace page can use it for the same kind of sentence.
//
// Generic: tone + title + body, an optional monospace file list (capped, with
// "… and N more"), and an optional actions slot on the right.
//
// Tones: `ok` (nothing is lost — a quiet success-tinted mark), `info`
// (neutral), `warn` (pressing on replaces something the reader may want).
import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Info } from "lucide-react";

export type StatusTone = "ok" | "info" | "warn";

export interface StatusCalloutProps {
  tone?: StatusTone;
  title: ReactNode;
  children?: ReactNode;
  /** A list of paths (changed files …), shown monospace under the body. */
  list?: string[];
  /** How many of `list` to show before "… and N more" (default 8). */
  listMax?: number;
  /** Right-aligned controls (a Cancel link …). */
  actions?: ReactNode;
  /** Replace the tone's default glyph. */
  icon?: ReactNode;
  className?: string;
}

const GLYPH: Record<StatusTone, typeof Info> = {
  ok: CheckCircle2,
  info: Info,
  warn: AlertTriangle,
};

export function StatusCallout({ tone = "info", title, children, list, listMax = 8, actions, icon, className }: StatusCalloutProps) {
  const Glyph = GLYPH[tone];
  const shown = list ? list.slice(0, listMax) : [];
  const more = list ? list.length - shown.length : 0;
  return (
    <section
      className={"as-callout is-" + tone + (className ? " " + className : "")}
      role={tone === "warn" ? "alert" : "status"}
    >
      <span className="as-callout-glyph" aria-hidden="true">
        {icon ?? <Glyph size={16} strokeWidth={2.1} />}
      </span>
      <div className="as-callout-copy">
        <div className="as-callout-title">{title}</div>
        {children && <div className="as-callout-body">{children}</div>}
        {shown.length > 0 && (
          <ul className="as-callout-list">
            {shown.map((p) => (
              <li key={p}>{p}</li>
            ))}
            {more > 0 && <li className="is-more">… and {more} more</li>}
          </ul>
        )}
      </div>
      {actions && <div className="as-callout-actions">{actions}</div>}
    </section>
  );
}

export default StatusCallout;
