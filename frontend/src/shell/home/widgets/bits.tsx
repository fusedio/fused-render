// Small pieces every widget body shares: list rows, icon tiles, the "+N more"
// line, and the error / empty states.
import type { ReactNode } from "react";
import { softNavigate } from "../strip";

export interface WidgetItem {
  key: string;
  name: string;
  sub?: string;
  /** Where the row goes. `onClick` overrides the default soft navigation. */
  href?: string;
  onClick?: (e: React.MouseEvent) => void;
  icon?: ReactNode;
  pill?: { label: string; tone: "ok" | "warn" | "err" | "idle" };
}

function ItemLink({ item, className, children }: { item: WidgetItem; className: string; children: ReactNode }) {
  if (!item.href) return <span className={className}>{children}</span>;
  return (
    <a
      className={className}
      href={item.href}
      title={item.sub ? `${item.name} — ${item.sub}` : item.name}
      onClick={(e) => (item.onClick ? item.onClick(e) : softNavigate(e, item.href!))}
    >
      {children}
    </a>
  );
}

export function ItemList({ items, cap, moreHref, variant }: { items: WidgetItem[]; cap: number; moreHref?: string; variant?: "tall" }) {
  const shown = items.slice(0, cap);
  const more = items.length - shown.length;
  return (
    <div className={variant === "tall" ? "hw-list-wrap is-tall" : "hw-list-wrap"}>
      <ul className="hw-list">
        {shown.map((it) => (
          <li key={it.key} className="hw-li">
            <ItemLink item={it} className="hw-row">
              {it.icon ? <span className="hw-row-icon" aria-hidden="true">{it.icon}</span> : null}
              <span className="hw-row-text">
                <span className="hw-row-name">{it.name}</span>
                {it.sub ? <span className="hw-row-sub">{it.sub}</span> : null}
              </span>
              {it.pill ? <span className={`hw-pill is-${it.pill.tone}`}>{it.pill.label}</span> : null}
            </ItemLink>
          </li>
        ))}
      </ul>
      <MoreLine count={more} href={moreHref} />
    </div>
  );
}

export function ItemIcons({ items, cap, moreHref }: { items: WidgetItem[]; cap: number; moreHref?: string }) {
  const shown = items.slice(0, cap);
  const more = items.length - shown.length;
  return (
    <div className="hw-list-wrap">
      <div className="hw-icons">
        {shown.map((it) => (
          <ItemLink key={it.key} item={it} className="hw-tile">
            <span className="hw-tile-icon" aria-hidden="true">{it.icon}</span>
            <span className="hw-tile-name">{it.name}</span>
          </ItemLink>
        ))}
      </div>
      <MoreLine count={more} href={moreHref} />
    </div>
  );
}

export function MoreLine({ count, href }: { count: number; href?: string }) {
  if (count <= 0) return null;
  const text = `+${count} more`;
  return href ? (
    <a className="hw-more" href={href} onClick={(e) => softNavigate(e, href)}>
      {text}
    </a>
  ) : (
    <span className="hw-more">{text}</span>
  );
}

export function EmptyLine({ children }: { children: ReactNode }) {
  return <p className="hw-empty">{children}</p>;
}

export function ErrorLine({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="hw-error" role="alert">
      <p className="hw-empty">{message}</p>
      <button type="button" className="hw-btn" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

/** Rows of grey bars for a list-shaped widget while its fetch is in flight. */
export function ListSkeleton({ rows, label }: { rows: number; label: string }) {
  return (
    <div className="hw-skel" role="status" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="skel-bar" style={{ width: `${86 - i * 12}%` }} />
      ))}
    </div>
  );
}

export function BigCount({ value, caption, accent }: { value: string; caption?: string; accent?: string }) {
  return (
    <div className="hw-count">
      <span className="hw-count-num">{value}</span>
      {caption ? <span className="hw-count-cap">{caption}</span> : null}
      {accent ? <span className="hw-count-accent">{accent}</span> : null}
    </div>
  );
}
