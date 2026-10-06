// Fused Bot's one shell control. The bot flavor mounts no GlobalSidebar
// (App.tsx), and in the native app the menu bar carries Tasks and
// Preferences — but the same shell also runs in a plain browser tab (native
// windows off, or the window failing to construct and falling back to the
// browser), where there is no menu bar at all. This corner button is the door
// that exists in both: a small gear at the status bar's left edge (its
// `leading` slot), opening the three shell pages the Bots page itself has
// no row for. In the bar, not floating: the bottom-left corner is live
// chrome (bot-list actions, the bots context menu) and a fixed control
// there covered it. Shell-only; apps may not import it.
import { useEffect, useRef, useState } from "react";
import { navigateUrl } from "@platform/lib/router";
import { ONBOARDING_PATH } from "@shell/onboarding/state";

const ENTRIES: { href: string; label: string }[] = [
  { href: "/bots", label: "Bots" },
  { href: "/tasks", label: "Tasks" },
  { href: "/preferences", label: "Preferences" },
];

export default function BotShellMenu() {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  // Outside click / Escape closes. Listening only while open keeps the
  // document free of a permanent handler on a page that never uses it.
  // Escape is taken in the capture phase and stopped: while this menu is
  // open it is the topmost dismissable, and the Bots page's panels and
  // dialogs each close on the same key.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  const pick = (href: string) => {
    setOpen(false);
    navigateUrl(href);
  };

  return (
    <div ref={rootRef} className="bot-shell-menu">
      {open && (
        <div className="context-menu placed bot-shell-menu-list" role="menu">
          {ENTRIES.map((e) => (
            <button
              key={e.href}
              type="button"
              role="menuitem"
              className="context-menu-item bot-shell-menu-row"
              onClick={() => pick(e.href)}
            >
              {e.label}
            </button>
          ))}
          <div className="context-menu-sep" role="separator" />
          <button
            type="button"
            role="menuitem"
            className="context-menu-item bot-shell-menu-row"
            onClick={() => pick(ONBOARDING_PATH)}
          >
            Setup wizard
          </button>
        </div>
      )}
      <button
        type="button"
        className="bot-shell-menu-btn"
        aria-label="Menu"
        aria-haspopup="menu"
        aria-expanded={open}
        title="Tasks, Preferences, Setup"
        onClick={() => setOpen((v) => !v)}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
        </svg>
      </button>
    </div>
  );
}
