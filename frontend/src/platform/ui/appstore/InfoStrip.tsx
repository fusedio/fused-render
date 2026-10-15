// The App Store "Information" ribbon under a detail hero: a row of cells
// divided by hairlines, each an 11px caps label over a 15–17px value and an
// optional muted caption (Category · Version · Developer · Size · …).
//
// Generic: the caller hands over the cells. Wraps to two rows on a narrow
// window without the dividers doubling up (marketplace.css `.as-strip`).
import type { ReactNode } from "react";

export interface InfoStripItem {
  /** Stable React key (default: the label). */
  key?: string;
  label: string;
  value: ReactNode;
  caption?: ReactNode;
  /** A small glyph above/beside the value (optional). */
  icon?: ReactNode;
  /** Tooltip for the whole cell. */
  title?: string;
}

export function InfoStrip({ items, className }: { items: InfoStripItem[]; className?: string }) {
  return (
    <dl className={"as-strip" + (className ? " " + className : "")}>
      {items.map((it) => (
        <div className="as-strip-cell" key={it.key ?? it.label} title={it.title}>
          <dt className="as-strip-label">{it.label}</dt>
          <dd className="as-strip-value">
            {it.icon && <span className="as-strip-icon">{it.icon}</span>}
            <span className="as-strip-text">{it.value}</span>
          </dd>
          {it.caption !== undefined && <dd className="as-strip-caption">{it.caption}</dd>}
        </div>
      ))}
    </dl>
  );
}

export default InfoStrip;
