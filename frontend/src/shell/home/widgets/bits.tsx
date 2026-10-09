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

/** Height reserved for the more line before one has rendered. */
const MORE_FALLBACK = 20;

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
 * The pure step of the measured fit. `contentH` is the height of the items
 * shown now (without the "+N more" line), `avail` the height the container
 * really has. Returns the next shown count: one line fewer when the content
 * (plus the more line, reserved whenever items are hidden) overflows, one line
 * more when that line still fits with the more line it would leave, else the
 * same. Both branches judge the same inequality, so repeated calls settle.
 */
export function fitCount(o: { n: number; total: number; avail: number; contentH: number; rowH: number; moreH: number; cols: number }): number {
  const { n, total, avail, contentH, rowH, moreH } = o;
  const cols = Math.max(1, o.cols);
  if (!(rowH > 0) || !Number.isFinite(avail) || !Number.isFinite(contentH)) return n;
  if (contentH + (n < total ? moreH : 0) > avail) return n > 1 ? Math.max(1, n - cols) : n;
  if (n >= total) return n;
  const next = Math.min(total, n + cols);
  const added = Math.ceil(next / cols) - Math.ceil(n / cols);
  const h = contentH + added * rowH + (next < total ? moreH : 0);
  return h <= avail ? next : n;
}

const px = (v: string) => (Number.parseFloat(v) || 0);

/**
 * Show as many of `total` items as the container's real height holds. `cap`
 * is only the first-paint guess; the count can go below it (a short tile) or
 * above it (a tall one). Items are `itemSel` inside the container; `listSel`
 * (optional) names the grid whose columns set how many items share a line.
 */
export function useFitCount(total: number, cap: number, itemSel: string, listSel?: string) {
  const ref = useRef<HTMLDivElement>(null);
  const [n, setN] = useState(() => Math.min(total, cap));
  const measure = () => {
    const wrap = ref.current;
    if (!wrap) return;
    const item = wrap.querySelector<HTMLElement>(itemSel);
    if (!item) return;
    const parent = item.parentElement as HTMLElement;
    const more = wrap.querySelector<HTMLElement>(".hw-more");
    const kids = Array.from(wrap.children).filter((c) => (c as HTMLElement).offsetParent !== null && c !== more) as HTMLElement[];
    if (!kids.length) return;
    const cs = getComputedStyle(wrap);
    const gap = px(getComputedStyle(parent).rowGap);
    const rowH = item.offsetHeight + gap;
    const list = listSel ? wrap.querySelector<HTMLElement>(listSel) : null;
    const cols = list ? Math.max(1, getComputedStyle(list).gridTemplateColumns.split(" ").filter(Boolean).length) : 1;
    const top = Math.min(...kids.map((k) => k.offsetTop));
    const bottom = Math.max(...kids.map((k) => k.offsetTop + k.offsetHeight));
    const avail = wrap.clientHeight - px(cs.paddingTop) - px(cs.paddingBottom);
    const moreH = (more ? more.offsetHeight : 0) || MORE_FALLBACK;
    const moreGap = px(cs.rowGap);
    setN((prev) => {
      const next = Math.min(total, Math.max(1, fitCount({ n: Math.min(prev, total), total, avail, contentH: bottom - top, rowH, moreH: moreH + moreGap, cols })));
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
  return { ref, n: Math.min(n, total) };
}

export function ItemList({ items, cap, moreHref, variant }: { items: WidgetItem[]; cap: number; moreHref?: string; variant?: "tall" }) {
  const { ref, n } = useFitCount(items.length, cap, ".hw-li", ".hw-list");
  const shown = items.slice(0, n);
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
