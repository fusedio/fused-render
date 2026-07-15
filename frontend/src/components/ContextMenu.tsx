// Generic Finder-style right-click context menu. Rendered at the cursor coords
// via position:fixed and clamped into the viewport once its real size is known
// (a menu that spills off-screen is worse than none). Dismissed on any outside
// pointerdown, Escape, scroll, resize or window blur — a menu pinned to a stale
// point after the page moves under it is a bug, so it just closes.
//
// Items are a flat list; a bare "separator" string draws a divider. An item can
// be disabled, `dimmed` (a cut entry, still actionable but visually faded), or
// carry a `submenu` — a lazy loader called when the row is hovered (Open With
// fetches the entry's template modes only on open). One level of submenu only.
//
// Styling lives in shell.css (.context-menu*), matching the pane-mode-dropdown.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

export interface MenuItem {
  label: string;
  // Optional 16px icon (see components/MenuIcons). The icon column is a fixed
  // width whether or not an item carries one, so labels line up Finder-style
  // even next to iconless rows.
  icon?: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  // A cut entry is shown dimmed until pasted (still clickable).
  dimmed?: boolean;
  // Destructive action (Delete): tinted with --error.
  danger?: boolean;
  // Lazy submenu: invoked when the row is hovered. While the promise is
  // pending the submenu shows a "Loading…" placeholder; the resolved items are
  // one level deep (no nested submenus).
  submenu?: () => Promise<MenuItem[]>;
}

// A separator is a bare sentinel so item arrays stay trivial to build inline.
export type MenuEntry = MenuItem | "separator";

interface ContextMenuProps {
  x: number;
  y: number;
  items: MenuEntry[];
  onClose: () => void;
}

// One flat row (used for both the top menu and a submenu). Submenu items never
// carry their own submenu, so `hasSubmenu`/`open` are top-level only.
// `showIcon` reserves the fixed icon column; when NO sibling in the group has
// an icon (e.g. a submenu of pure-text placeholders) the column is dropped so
// labels sit flush instead of behind an empty 20px gutter.
function Row({
  item,
  open,
  showIcon,
  onEnter,
  onActivate,
}: {
  item: MenuItem;
  open: boolean;
  showIcon: boolean;
  onEnter: () => void;
  onActivate: () => void;
}) {
  return (
    <div
      className={
        "context-menu-item" +
        (item.disabled ? " disabled" : "") +
        (item.dimmed ? " dimmed" : "") +
        (item.danger ? " danger" : "") +
        (item.submenu ? " has-submenu" : "") +
        (open ? " open" : "")
      }
      onMouseEnter={onEnter}
      onClick={(e) => {
        e.stopPropagation();
        onActivate();
      }}
    >
      {showIcon && (
        <span className="context-menu-icon" aria-hidden="true">
          {item.icon}
        </span>
      )}
      <span className="context-menu-label">{item.label}</span>
      {item.submenu && <span className="context-menu-arrow">›</span>}
    </div>
  );
}

// Whether a group of entries reserves the icon column: true if any real item
// carries an icon.
function groupHasIcon(entries: MenuEntry[]): boolean {
  return entries.some((e) => e !== "separator" && e.icon != null);
}

export default function ContextMenu({ x, y, items, onClose }: ContextMenuProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ left: x, top: y });
  // Index of the top-level item whose submenu is open (null = none), plus its
  // resolved items (null while the loader is still in flight).
  const [openSub, setOpenSub] = useState<number | null>(null);
  const [subItems, setSubItems] = useState<MenuItem[] | null>(null);
  // Guards against a stale loader resolving after the user moved to another row.
  const loadToken = useRef(0);

  // Clamp into the viewport after first paint, when the real size is known.
  useLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    let left = x;
    let top = y;
    if (left + r.width > window.innerWidth) left = Math.max(4, window.innerWidth - r.width - 4);
    if (top + r.height > window.innerHeight) top = Math.max(4, window.innerHeight - r.height - 4);
    setPos({ left, top });
  }, [x, y, items]);

  // Dismiss on outside pointerdown / Escape / scroll / resize / blur. Scroll is
  // captured (the .listing-scroll scroller doesn't bubble scroll to window).
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", onClose, true);
    window.addEventListener("resize", onClose);
    window.addEventListener("blur", onClose);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("scroll", onClose, true);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("blur", onClose);
    };
  }, [onClose]);

  const enterItem = (idx: number, item: MenuItem) => {
    if (!item.submenu) {
      setOpenSub(null);
      setSubItems(null);
      return;
    }
    if (openSub === idx) return;
    const token = ++loadToken.current;
    setOpenSub(idx);
    setSubItems(null);
    item.submenu().then(
      (r) => {
        if (loadToken.current === token) setSubItems(r);
      },
      () => {
        if (loadToken.current === token) setSubItems([{ label: "Failed to load", disabled: true }]);
      }
    );
  };

  const activate = (item: MenuItem) => {
    if (item.disabled || item.submenu) return; // submenus open on hover, not click
    onClose();
    item.onClick?.();
  };

  // Open the submenu to the left when the menu sits in the right portion of the
  // viewport, so a right-edge right-click doesn't push it off-screen.
  const subLeft = pos.left > window.innerWidth * 0.6;

  // Reserve the icon column only when some item in the group actually has one.
  const topHasIcon = groupHasIcon(items);
  const subHasIcon = subItems !== null && groupHasIcon(subItems);

  return (
    <div ref={rootRef} className="context-menu" style={{ left: pos.left, top: pos.top }}>
      {items.map((it, i) =>
        it === "separator" ? (
          <div key={i} className="context-menu-sep" />
        ) : (
          <div key={i} className="context-menu-row">
            <Row
              item={it}
              open={openSub === i}
              showIcon={topHasIcon}
              onEnter={() => enterItem(i, it)}
              onActivate={() => activate(it)}
            />
            {it.submenu && openSub === i && (
              <div className={"context-menu context-submenu" + (subLeft ? " left" : "")}>
                {subItems === null ? (
                  <div className="context-menu-item disabled">Loading…</div>
                ) : (
                  subItems.map((s, j) => (
                    <Row
                      key={j}
                      item={s}
                      open={false}
                      showIcon={subHasIcon}
                      onEnter={() => {}}
                      onActivate={() => activate(s)}
                    />
                  ))
                )}
              </div>
            )}
          </div>
        )
      )}
    </div>
  );
}
