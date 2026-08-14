// Shared Finder / File-Explorer glyph (streamline-logos:mac-finder-logo, MIT
// line version). Used by the breadcrumb's "Open in Finder" button and the
// listing context menu's "Reveal in Finder" item so both show the SAME icon.
// 16x16, viewBox 0 0 24 24, currentColor stroke — inherits the row's colour.
export function FinderIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
      <g fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12.5 1.5c-.833 2-2.5 7.2-2.5 12h3c0 2.537.2 6.6 1 9m-7.5-15v2m10-2v2" />
        <path d="M5.5 15.5c.667 1 2.9 3 6.5 3s5.667-2 6.5-3" />
        <path d="M1.5 18.5v-13a4 4 0 0 1 4-4h13a4 4 0 0 1 4 4v13a4 4 0 0 1-4 4h-13a4 4 0 0 1-4-4" />
      </g>
    </svg>
  );
}
