// THE STATUS BAR'S DISCLOSURE GLYPH — one inline SVG, for the three chips
// this bar draws (Activity, Updates; Models — same markup, own file). Round 3
// (user, on the shipped chip: "the collapsing arrow is very ugly").
//
// It replaces a literal text glyph (`⌃`), CSS-rotated. A text caret renders
// at whatever weight and baseline the font stack gives it — it is not drawn,
// it is TYPESET — so it sat at a different weight and optical size than every
// other control near it. Every other disclosure in this app already draws its
// chevron as an inline lucide-style SVG and rotates THAT with CSS instead
// (`shell/NewJobModal.tsx`'s `ICON_CHEVRON`/`ICON_CHEVRON_DOWN`, both
// `<polyline>`s on a 24-unit grid at strokeWidth 2; `platform/ui/
// MenuIcons.tsx`'s `chevron` entry, rotated 90° by `[open] > summary`) — this
// follows that convention rather than inventing a fourth. It is deliberately
// NOT built on `platform/ui/Chevron.tsx`: that component's own header comment
// retired it down to the crumb bar's left/right history arrows on purpose,
// and its two glyphs point sideways, not up — the wrong shape for a control
// that has to rotate through "open" and "closed" the way `[open] > summary`
// and `ICON_CHEVRON_DOWN` do.
//
// One shared component rather than three hand-rolled copies (`DownloadManager
// .tsx`, `ModelsDock.tsx`, `RepoUpdatesDock.tsx` all drew the same `⌃`) for
// the same reason `Chevron.tsx`'s own header gives: three copies is how a
// weight or a viewBox drifts on the next attribute anyone touches without
// noticing the other two.
//
// SEMANTICS UNCHANGED from round 2 (D568 finding #6): the glyph points the
// way the panel opens — UP, since `.dl-panel` opens `bottom: 100%` — and
// `.dl-chevron.is-collapsed` (notifications.css) still rotates it 90° to a
// closed read pointing right. This file only supplies the shape and the
// class names; the rotation and the color (`currentColor`, driven by CSS
// `color`) are unchanged in their MECHANISM, including the `.is-failure` ->
// `var(--error)` tint — only the REST-state color and stroke weight moved,
// D570 below.
//
// 15px, not the app's usual 16px icon norm — the exact size round 2's own
// finding kept for legibility ("Legible at a glance… a bigger glyph make the
// header's clickability obvious"); shrinking it to match `Chevron.tsx`/
// `PanelIcon.tsx`'s 16px would be a size nobody asked to change.
//
// D570 (user, on the round-3 SVG: "the up arrow is glaring") — the same
// over-correction shape D568 finding #7 hit in the opposite direction (an
// 11px `--fg-muted` text glyph that was too QUIET). `strokeWidth` drops 2 ->
// 1.5: at 15px on this 24-unit grid, 2 renders a ~1.25px stroke, heavier
// than `.dl-summary`'s 500-weight text beside it and the loudest mark in
// the bar — wrong for a disclosure affordance whose job is to be found, not
// to shout. The REST color moved out of this file entirely: `currentColor`
// still drives the stroke, but `.dl-chevron` (notifications.css) now sets
// `--fg-muted` at rest and only rises to `--fg` on `.dl-toggle:hover`/
// `:focus-visible` — quieter than the old flat full-`--fg` fill, and it
// gains real hover feedback the affordance should have had from round 1.
// 1.5 is still well clear of a hairline: this stays structurally a drawn
// glyph, not a reversion to the 11px text caret round 2 replaced.
export default function DlChevron({ collapsed }: { collapsed: boolean }) {
  return (
    <svg
      className={"dl-chevron" + (collapsed ? " is-collapsed" : "")}
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="18 15 12 9 6 15" />
    </svg>
  );
}
