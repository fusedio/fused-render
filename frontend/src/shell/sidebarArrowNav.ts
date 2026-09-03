// Arrow keys walk the sidebar's Projects and Bookmarks rows (owner, 2026-08-26):
// Up/Down move to the previous/next row and OPEN it, so the sidebar behaves as
// one list you can step through without the mouse. The "list" is every
// navigable row link (`a.bookmark-name`) inside #sidebar in DOM order — Projects
// first, then the bookmark tree as it is currently unfolded. Folder rows and
// the "+ New app" row are not links and are stepped over.
//
// WHO OWNS THE ARROWS. The explorer's Listing drives its own selection with
// Up/Down whenever focus sits on <body> (useListingSelection: `navActive`), and
// two document listeners cannot negotiate order (App.tsx records why). So this
// one claims the keys only where the listing cannot be in play:
//   - focus is INSIDE the sidebar (a row was clicked or reached by Tab), or
//   - focus is on <body> and the page is an app page (/apps/<folder>), whose
//     main pane is the app's frame or its tasks/files — no listing to fight.
// Anywhere else the keys stay with the page.
//
// STAYING IN THE CHAIN. Opening a row navigates, and the sidebar remounts on
// every navigation (App.tsx), which drops focus to <body>. The mount hook below
// puts focus back on the row that is now active — but only when the last step
// was one of ours, so a mouse click never yanks focus into the sidebar.
import { useEffect } from "react";
import { isOverlayOpen } from "@platform/lib/ui-overlay";
import { appPathFromPath } from "@shell/current-apps-lib";

// Two dialects, one list: the Projects rows mark their link with
// `data-sidebar-row` (CurrentAppsSection), the bookmark tree still speaks the
// legacy `a.bookmark-name`. Same for "which row is current" — `aria-current`
// on the link, or the legacy `.active` on its row.
const ROW_LINKS = "#sidebar a[data-sidebar-row], #sidebar a.bookmark-name";

function isCurrentRow(a: HTMLAnchorElement): boolean {
  return (
    a.getAttribute("aria-current") === "page" ||
    !!a.closest(".bookmark-row")?.classList.contains("active")
  );
}

// Set when an arrow step navigated; consumed by the next mount.
let refocusPending = false;

function rowLinks(): HTMLAnchorElement[] {
  return Array.from(document.querySelectorAll<HTMLAnchorElement>(ROW_LINKS));
}

function inTextField(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || (el as HTMLElement).isContentEditable;
}

/** The row to step FROM: the focused row link if one is focused, else the row
 *  marked `.active` (the page on screen), else none. */
function currentIndex(links: HTMLAnchorElement[]): number {
  const el = document.activeElement;
  const focused = links.findIndex((a) => a === el);
  if (focused !== -1) return focused;
  return links.findIndex(isCurrentRow);
}

export function stepSidebarRow(e: KeyboardEvent): void {
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  if (e.isComposing || e.defaultPrevented) return;
  if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
  if (isOverlayOpen()) return;
  const el = document.activeElement;
  if (inTextField(el)) return;
  const sidebar = document.getElementById("sidebar");
  if (!sidebar) return;
  const inSidebar = !!el && sidebar.contains(el);
  const onBody = !el || el === document.body || el === document.documentElement;
  if (!inSidebar && !(onBody && appPathFromPath(location.pathname) !== null)) return;

  const links = rowLinks();
  if (!links.length) return;
  const cur = currentIndex(links);
  const down = e.key === "ArrowDown";
  // Nothing current: Down starts at the top, Up at the bottom. Ends stop.
  const next = cur === -1 ? (down ? 0 : links.length - 1) : cur + (down ? 1 : -1);
  if (next < 0 || next >= links.length) {
    e.preventDefault();
    return;
  }
  e.preventDefault();
  const target = links[next];
  refocusPending = true;
  target.focus();
  // The row's own onClick does the navigation (and its active/no-op logic).
  target.click();
}

/** Mounted once per GlobalSidebar: the document listener plus the refocus that
 *  keeps the keyboard chain alive across the remount a navigation causes. */
export function useSidebarArrowNav(): void {
  useEffect(() => {
    if (refocusPending) {
      refocusPending = false;
      const active = rowLinks().find(isCurrentRow);
      active?.focus({ preventScroll: false });
    }
    document.addEventListener("keydown", stepSidebarRow);
    return () => document.removeEventListener("keydown", stepSidebarRow);
  }, []);
}
