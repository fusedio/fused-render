// The App-Store detail page skeleton, shared by the Marketplace's app page and
// (phase 2) the `.fused` file install page — so every piece here takes SLOTS,
// never a catalog entry:
//
//   <DetailLayout back={…}>
//     <DetailHero icon name subtitle author tags actions menu />
//     <InfoStrip items />                       (InfoStrip.tsx)
//     <PreviewStage src alt />                  (PreviewStage.tsx)
//     <DetailBody main={<SegmentedTabs/>…} aside={<InfoList/><TrustCard/>} />
//     <DetailSection title="More in …">…</DetailSection>
//   </DetailLayout>
//
// Layout and type live in marketplace.css (`.as-detail*`); this file is only
// structure, so the two pages cannot drift apart in spacing.
import { useRef, useState, type MouseEvent, type ReactNode } from "react";
import { ChevronLeft, MoreHorizontal } from "lucide-react";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";

export interface BackLink {
  label: string;
  href: string;
  onClick?: (e: MouseEvent<HTMLAnchorElement>) => void;
}

export function BackLinkView({ back }: { back: BackLink }) {
  return (
    <a className="as-back" href={back.href} onClick={back.onClick}>
      <ChevronLeft size={16} strokeWidth={2.2} aria-hidden="true" />
      <span>{back.label}</span>
    </a>
  );
}

export interface DetailLayoutProps {
  back?: BackLink;
  /** Right side of the top row (opposite the back link) — e.g. a source link. */
  topRight?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function DetailLayout({ back, topRight, children, className }: DetailLayoutProps) {
  return (
    <div className={"as-detail" + (className ? " " + className : "")}>
      {(back || topRight) && (
        <div className="as-detail-top">
          {back ? <BackLinkView back={back} /> : <span />}
          {topRight}
        </div>
      )}
      {children}
    </div>
  );
}

export interface DetailHeroProps {
  /** The icon node — normally `<AppIcon size={112} raised />`. */
  icon: ReactNode;
  name: ReactNode;
  /** One-liner under the name. */
  subtitle?: ReactNode;
  /** Author line ("Isaac", a GitHub link …). */
  author?: ReactNode;
  /** Tag pills: strings are rendered as pills; a node is rendered as is. */
  tags?: string[] | ReactNode;
  /** Primary action(s) at the right — an ActionPill, a secondary line. */
  actions?: ReactNode;
  /** Small line under the actions ("Version 1.2.0 installed"). */
  actionsNote?: ReactNode;
  /** The "⋯" menu's entries; omitted or empty = no menu button. */
  menu?: MenuEntry[];
  /** Accessible name of the menu button. */
  menuLabel?: string;
}

export function DetailHero({ icon, name, subtitle, author, tags, actions, actionsNote, menu, menuLabel = "More actions" }: DetailHeroProps) {
  return (
    <header className="as-hero">
      <div className="as-hero-icon">{icon}</div>
      <div className="as-hero-text">
        <h1 className="as-hero-name">{name}</h1>
        {subtitle && <p className="as-hero-sub">{subtitle}</p>}
        {author && <div className="as-hero-author">{author}</div>}
        {tags &&
          (Array.isArray(tags) ? (
            tags.length > 0 && (
              <ul className="as-tags" aria-label="Tags">
                {tags.map((t) => (
                  <li key={t} className="as-tag">
                    {t}
                  </li>
                ))}
              </ul>
            )
          ) : (
            tags
          ))}
      </div>
      <div className="as-hero-actions">
        <div className="as-hero-action-row">
          {actions}
          {menu && menu.length > 0 && <MoreMenuButton items={menu} label={menuLabel} />}
        </div>
        {actionsNote && <div className="as-hero-note">{actionsNote}</div>}
      </div>
    </header>
  );
}

/** A round "⋯" button that opens a ContextMenu under itself. */
export function MoreMenuButton({ items, label = "More actions", size = "md" }: { items: MenuEntry[]; label?: string; size?: "sm" | "md" }) {
  const ref = useRef<HTMLButtonElement>(null);
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  const open = () => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    // Right-aligned under the button: the menu is ~220px wide and ContextMenu
    // clamps into the viewport, so start it that far left of the right edge.
    setAt({ x: Math.max(8, r.right - 220), y: r.bottom + 6 });
  };
  return (
    <>
      <button
        ref={ref}
        type="button"
        className={"as-more as-more-" + size + (at ? " is-open" : "")}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={!!at}
        title={label}
        onClick={(e) => {
          e.stopPropagation();
          e.preventDefault();
          if (at) setAt(null);
          else open();
        }}
      >
        <MoreHorizontal size={size === "sm" ? 15 : 17} strokeWidth={2.2} aria-hidden="true" />
      </button>
      {at && <ContextMenu x={at.x} y={at.y} items={items} onClose={() => setAt(null)} />}
    </>
  );
}

export interface DetailBodyProps {
  main: ReactNode;
  aside?: ReactNode;
}

/** Two columns (1fr / 280px); the aside drops below on a narrow window. */
export function DetailBody({ main, aside }: DetailBodyProps) {
  return (
    <div className={"as-body" + (aside ? "" : " is-single")}>
      <div className="as-body-main">{main}</div>
      {aside && <aside className="as-body-aside">{aside}</aside>}
    </div>
  );
}

export function DetailSection({ title, action, children, className }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={"as-section" + (className ? " " + className : "")}>
      {(title || action) && (
        <div className="as-section-head">
          {title && <h2 className="as-section-title">{title}</h2>}
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

export interface InfoListRow {
  label: string;
  value: ReactNode;
  key?: string;
}

/** The sidebar "Information" list: label left, value right, hairlines. */
export function InfoList({ title = "Information", rows }: { title?: ReactNode; rows: InfoListRow[] }) {
  return (
    <section className="as-info">
      {title && <h3 className="as-side-title">{title}</h3>}
      <dl className="as-info-list">
        {rows.map((r) => (
          <div className="as-info-row" key={r.key ?? r.label}>
            <dt>{r.label}</dt>
            <dd>{r.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export interface SegmentedTab {
  id: string;
  label: ReactNode;
  count?: number;
}

/** Underline tabs (Overview · Files) — a tablist with arrow-key movement. */
export function SegmentedTabs({ tabs, value, onChange, label = "Sections" }: { tabs: SegmentedTab[]; value: string; onChange: (id: string) => void; label?: string }) {
  return (
    <div
      className="as-tabs"
      role="tablist"
      aria-label={label}
      onKeyDown={(e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        const i = tabs.findIndex((t) => t.id === value);
        const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        onChange(next.id);
        (e.currentTarget.querySelector(`[data-tab="${next.id}"]`) as HTMLElement | null)?.focus();
      }}
    >
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          data-tab={t.id}
          aria-selected={t.id === value}
          tabIndex={t.id === value ? 0 : -1}
          className={"as-tab" + (t.id === value ? " is-active" : "")}
          onClick={() => onChange(t.id)}
        >
          {t.label}
          {t.count !== undefined && <span className="as-tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}
