// A store tile: a 16:10 screenshot on top, then icon · name · two-line
// description, and a foot row with the meta line left and the action pill
// right (the pill on its own row leaves the name the card's full width).
//
// The whole card is clickable through a STRETCHED LINK: the name is the one
// anchor (its `::after` covers the card, so Cmd-click / middle-click anywhere
// open the detail in a new tab, like the /apps cards — appEntry.ts), and the
// pill is a sibling stacked above that overlay — never a button nested in an
// anchor, which is invalid and reads to a screen reader as one long link.
//
// `compact` drops the screenshot — the detail page's "More in …" shelf and any
// dense list. Generic: every piece is a prop or a slot.
import { useState, type MouseEvent, type ReactNode } from "react";
import { isBrowserHandledClick } from "@platform/lib/appEntry";

export interface StoreCardProps {
  href: string;
  /** In-app navigation for a plain left click (the href covers the rest). */
  onOpen: () => void;
  icon: ReactNode;
  name: ReactNode;
  description?: ReactNode;
  meta?: ReactNode;
  action?: ReactNode;
  /** Screenshot URL; null shows the soft placeholder wash. */
  thumb?: string | null;
  /** Corner chip over the screenshot ("Installed", "Update"). */
  badge?: ReactNode;
  variant?: "default" | "compact";
  /** Accessible name for the card's link (default: the visible name). */
  ariaLabel?: string;
}

export function StoreCard({ href, onOpen, icon, name, description, meta, action, thumb, badge, variant = "default", ariaLabel }: StoreCardProps) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const click = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.defaultPrevented || isBrowserHandledClick(e)) return;
    e.preventDefault();
    onOpen();
  };
  const link = (
    <a className="as-card-link" href={href} onClick={click} aria-label={ariaLabel}>
      {name}
    </a>
  );
  return (
    <div className={"as-card is-" + variant}>
      {variant === "default" && (
        <span className="as-card-thumb" aria-hidden="true">
          {thumb && !failed ? (
            <img
              src={thumb}
              alt=""
              loading="lazy"
              decoding="async"
              draggable={false}
              className={loaded ? "is-loaded" : ""}
              onLoad={() => setLoaded(true)}
              onError={() => setFailed(true)}
            />
          ) : (
            <span className="as-card-thumb-empty">{icon}</span>
          )}
          {thumb && !failed && !loaded && <span className="as-shimmer" />}
          {badge && <span className="as-card-badge">{badge}</span>}
        </span>
      )}
      {variant === "compact" ? (
        <span className="as-card-body">
          <span className="as-card-icon">{icon}</span>
          <span className="as-card-text">
            <span className="as-card-name">{link}</span>
            {description && <span className="as-card-desc">{description}</span>}
          </span>
          {action && <span className="as-card-action">{action}</span>}
        </span>
      ) : (
        <>
          <span className="as-card-body">
            <span className="as-card-icon">{icon}</span>
            <span className="as-card-text">
              <span className="as-card-name">{link}</span>
              {description && <span className="as-card-desc">{description}</span>}
            </span>
          </span>
          {(meta || action) && (
            <span className="as-card-foot">
              <span className="as-card-meta">{meta}</span>
              {action && <span className="as-card-action">{action}</span>}
            </span>
          )}
        </>
      )}
    </div>
  );
}

export default StoreCard;
