// Small pieces every widget body shares: list rows, icon tiles, the "+N more"
// line, and the error / empty states.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
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

/** Fallback row height when no row has rendered yet. */
const ROW_FALLBACK = 44;

/**
 * Whole rows (times `cols` items per row) that `free` px of spare height buys,
 * given rows `rowH` px tall. Negative when the content overflows by a row or
 * more, so callers can shrink back.
 */
export function fitExtra(free: number, rowH: number, cols = 1): number {
  if (!(rowH > 0) || !Number.isFinite(free)) return 0;
  const lines = Math.floor(free / rowH);
  return lines === 0 ? 0 : lines * Math.max(1, cols);
}

/**
 * How many rows beyond `cap` the list wrapper's real height has room for. A
 * tile often renders taller than its nominal size (a neighbour stretches the
 * grid row), so `cap` is only the minimum. Extra rows only consume spare
 * height, so they never grow the tile.
 */
function useFitRows(total: number, cap: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [extra, setExtra] = useState(0);
  const measure = () => {
    const wrap = ref.current;
    if (!wrap) return;
    const ul = wrap.querySelector<HTMLElement>(".hw-list");
    if (!ul) return;
    const li = ul.querySelector<HTMLElement>(".hw-li");
    const more = wrap.querySelector<HTMLElement>(".hw-more");
    const rowH = (li?.offsetHeight || 0) || ROW_FALLBACK;
    const cols = Math.max(1, getComputedStyle(ul).gridTemplateColumns.split(" ").filter(Boolean).length);
    const free = wrap.clientHeight - ul.offsetHeight - (more ? more.offsetHeight : 0);
    const room = Math.max(0, total - cap);
    setExtra((prev) => {
      const next = Math.min(room, Math.max(0, prev + fitExtra(free, rowH, cols)));
      return next === prev ? prev : next;
    });
  };
  useLayoutEffect(measure);
  useEffect(() => {
    const wrap = ref.current;
    if (!wrap || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(wrap);
    return () => ro.disconnect();
  });
  return { ref, extra };
}

export function ItemList({ items, cap, moreHref, variant }: { items: WidgetItem[]; cap: number; moreHref?: string; variant?: "tall" }) {
  const { ref, extra } = useFitRows(items.length, cap);
  const shown = items.slice(0, cap + extra);
  const more = items.length - shown.length;
  return (
    <div ref={ref} className={variant === "tall" ? "hw-list-wrap is-tall" : "hw-list-wrap"}>
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
