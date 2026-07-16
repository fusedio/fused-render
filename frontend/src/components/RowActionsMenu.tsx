// A single "⋯" overflow trigger that opens the app's Finder-style ContextMenu
// (components/ContextMenu) anchored just under it. It lets a table row expose
// ONE quiet control instead of a pair of always-visible action buttons — the
// row's actions (open/copy, and the destructive revoke/forget) live behind an
// intentional click. Used by the Environments and Deployments tables on the
// Fused account page to cut the number of buttons on screen at once.
//
// Positioning and dismissal (viewport clamp, outside-pointerdown, Escape,
// scroll, resize, blur) are all inherited from ContextMenu — this only decides
// the open point (the button's bottom-left; ContextMenu clamps it back in when
// the row sits near a viewport edge). A row with no actionable entries renders
// a muted "—" so the actions column still lines up.
import { useRef, useState } from "react";
import ContextMenu, { type MenuEntry } from "./ContextMenu";

export default function RowActionsMenu({
  items,
  disabled = false,
  label = "Row actions",
}: {
  items: MenuEntry[];
  disabled?: boolean;
  label?: string;
}) {
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);

  const hasActions = items.some((it) => it !== "separator" && !it.disabled);
  if (!hasActions) return <span className="deploy-muted">—</span>;

  const open = () => {
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    setMenu({ x: r.left, y: r.bottom + 4 });
  };

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className="row-actions-btn"
        aria-label={label}
        aria-haspopup="menu"
        disabled={disabled}
        // Re-clicking while open lands after ContextMenu's outside-pointerdown
        // has already closed it, so it just reopens at the same point — a
        // no-op-feel that never leaves a stale menu behind.
        onClick={open}
      >
        ⋯
      </button>
      {menu && <ContextMenu x={menu.x} y={menu.y} items={items} onClose={() => setMenu(null)} />}
    </>
  );
}
