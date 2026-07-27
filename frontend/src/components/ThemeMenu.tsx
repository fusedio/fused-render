// Appearance menu (SPEC §30, D134): the shell's System / Light / Dark switch.
//
// Lives in the sidebar brand row rather than the footer on purpose — the
// footer is deliberately three equal columns (D125 walked a 4th entry back),
// and the brand row already hosts an icon-button slot (the collapse chevron).
//
// Selecting an option writes the preference and repaints by attribute
// (lib/theme.ts). Nothing here re-renders a view: open panes and view
// documents pick the change up on their own, so a theme switch can never
// disturb a live iframe.
import { useEffect, useRef, useState } from "react";
import { THEME_PREFS, THEME_PREF_LABELS, useThemePref, type ThemePref } from "../lib/theme";

// 14px line icons, matching the collapse chevron beside them.
const ICONS: Record<ThemePref, React.ReactNode> = {
  system: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="2" y="4" width="20" height="13" rx="2" />
      <path d="M8 21h8M12 17v4" />
    </svg>
  ),
  light: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4" />
    </svg>
  ),
  dark: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  ),
};

const CHECK = (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M20 6L9 17l-5-5" />
  </svg>
);

export default function ThemeMenu() {
  const [pref, setPref] = useThemePref();
  // Non-null = open. Fixed coordinates, like PaneModeMenu's dropdown: the
  // sidebar scrolls its own overflow, which would clip an absolutely
  // positioned menu.
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const rootRef = useRef<HTMLSpanElement | null>(null);

  // Close on outside pointerdown, on Escape, or on window blur — a click
  // landing inside any view iframe never reaches this document, but it does
  // blur the shell window (same reasoning as PaneModeMenu).
  useEffect(() => {
    if (!pos) return;
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setPos(null);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPos(null);
    };
    const onBlur = () => setPos(null);
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("blur", onBlur);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", onBlur);
    };
  }, [pos]);

  const toggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (pos) {
      setPos(null);
      return;
    }
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    // Hangs below the trigger, clamped to the viewport's right edge.
    setPos({ top: r.bottom + 4, left: Math.max(4, Math.min(r.left, window.innerWidth - 150)) });
  };

  return (
    <span className="theme-menu" ref={rootRef}>
      <button
        type="button"
        className="icon-btn theme-menu-btn"
        aria-label="Appearance"
        aria-haspopup="menu"
        aria-expanded={pos !== null}
        title={"Appearance: " + THEME_PREF_LABELS[pref]}
        onClick={toggle}
      >
        {ICONS[pref]}
      </button>
      {pos && (
        <div className="theme-menu-dropdown" role="menu" style={{ top: pos.top, left: pos.left }}>
          {THEME_PREFS.map((option) => (
            <button
              key={option}
              type="button"
              role="menuitemradio"
              aria-checked={option === pref}
              className={"theme-menu-item" + (option === pref ? " active" : "")}
              onClick={(e) => {
                e.stopPropagation();
                setPos(null);
                if (option !== pref) setPref(option);
              }}
            >
              <span className="theme-menu-icon">{ICONS[option]}</span>
              <span className="theme-menu-label">{THEME_PREF_LABELS[option]}</span>
              <span className="theme-menu-check">{option === pref ? CHECK : null}</span>
            </button>
          ))}
        </div>
      )}
    </span>
  );
}
