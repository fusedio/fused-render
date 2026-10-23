// Connected nodes: a canvas is a graph of UDFs — the sidebar's Canvases
// entry (GlobalSidebar CANVASES_ICON draws the same path), the Canvases
// section's rows and the canvas page's header mark. A module of its own, not
// the sub-app barrel: the sidebar imports it, and the barrel would pull the
// whole sub-app into the shell's main bundle for one glyph (the reason
// feature-flag.ts is split out too).
export function CanvasGlyph({ size = 16 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="6" cy="6" r="3" />
      <circle cx="18" cy="10" r="3" />
      <circle cx="9" cy="18" r="3" />
      <path d="M8.8 7.1 15.2 9M7.9 15.4 6.8 8.9M11.6 16.6l4.2-4.4" />
    </svg>
  );
}
