// The Mod+K keyboard cheat sheet. Pure presentation over the registry in
// lib/shortcuts — it renders whatever that module declares, so the list can
// never drift from the handlers' documentation.
import { useLayoutEffect, useId } from "react";
import Modal from "@platform/ui/modal/Modal";
import { Kbd, KbdGroup } from "@platform/shadcn/ui/kbd";
import { SectionHeading } from "@platform/ui/flow/Typography";
import { acquireOverlay, releaseOverlay } from "@platform/lib/ui-overlay";
import { isMac, isMod } from "@platform/lib/platform";
import { shortcutGroups } from "@platform/lib/shortcuts";

export function ShortcutsOverlay({ onClose }: { onClose: () => void }) {
  const headingId = useId();

  // Modal owns focus trap / Esc / backdrop but does NOT touch the shared
  // overlay registry — its callers do (see Listing.tsx). Without this hold,
  // Listing's document-level nav + file-op handlers keep firing on the row
  // behind the cheat sheet. Layout effect so the hold registers before paint,
  // i.e. no frame where those handlers still see isOverlayOpen() false.
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
    <Modal title="Keyboard shortcuts" onClose={onClose} width={720} plainBody>
      {/* Two columns of grouped rows, collapsing to one on a narrow dialog:
          auto-fit + minmax is driven by available width, so it also works when
          the dialog is clamped by a small viewport. */}
      <div className="grid grid-cols-[repeat(auto-fit,minmax(260px,1fr))] gap-x-7 gap-y-4">
        {groups.map((group) => (
          // Section labelled by its own visible heading, so screen readers
          // announce "Navigation, region" when landing in the group.
          <section key={group.title} aria-labelledby={`${headingId}-${slug(group.title)}`}>
            <SectionHeading id={`${headingId}-${slug(group.title)}`} className="mb-2 text-xs">
              {group.title}
            </SectionHeading>
            <dl className="m-0 flex flex-col gap-0.5">
              {group.items.map((item) => (
                <div
                  className="flex items-baseline justify-between gap-3 rounded-md px-1.5 py-1 hover:bg-accent/50"
                  key={`${item.label}|${item.keys.join("+")}`}
                >
                  <dt className="min-w-0 text-sm">{item.label}</dt>
                  <dd className="m-0 shrink-0 whitespace-nowrap">{renderKeys(item.keys)}</dd>
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
  return (
    <KbdGroup>
      {keys.map((key, i) => (
        <span className="inline-flex items-center gap-0.5" key={`${key}-${i}`}>
          {i > 0 && !isMac && <span className="text-xs text-muted-foreground">+</span>}
          <Kbd className="text-foreground">{key}</Kbd>
        </span>
      ))}
    </KbdGroup>
  );
}

export default ShortcutsOverlay;
