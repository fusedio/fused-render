// THE SLASH-COMMAND MENU (SPEC C-3) — the list that opens above the composer
// while the box holds nothing but a `/token` being typed.
//
// NOT A POPOVER. The pills' menus (`PillSelect`) and the ⋮ (`Kebab`) take the
// keyboard when they open, and this one must not: the reader is still typing
// in the box, and ↑/↓/Enter/Tab/Esc are routed here by the textarea's own
// `onKeyDown` (`menuKey`). So it is a plain listbox, absolutely placed over the
// composer form, and a press on a row is `mousedown`-cancelled so the caret
// never leaves the box.
import { useEffect, useRef } from "react";
import type { MenuCommand } from "../protocol/commands";

/** Rows visible before the list scrolls (the height is CSS's, `.chat-cmd-menu`). */
export const MENU_MAX_ROWS = 8;

/**
 * What the menu is filtering by, or null when it should not be open: the box
 * holds ONLY a first token that starts with `/` (`^/[^\s]*$`) — the caret may
 * be anywhere inside it. A space ends the token, and with it the menu.
 */
export function menuQuery(text: string): string | null {
  const m = /^\/([^\s]*)$/.exec(text);
  return m ? m[1] : null;
}

export type MenuKey =
  | { type: "move"; index: number }
  | { type: "accept" }
  | { type: "close" };

/**
 * THE MENU'S KEYBOARD, as a pure function of the key and where the highlight
 * is. ↑/↓ move and wrap, Enter (not Shift+Enter, which stays a newline) and Tab
 * accept, Esc closes. Anything else is the box's (null).
 */
export function menuKey(key: string, index: number, count: number, shift = false): MenuKey | null {
  if (count <= 0) return null;
  switch (key) {
    case "ArrowDown":
      return { type: "move", index: (index + 1) % count };
    case "ArrowUp":
      return { type: "move", index: (index - 1 + count) % count };
    case "Enter":
      return shift ? null : { type: "accept" };
    case "Tab":
      return shift ? null : { type: "accept" };
    case "Escape":
      return { type: "close" };
    default:
      return null;
  }
}

/** What accepting a row leaves in the box: the token replaced by `/name ` —
 *  with the space, so an argument can follow and the menu closes. */
export function acceptText(name: string): string {
  return "/" + name + " ";
}

export interface CommandMenuProps {
  id: string;
  rows: readonly MenuCommand[];
  active: number;
  onPick(name: string): void;
  onHover?(index: number): void;
}

export function CommandMenu({ id, rows, active, onPick, onHover }: CommandMenuProps) {
  const listRef = useRef<HTMLDivElement | null>(null);
  // The highlighted row stays in view as ↑/↓ walk past the eighth.
  useEffect(() => {
    const el = listRef.current?.querySelector?.(`[data-index="${active}"]`) as
      | (HTMLElement & { scrollIntoView?: (o?: ScrollIntoViewOptions) => void })
      | null
      | undefined;
    el?.scrollIntoView?.({ block: "nearest" });
  }, [active]);
  return (
    <div ref={listRef} className="chat-cmd-menu" role="listbox" id={id} aria-label="Commands">
      {rows.map((row, i) => (
        <div
          key={row.name}
          id={`${id}-${i}`}
          role="option"
          aria-selected={i === active}
          data-index={i}
          className={"chat-cmd-row" + (i === active ? " is-on" : "")}
          // The caret stays in the box: a press here is not the reader leaving it.
          onMouseDown={(ev) => ev.preventDefault()}
          onMouseEnter={onHover ? () => onHover(i) : undefined}
          onClick={() => onPick(row.name)}
        >
          <span className="chat-cmd-name">/{row.name}</span>
          {row.hint ? <span className="chat-cmd-hint">{row.hint}</span> : null}
          <span className="chat-cmd-desc">{row.description}</span>
          {row.local ? <span className="chat-cmd-tag">local</span> : null}
        </div>
      ))}
    </div>
  );
}
