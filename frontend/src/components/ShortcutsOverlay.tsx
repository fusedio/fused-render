// The Mod+K keyboard cheat sheet. Pure presentation over the registry in
// lib/shortcuts — it renders whatever that module declares, so the list can
// never drift from the handlers' documentation.
import { useLayoutEffect, useId } from "react";
import Modal from "./modal/Modal";
import { acquireOverlay, releaseOverlay } from "../lib/ui-overlay";
import { isMac, isMod } from "../lib/platform";
import { shortcutGroups } from "../lib/shortcuts";

export function ShortcutsOverlay({ onClose }: { onClose: () => void }) {
  const headingId = useId();

  // Modal owns focus trap / Esc / backdrop but does NOT touch the shared
  // overlay registry — its callers do (see Listing.tsx:405-410). Without this
  // hold, Listing's document-level nav + file-op handlers keep firing on the
  // row behind the cheat sheet. Layout effect so the hold registers before
  // paint, i.e. no frame where those handlers still see isOverlayOpen() false.
  useLayoutEffect(() => {
    acquireOverlay();
    return () => releaseOverlay();
  }, []);

  // Mod+K toggles: pressing it again while open closes. preventDefault so the
  // browser's own Ctrl/Cmd+K (search-bar focus) doesn't steal it.
  useLayoutEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isMod(e) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const groups = shortcutGroups();

  return (
    <Modal title="Keyboard shortcuts" onClose={onClose} width={720} dialogClassName="shortcuts-dialog">
      <div className="shortcuts-grid">
        {groups.map((group) => (
          // Section labelled by its own visible heading, so screen readers
          // announce "Navigation, region" when landing in the group.
          <section
            key={group.title}
            className="shortcuts-group"
            aria-labelledby={`${headingId}-${slug(group.title)}`}
          >
            <h3 id={`${headingId}-${slug(group.title)}`} className="shortcuts-group-title">
              {group.title}
            </h3>
            <dl className="shortcuts-list">
              {group.items.map((item) => (
                <div className="shortcuts-row" key={`${item.label}|${item.keys.join("+")}`}>
                  <dt className="shortcuts-label">{item.label}</dt>
                  <dd className="shortcuts-keys">{renderKeys(item.keys)}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </Modal>
  );
}

function slug(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

// Combos read as ⌘C on macOS (the glyphs already imply "held together") but need
// the explicit joiner off it — Ctrl+C, not CtrlC. Rendering-only decision, kept
// out of the registry data.
function renderKeys(keys: string[]) {
  return keys.map((key, i) => (
    <span className="shortcuts-key-wrap" key={`${key}-${i}`}>
      {i > 0 && !isMac && <span className="shortcuts-key-join">+</span>}
      <kbd className="templates-key-pill shortcuts-kbd">{key}</kbd>
    </span>
  ));
}

export default ShortcutsOverlay;
