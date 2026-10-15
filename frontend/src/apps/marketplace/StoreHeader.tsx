// The Marketplace's sticky header (56px): the store's name on the left, the
// Discover / Installed switch and the search field on the right. Translucent
// and blurred over the scrolling content; the hairline under it appears only
// once the page has scrolled (`scrolled`), so at rest the header and the page
// read as one surface.
//
// "/" focuses the search from anywhere on the page (unless something editable
// already has focus, or the shell's chat took the key first — App.tsx handles
// "/" in the capture phase and marks it taken). Esc in the field clears it,
// then blurs it.
import { useEffect, useRef, type MouseEvent, type RefObject } from "react";
import { Search, Store, X } from "lucide-react";
import { navigateUrl } from "@platform/lib/router";
import { MARKETPLACE_HOME } from "@platform/lib/marketplace";

export type StoreTab = "discover" | "installed" | null;

export interface StoreHeaderProps {
  tab: StoreTab;
  q: string;
  onQ: (q: string) => void;
  /** Enter in the field (the non-Discover pages search by navigating). */
  onSubmit: () => void;
  scrolled: boolean;
  updates: number;
  installedCount: number;
  inputRef: RefObject<HTMLInputElement>;
}

function isEditable(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (el as HTMLElement).isContentEditable;
}

export function StoreHeader({ tab, q, onQ, onSubmit, scrolled, updates, installedCount, inputRef }: StoreHeaderProps) {
  const localRef = useRef<HTMLInputElement>(null);
  const ref = inputRef ?? localRef;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      if (isEditable(document.activeElement)) return;
      if (document.querySelector(".modal-overlay, .as-lightbox")) return;
      e.preventDefault();
      ref.current?.focus();
      ref.current?.select();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [ref]);

  const go = (url: string) => (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigateUrl(url);
  };

  return (
    <header className={"mp-header" + (scrolled ? " is-scrolled" : "")}>
      <div className="mp-header-inner">
        <a className="mp-brand" href={MARKETPLACE_HOME} onClick={go(MARKETPLACE_HOME)}>
          <span className="mp-brand-mark" aria-hidden="true">
            <Store size={15} strokeWidth={2.1} />
          </span>
          <span className="mp-brand-text">
            <span className="mp-brand-title">Marketplace</span>
            <span className="mp-brand-sub">Community apps for Fused Render</span>
          </span>
        </a>
        <nav className="mp-switch" aria-label="Marketplace sections">
          <a
            href={MARKETPLACE_HOME}
            onClick={go(MARKETPLACE_HOME)}
            className={"mp-switch-btn" + (tab === "discover" ? " is-active" : "")}
            aria-current={tab === "discover" ? "page" : undefined}
          >
            Discover
          </a>
          <a
            href="/marketplace/installed"
            onClick={go("/marketplace/installed")}
            className={"mp-switch-btn" + (tab === "installed" ? " is-active" : "")}
            aria-current={tab === "installed" ? "page" : undefined}
            title={updates > 0 ? `${updates} update${updates === 1 ? "" : "s"} available` : undefined}
          >
            Installed
            {updates > 0 ? (
              <span className="mp-switch-badge is-update" aria-label={`${updates} updates`}>
                {updates}
              </span>
            ) : installedCount > 0 ? (
              <span className="mp-switch-count">{installedCount}</span>
            ) : null}
          </a>
        </nav>
        <label className="mp-search">
          <Search size={15} strokeWidth={2.1} aria-hidden="true" className="mp-search-glyph" />
          <input
            ref={ref}
            type="search"
            value={q}
            placeholder="Search apps"
            aria-label="Search apps"
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => onQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onSubmit();
              if (e.key === "Escape") {
                e.preventDefault();
                if (q) onQ("");
                else e.currentTarget.blur();
              }
            }}
          />
          {q ? (
            <button
              type="button"
              className="mp-search-clear"
              aria-label="Clear search"
              onClick={() => {
                onQ("");
                ref.current?.focus();
              }}
            >
              <X size={12} strokeWidth={2.6} aria-hidden="true" />
            </button>
          ) : (
            <kbd className="mp-search-kbd" aria-hidden="true">
              /
            </kbd>
          )}
        </label>
      </div>
    </header>
  );
}
