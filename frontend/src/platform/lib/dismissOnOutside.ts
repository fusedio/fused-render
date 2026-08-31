// Close an open status-bar panel when the user clicks anywhere else (D574,
// user: "clicking anywhere else should background the notification"). Shared
// by all three sections — platform/ui/DownloadManager.tsx,
// shell/ActivityDock.tsx, shell/RepoUpdatesDock.tsx — which is why it is a hook
// here rather than three copies of the same listener.
//
// A HOOK, NOT A REUSE, because there was nothing to reuse: this app has three
// existing outside-dismiss implementations (apps/explorer/BarMenu.tsx,
// apps/explorer/PaneModeMenu.tsx, platform/ui/ContextMenu.tsx) and every one
// of them is a hand-rolled `document.addEventListener("pointerdown", …)`
// inside the component that owns the menu, closing over that component's own
// state — none exposes a reusable seam. This follows their convention
// (pointerdown, not click, so the panel is gone before whatever the user
// actually aimed at reacts) instead of inventing a fourth spelling.
//
// SCOPED TO THE WHOLE `.dl-host`, not the panel alone: the host wraps the chip
// AND the panel, so a click on the chip counts as INSIDE and never reaches
// this handler. That is what stops the double-fire — the chip's own onClick
// closes an open panel, and if this listener also saw that click it would
// close first and then let the chip reopen it, making the chip look dead. It
// is also what keeps clicks on `Unload` / `Cancel` / `Clear` / a row's ✕ from
// dismissing the panel out from under them.
//
// `capture: true` matches ContextMenu.tsx: a panel row that stops propagation
// on its own pointerdown must not be able to leave the panel undismissable.
import { useEffect, useRef, type RefObject } from "react";

export function useDismissOnOutside(
  hostRef: RefObject<HTMLElement | null>,
  open: boolean,
  close: () => void,
): void {
  // `close` is a fresh closure every render (it reads `collapsed`/`autoOpen`).
  // Through a ref so the effect's dependencies stay `[open]` and the listener
  // is attached once per open/close rather than torn down and rebuilt on every
  // poll tick — six seconds apart, but a panel is open across many of them.
  const closeRef = useRef(close);
  closeRef.current = close;

  useEffect(() => {
    // Not merely an optimisation: the platform-side tests mount these views
    // with no DOM globals at all (see autoExpand.ts's own note on why that
    // file imports nothing heavier than react).
    if (!open || typeof document === "undefined") return;

    const onDown = (e: Event) => {
      const host = hostRef.current;
      if (host && e.target instanceof Node && host.contains(e.target)) return;
      closeRef.current();
    };
    // Escape as well — expected of anything that floats over the page, and
    // the only way out for a keyboard user who never pointed at anything.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
    };

    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [hostRef, open]);
}
