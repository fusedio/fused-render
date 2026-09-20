// Sizing for the two foldable sidebar sections (Projects, Bookmarks). Both are
// `flex: 1 1 0` scroll containers, so with plenty of rows on both sides they
// split the sidebar's free height equally and each scrolls inside its half. A
// section that needs LESS than its half must not keep the blank remainder,
// though — the other section should take it — and a collapsed section is just
// its heading. Flexbox does that redistribution on its own once an item has a
// max-height: the capped item freezes at its cap and the leftover goes to the
// other. The cap is the section's own content height, which CSS cannot express
// portably (`max-height: fit-content` in the block axis is not Baseline), so it
// is measured here.
//
// What is measured is the `.sidebar-section-body` wrapper, NOT the section: the
// section is the flex item, so unclamped it stands at its share of the free
// height — read that back as the cap and it never comes down again when rows
// leave. The wrapper is always mounted and holds the heading plus whatever rows
// the collapse state shows, so its offsetHeight IS the content height and a
// ResizeObserver on it fires for every add/remove/fold/rename. The heading's
// height is also written as the section's min-height so a very short window
// squeezes the rows, never the toggle that brings them back (the sidebar's own
// overflow-y takes over below that).
//
// Pop-ups a section renders (tooltip, icon picker, context menu, modal) must
// sit OUTSIDE the wrapper so they cannot inflate the measurement.
import { useLayoutEffect, type RefObject } from "react";

export const SECTION_BODY_CLASS = "sidebar-section-body";

export function useSectionContentCap(sectionRef: RefObject<HTMLElement | null>): void {
  useLayoutEffect(() => {
    const section = sectionRef.current;
    if (!section) return;
    const body = section.querySelector<HTMLElement>(":scope > ." + SECTION_BODY_CLASS);
    if (!body) return;
    const heading = body.querySelector<HTMLElement>(":scope > .sidebar-heading");
    const measure = () => {
      section.style.maxHeight = body.offsetHeight + "px";
      if (heading) section.style.minHeight = heading.offsetHeight + "px";
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(body);
    if (heading) ro.observe(heading);
    return () => ro.disconnect();
  }, [sectionRef]);
}
