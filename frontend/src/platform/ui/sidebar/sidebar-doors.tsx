// HOVER CARDS BEHIND THE SIDEBAR'S ENTRIES, as the platform sees them: hooks
// an owner hangs on an entry — a collapsed rail icon (SidebarRailItem.door),
// an expanded row (NavItem `doorKey`, the sections' rows) — and nothing more.
// The shell's "Left sidebar preview" (shell/left-rail) fills them; the platform
// and the apps never learn what the card is.
//
// THE EXPANDED ROWS live in components the shell does not draw (NavItem, the
// explorer's Bookmarks section), so their hooks come through a context the
// shell provides around the sidebar (`SidebarDoorContext`): a lookup by the
// entry's key. No provider, or a lookup that answers nothing, and the row is
// exactly what it always was.
import { createContext, useContext, type FocusEvent, type KeyboardEvent, type MouseEvent, type PointerEvent } from "react";

/** The hooks an entry's hover card hangs on the entry. */
export interface SidebarRailDoor {
  /** The entry's element, and null when it goes. */
  ref?: (el: HTMLElement | null) => void;
  onPointerEnter?: (e: PointerEvent<HTMLElement>) => void;
  onPointerLeave?: (e: PointerEvent<HTMLElement>) => void;
  /** A press anywhere on the entry (its link, a kebab, a drag's start). */
  onPointerDown?: (e: PointerEvent<HTMLElement>) => void;
  /** The entry's context menu is opening. */
  onContextMenu?: (e: MouseEvent<HTMLElement>) => void;
  onFocus?: (e: FocusEvent<HTMLElement>) => void;
  onBlur?: (e: FocusEvent<HTMLElement>) => void;
  onKeyDown?: (e: KeyboardEvent<HTMLElement>) => void;
  /** A click, before the entry's own (`onClick`, else the navigation).
      Answering true, the owner took it (its card came up instead): the
      entry's own click does not run. `keyboard`: the click came from Enter
      (no pointer); `modified`: Shift, Cmd, Ctrl or Alt was down. Only a
      literal `true` takes the click; any other answer is ignored. */
  onActivate?: (how?: { keyboard: boolean; modified: boolean }) => unknown;
  /** Extra attributes on the entry (`data-*`, `aria-*`). */
  attrs?: Record<string, string | boolean | undefined>;
  /** The card speaks for the entry: no tooltip (`.sidebar-rail-tip`, a
      row's `title`, a bookmark's hover tip); the accessible name stays. */
  quietTip?: boolean;
}

/** An entry's key → its hooks, or undefined (no card for it). */
export type SidebarDoorLookup = (key: string) => SidebarRailDoor | undefined;

/** How a click arrived, for `onActivate`. */
export function activateHow(e: { detail: number; shiftKey: boolean; metaKey: boolean; ctrlKey: boolean; altKey: boolean }): { keyboard: boolean; modified: boolean } {
  return { keyboard: e.detail === 0, modified: e.shiftKey || e.metaKey || e.ctrlKey || e.altKey };
}

export const SidebarDoorContext = createContext<SidebarDoorLookup | null>(null);

/** The hooks for the entry `key`, if the sidebar's owner offers any. */
export function useSidebarDoor(key: string | null | undefined): SidebarRailDoor | undefined {
  const lookup = useContext(SidebarDoorContext);
  return key && lookup ? lookup(key) : undefined;
}

/** What a door adds to its element: attributes, ref and the handlers the
 *  element has none of its own for. Empty with no door, so such an entry
 *  renders as it always did. An element with its own `onContextMenu` /
 *  `onKeyDown` calls the door's from it instead. */
export function sidebarDoorProps(door: SidebarRailDoor | undefined) {
  if (!door) return {};
  return {
    ...(door.attrs ?? {}),
    ...(door.ref ? { ref: door.ref } : {}),
    ...(door.onPointerEnter ? { onPointerEnter: door.onPointerEnter } : {}),
    ...(door.onPointerLeave ? { onPointerLeave: door.onPointerLeave } : {}),
    ...(door.onPointerDown ? { onPointerDown: door.onPointerDown } : {}),
    ...(door.onContextMenu ? { onContextMenu: door.onContextMenu } : {}),
    ...(door.onKeyDown ? { onKeyDown: door.onKeyDown } : {}),
  };
}
