// The animated body of a foldable sidebar section (Projects, Canvases,
// Favourites). A fold is a CSS grid whose one row goes 1fr ↔ 0fr, which the
// browser transitions without anyone measuring the rows (`height: auto` cannot
// be transitioned; a max-height guess would either clip or lag). The rows stay
// MOUNTED while folded so the fold-out has something to reveal and the fold-in
// something to hide; `inert` takes them out of the tab order, the accessibility
// tree and hit-testing meanwhile, and shell/sidebarArrowNav.ts skips them too.
//
// The wrapper sits inside the `.sidebar-section-body` useSectionContentCap
// measures, so the section's max-height follows the fold frame by frame and the
// sibling sections take the room as it goes (flexbox, see sidebar.css).
import type { ReactNode } from "react";

// React 18.3's DOM typings do not know `inert` (a boolean attribute; React 19
// does). An empty string is how the attribute is written in HTML.
const INERT = { inert: "" } as unknown as Record<string, never>;

export default function SectionFold({ open, children }: { open: boolean; children: ReactNode }) {
  return (
    <div className={"sidebar-fold" + (open ? "" : " collapsed")} {...(open ? {} : INERT)}>
      <div className="sidebar-fold-inner">{children}</div>
    </div>
  );
}
