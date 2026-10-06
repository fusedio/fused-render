// Find in this thread (OpenBot chat.js search): the header's .searchbar and its #searchtog button. The Thread does the
// filtering (toggles .filtering on itself and .hit on each .ev wrapper by textContent, after every render) from the
// query ChatPane hands it; this component only owns the bar, ⌘F / Ctrl+F and Esc.
import { useEffect, useRef } from "react";
import { getState } from "../state/store";

export interface ThreadSearchProps {
  /** The bar is shown. */
  open: boolean;
  /** The raw query text. */
  q: string;
  /** "3 matches" / "No matches" / "" (from the Thread). */
  count: string;
  /** No bot selected: the toggle is disabled. */
  disabled: boolean;
  onOpen: () => void;
  /** Close and clear. */
  onClose: () => void;
  onQuery: (q: string) => void;
}

export function ThreadSearch({ open, q, count, disabled, onOpen, onClose, onQuery }: ThreadSearchProps) {
  const inp = useRef<HTMLInputElement>(null);
  // openSearch(): show, focus, select. Focus lands once the bar is displayed (next frame).
  const openSearch = () => { onOpen(); requestAnimationFrame(() => { inp.current?.focus(); inp.current?.select(); }); };
  const openRef = useRef(openSearch); openRef.current = openSearch;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const s = getState();
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f" && s.sel && !s.fast) { e.preventDefault(); openRef.current(); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  return (
    <>
      <div className={`searchbar${open ? " show" : ""}`} id="searchbar">
        <input id="searchq" ref={inp} type="search" placeholder="Find in this thread…" value={q}
          onChange={(e) => onQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); onClose(); } }} />
        <span id="searchn" className="n">{open ? count : ""}</span>
        <button id="searchx" title="Close (Esc)" onClick={onClose}>×</button>
      </div>
      <button id="searchtog" className="ptog" title="Search this thread (⌘F / Ctrl+F)" disabled={disabled}
        onClick={() => (open ? onClose() : openSearch())}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
      </button>
    </>
  );
}
