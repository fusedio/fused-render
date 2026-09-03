// The open/close brain of one status-bar chip (statusbar redesign, replaces
// `autoExpand.ts`'s arrival-driven opening).
//
// RULES, the same for all three chips:
//   * hover the chip (or its panel) for OPEN_MS  -> the panel opens as a preview
//   * leave chip+panel for CLOSE_MS              -> a preview closes again
//   * click                                       -> PIN: stays open until…
//   * click again / Escape / click outside        -> unpin and close
//   * one panel at a time — hovering or pinning another chip closes this one
//     (`exclusiveSection.ts` arbitrates; newest intent wins)
//
// NOTHING OPENS ON ITS OWN. The old bar popped a panel open whenever a job or
// repo update arrived, which is how it ended up sitting over the Claude
// composer's send button uninvited. Arrival is now announced by the chip
// itself (its label, count and progress line) and the pointer decides when
// the detail shows.
//
// Timings are injectable so tests run with 0ms and a microtask, not fake
// clocks.
import { useCallback, useEffect, useRef, useState } from "react";
import { useExclusiveSection, type SectionKey } from "@platform/lib/exclusiveSection";
import { useDismissOnOutside } from "@platform/lib/dismissOnOutside";

export const HOVER_OPEN_MS = 120;
export const HOVER_CLOSE_MS = 200;

export interface StatusChipTimings {
  openMs?: number;
  closeMs?: number;
}

export interface StatusChipState {
  /** Panel visible — hovered or pinned. */
  open: boolean;
  /** Held open by a click; survives the pointer leaving. */
  pinned: boolean;
  hostRef: React.RefObject<HTMLDivElement>;
  /** Spread onto the `.dl-host` wrapper: hover intent for chip AND panel. */
  hostProps: {
    ref: React.RefObject<HTMLDivElement>;
    onPointerEnter: () => void;
    onPointerLeave: () => void;
  };
  /** The chip's click. */
  toggle: () => void;
  /** Unpin and close, at once. */
  close: () => void;
}

export function useStatusChip(
  key: SectionKey,
  initialPinned = false,
  { openMs = HOVER_OPEN_MS, closeMs = HOVER_CLOSE_MS }: StatusChipTimings = {},
): StatusChipState {
  const [pinned, setPinned] = useState(initialPinned);
  const [hovered, setHovered] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const open = pinned || hovered;

  const clear = () => {
    if (timer.current !== undefined) clearTimeout(timer.current);
    timer.current = undefined;
  };
  useEffect(() => clear, []);

  const close = useCallback(() => {
    clear();
    setPinned(false);
    setHovered(false);
  }, []);

  useExclusiveSection(key, open, close);
  useDismissOnOutside(hostRef, open, close);

  const onPointerEnter = useCallback(() => {
    clear();
    if (openMs <= 0) {
      setHovered(true);
      return;
    }
    timer.current = setTimeout(() => setHovered(true), openMs);
  }, [openMs]);

  const onPointerLeave = useCallback(() => {
    clear();
    if (closeMs <= 0) {
      setHovered(false);
      return;
    }
    timer.current = setTimeout(() => setHovered(false), closeMs);
  }, [closeMs]);

  const toggle = useCallback(() => {
    clear();
    if (pinned) {
      // A click on a pinned chip means "put it away" — even with the pointer
      // still on it, so `hovered` drops too and the panel goes at once.
      setPinned(false);
      setHovered(false);
    } else {
      setPinned(true);
    }
  }, [pinned]);

  return {
    open,
    pinned,
    hostRef,
    hostProps: { ref: hostRef, onPointerEnter, onPointerLeave },
    toggle,
    close,
  };
}
