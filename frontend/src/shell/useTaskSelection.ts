// One view mount's selection (task-selection.ts is the data). It forgets the
// keys that leave the visible list, empties when `resetKey` changes (the view's
// filters or search), and Esc empties it first, before any other Esc handling
// in the view: the listener is on the window's CAPTURE phase and stops the
// press, but stands aside while a dialog is open (the dialog owns Esc then).
import { useCallback, useEffect, useMemo, useState } from "react";
import { EMPTY_SELECTION, pruned, toggled, withMany, type SelectionSet } from "@shell/task-selection";

export interface TaskSelection {
  selected: SelectionSet;
  size: number;
  has: (key: string) => boolean;
  toggle: (key: string) => void;
  setMany: (keys: readonly string[], on: boolean) => void;
  clear: () => void;
}

export function useTaskSelection(visibleKeys: readonly string[], resetKey = ""): TaskSelection {
  const [selected, setSelected] = useState<SelectionSet>(EMPTY_SELECTION);
  const [seen, setSeen] = useState(resetKey);
  if (seen !== resetKey) {
    setSeen(resetKey);
    setSelected(EMPTY_SELECTION);
  }
  useEffect(() => {
    setSelected((s) => pruned(s, visibleKeys));
  }, [visibleKeys]);
  const size = selected.size;
  useEffect(() => {
    if (size === 0) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (document.querySelector("[aria-modal='true'], dialog[open], .modal-overlay") !== null) return;
      e.preventDefault();
      e.stopPropagation();
      setSelected(EMPTY_SELECTION);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [size]);
  const has = useCallback((k: string) => selected.has(k), [selected]);
  const toggle = useCallback((k: string) => setSelected((s) => toggled(s, k)), []);
  const setMany = useCallback((keys: readonly string[], on: boolean) => setSelected((s) => withMany(s, keys, on)), []);
  const clear = useCallback(() => setSelected(EMPTY_SELECTION), []);
  return useMemo(() => ({ selected, size, has, toggle, setMany, clear }), [selected, size, has, toggle, setMany, clear]);
}
