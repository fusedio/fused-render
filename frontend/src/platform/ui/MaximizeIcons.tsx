// The Maximize / Restore glyphs: two corner arrows pointing out, two pointing in.
// Shared by the multi-pane explorer's panel bar (apps/explorer/Panel.tsx) and the
// side pane's full-screen button (apps/explorer/SideChrome.tsx), so the two read
// as one idea.
export function MaximizeIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5L9.75 6.25M2.5 13.5l3.75-3.75"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function RestoreIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M6.5 5.5h-4v-4M9.5 10.5h4v4M2.5 5.5l3.5-3.5M13.5 10.5L10 14"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
