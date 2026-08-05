// Small presentational pieces of the listing: skeleton rows, the clipboard
// pill, and search-match highlighting.
import { highlightSegments } from "@platform/lib/fuzzy";
import { FLIP_KEY_ATTR } from "@platform/lib/flip";

// Shimmering placeholder rows shown while the listing fetch is in flight —
// same column shape as the real rows (icon + name + size + mtime), just with
// shimmer bars instead of text so the table never reads as "frozen". The
// width cycles make the bars ragged like real filenames.
const SKEL_NAME_W = [70, 45, 82, 38, 60, 50, 74, 42, 66, 34];
const SKEL_SIZE_W = [34, 28, 40, 24, 36, 30, 26, 38, 32, 22];
export function skeletonRows(n: number): React.ReactNode {
  return Array.from({ length: n }, (_, i) => (
    <tr key={i} className="skel-row">
      <td className="name">
        <span className="skel-bar icon-skel" />
        <span className="skel-bar" style={{ width: `${SKEL_NAME_W[i % SKEL_NAME_W.length]}%` }} />
      </td>
      <td className="size">
        <span className="skel-bar" style={{ width: SKEL_SIZE_W[i % SKEL_SIZE_W.length] }} />
      </td>
      <td className="mtime">
        <span className="skel-bar" style={{ width: 84 }} />
      </td>
    </tr>
  ));
}

// The pending-clipboard mark on a row: a small "Cut" / "Copied" pill in the name
// cell, alongside the row-level styling (dim for cut, accent edge + wash for
// copy). This IS the whole pending-clipboard UI — there is no chrome-level chip
// (see Breadcrumb.tsx) — so the pill carries the Esc affordance in its tooltip.
// `cut` and `copied` are never both true: the clipboard holds a single op.
export function ClipMark({ cut, copied }: { cut: boolean; copied: boolean }) {
  if (!cut && !copied) return null;
  return (
    <span className={"clip-mark" + (cut ? " cut" : " copied")} title="Press Esc to cancel">
      {cut ? "Cut" : "Copied"}
    </span>
  );
}

export function renderHighlight(text: string, positions: number[]) {
  return highlightSegments(text, positions).map((seg, i) =>
    seg.match ? (
      <mark key={i} className="search-mark">
        {seg.text}
      </mark>
    ) : (
      <span key={i}>{seg.text}</span>
    )
  );
}

// Where the scroll position is pinned across a dir-watch refresh: the lead
// (selected) row, or failing that the topmost row still in view. Returns null
// when there is nothing to anchor to (empty or unmounted listing).
export function measureScrollAnchor(
  scroller: HTMLElement,
): { key: string; top: number; scrollTop: number } | null {
  let el = scroller.querySelector<HTMLElement>("tr.row.lead");
  if (!el) {
    for (const row of scroller.querySelectorAll<HTMLElement>(`[${FLIP_KEY_ATTR}]`)) {
      if (row.offsetTop + row.offsetHeight > scroller.scrollTop) {
        el = row;
        break;
      }
    }
  }
  const key = el?.getAttribute(FLIP_KEY_ATTR);
  if (!el || !key) return null;
  return { key, top: el.offsetTop, scrollTop: scroller.scrollTop };
}
