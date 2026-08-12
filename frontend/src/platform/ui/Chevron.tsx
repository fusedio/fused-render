// THE COLLAPSE CHEVRON — one glyph, stated once, for every control whose job is
// "this panel goes away in that direction".
//
// It exists because there were three hand-written copies of it and they had
// drifted: the left sidebar's collapse button drew it at 14px, the collapsed
// rail's expand button beside it at 16px, and the right-hand companion column's
// close button (SideChrome) at 16px as a `<polyline>` instead of a `<path>`.
// Same 24-unit grid and the same strokeWidth="2" in all three, which is exactly
// what made the difference hard to name and impossible to unsee once named: a
// 24-grid stroke of 2 renders at 2 x (box / 24) actual pixels, so 14px drew a
// 1.17px line and 16px drew a 1.33px one. The chevron on the left of the window
// was a THINNER chevron than the one on the right — a 14% weight difference
// between two controls the eye reads as one pair, from a size nobody chose for a
// reason.
//
// 16px is the app's icon norm (the bar and rail glyphs, ModeSwitcher, the
// mode-side icons — all 16px on a 0 0 24 24 viewBox at strokeWidth 2), so the
// two 16px copies were right and the 14px one was the outlier. Fixing it by
// editing the 14 to a 16 would have left three copies free to drift again on the
// next attribute anyone touched, hence a component: there is now one place where
// this glyph's weight is decided, and no way for two chevrons to disagree.
//
// A `<path>` and not the polyline, because that is the form the rest of the
// app's Lucide-style 24-grid icons use; with round caps and joins the two
// primitives rasterise identically, so this is purely about having one spelling.
//
// DIRECTION IS THE ONLY PROP. The chevron always points the way the panel goes,
// which is a fact about which seam the button sits on and not a style choice:
// `left` for a left-hand sidebar collapsing away from the content, `right` for a
// right-hand column doing the same, and the same glyph mirrored is what makes an
// expand control legible as the inverse of its collapse control. Size and weight
// are deliberately NOT props — a caller that wants a different weight wants a
// different icon, and this file is the answer to callers having had that choice.

// Both on the 24-unit grid, each a mirror of the other about x=12: (15,18) ->
// (9,12) -> (15,6) points left, (9,18) -> (15,12) -> (9,6) points right.
const D = {
  left: "m15 18-6-6 6-6",
  right: "m9 18 6-6-6-6",
} as const;

export default function Chevron({ dir }: { dir: "left" | "right" }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={D[dir]} />
    </svg>
  );
}
