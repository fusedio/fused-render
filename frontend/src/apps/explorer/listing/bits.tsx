// Small presentational pieces of the listing: skeleton rows, the clipboard
// pill, the git status badge, and search-match highlighting.
import { highlightSegments } from "@platform/lib/fuzzy";
import { FLIP_KEY_ATTR } from "@platform/lib/flip";
import { gitMarkFor } from "./git-mark";

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
// (see Breadcrumb.tsx). `cut` and `copied` are never both true: the clipboard
// holds a single op.
export function ClipMark({ cut, copied }: { cut: boolean; copied: boolean }) {
  if (!cut && !copied) return null;
  const label = cut ? "Cut" : "Copied";
  return (
    <span className={"clip-mark" + (cut ? " cut" : " copied")} title={label}>
      {label}
    </span>
  );
}

// The git status badge in a row's name cell: one letter, tinted the same as the
// name it follows (styles/explorer.css keys both off the row's `git-*` class).
// See listing/git-mark.ts for why a letter exists at all rather than colour
// alone. Renders nothing when git has nothing to say — which is the common case
// and must cost the row no layout.
export function GitMark({ status }: { status?: string }) {
  const mark = gitMarkFor(status);
  if (!mark) return null;
  return (
    // role="img": a plain `<span>` is `role=generic`, where `aria-label` is
    // unsupported and dropped — a screen reader would announce the bare
    // glyph ("!" as an exclamation mark), defeating the whole reason this
    // badge carries a letter and a label instead of colour alone (see this
    // file's header). Same trap, same fix, as ScheduleTaskViews.tsx's own
    // status dot.
    <span className="git-mark" role="img" title={mark.label} aria-label={mark.label}>
      {mark.letter}
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

// Splits a single highlight segment's own text on "/", wrapping each
// separator in its own element so CSS can give it margin/color — WITHOUT
// inserting any character into the string itself (the copy-to-clipboard text
// must stay the exact path). This only rearranges what's already inside one
// segment; it never creates a new segment, so a fuzzy match that straddles a
// "/" stays the one continuous <mark> highlightSegments produced for it, not
// two marks with a plain slash between them.
function withSpacedSeparators(text: string): React.ReactNode {
  const parts = text.split("/");
  if (parts.length === 1) return text;
  const out: React.ReactNode[] = [];
  parts.forEach((part, i) => {
    if (i > 0) out.push(
      <span key={`sep-${i}`} className="path-sep">
        /
      </span>,
    );
    if (part) out.push(part);
  });
  return out;
}

// Opt-in sibling of renderHighlight, for multi-segment PATHS only (a
// filename has no "/" to space out, and renderHighlight's own callers must
// not change behavior — see FilesHome.tsx, which uses this for its path span
// but plain renderHighlight for its name span).
export function renderHighlightPath(text: string, positions: number[]) {
  return highlightSegments(text, positions).map((seg, i) =>
    seg.match ? (
      <mark key={i} className="search-mark">
        {withSpacedSeparators(seg.text)}
      </mark>
    ) : (
      <span key={i}>{withSpacedSeparators(seg.text)}</span>
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
