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
// `color`) are unchanged, including the `.is-failure` -> `var(--error)` tint.
//
// 15px, not the app's usual 16px icon norm — the exact size round 2's own
// finding kept for legibility ("Legible at a glance… a bigger glyph make the
// header's clickability obvious"); shrinking it to match `Chevron.tsx`/
// `PanelIcon.tsx`'s 16px would be a size nobody asked to change.
export default function DlChevron({ collapsed }: { collapsed: boolean }) {
  return (
    <svg
      className={"dl-chevron" + (collapsed ? " is-collapsed" : "")}
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="18 15 12 9 6 15" />
    </svg>
  );
}
