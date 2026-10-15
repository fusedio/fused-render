// "Before you install" — what an app will do on this machine, said plainly
// before the reader presses Get: runs Python, uses local AI models, where it
// came from. A titled list of icon + line + detail rows. Used in the detail
// sidebar and inside the install confirmation sheet.
//
// Generic: rows are data. `tone: "caution"` tints a row's glyph (Python
// running on your machine is the one most readers should notice).
import type { ReactNode } from "react";

export interface TrustItem {
  key?: string;
  icon: ReactNode;
  title: ReactNode;
  detail?: ReactNode;
  tone?: "default" | "caution" | "ok";
}

export interface TrustCardProps {
  title?: ReactNode;
  items: TrustItem[];
  footer?: ReactNode;
  /** Drop the card chrome (border/fill) — for embedding inside a sheet. */
  bare?: boolean;
  className?: string;
}

export function TrustList({ items }: { items: TrustItem[] }) {
  return (
    <ul className="as-trust-list">
      {items.map((it, i) => (
        <li key={it.key ?? i} className={"as-trust-item is-" + (it.tone ?? "default")}>
          <span className="as-trust-glyph" aria-hidden="true">
            {it.icon}
          </span>
          <span className="as-trust-copy">
            <span className="as-trust-title">{it.title}</span>
            {it.detail && <span className="as-trust-detail">{it.detail}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function TrustCard({ title = "Before you install", items, footer, bare, className }: TrustCardProps) {
  return (
    <section className={"as-trust" + (bare ? " is-bare" : "") + (className ? " " + className : "")}>
      {title && <h3 className="as-side-title">{title}</h3>}
      <TrustList items={items} />
      {footer && <div className="as-trust-footer">{footer}</div>}
    </section>
  );
}

export default TrustCard;
