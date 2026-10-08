// Skeleton cards/rows for a widget whose fetch is in flight. Moved from
// Home.tsx unchanged.

// Skeleton for one card while a strip's fetch is in flight. Two variants,
// because Home's two async strips draw two DIFFERENT real cards and a single
// shared shape would be wrong for one of them:
//   - "app"    mirrors AppPreviewCard/`.app-pcard` (apps.css) — title + a meta
//     row (tag pill, timestamp) OVER a full-bleed thumb, with a 32px mark in
//     the head's icon column. This variant DOES shimmer that square, unlike
//     the folder one below: which mark the real card draws (the app's own
//     `icon.svg`, or the generic star) arrives with the fetch, so a shimmer
//     there claims exactly what is true — and without it the skeleton's text
//     would start 42px left of where the real card's does and jump sideways
//     on the swap.
//   - "folder" mirrors FolderPreviewCard/`.fhb-card` (preferences.css) — a
//     head row over an inset thumb well. The real card's head DOES carry an
//     icon, but it's a static decorative folder glyph, identical on every
//     card and independent of the fetch — shimmering it (or even placing an
//     inert placeholder for it) would claim something is loading that isn't,
//     so both variants render NO icon.
// Built by reusing the real card's own classes rather than a bespoke shimmer
// shape with hand-measured dimensions: the browser lays both variants out
// with the exact same box model (padding, border, font metrics) the real
// card gets, so the skeleton's height tracks the real card's automatically —
// including through a CSS change neither this file nor a hand-derived
// constant would notice. Pure decoration — `aria-hidden`, with the row
// wrapper (below) carrying the one `role="status"` announcement for the
// whole strip, the way the old "Loading apps" line (a single ~18px
// paragraph) used to speak for the row rather than each line in it.
export function SkeletonCard({ variant }: { variant: "app" | "folder" }) {
  if (variant === "app") {
    return (
      <span className="app-pcard home-skel-card" aria-hidden="true">
        <span className="app-pcard-body">
          <span className="skel-bar app-pcard-icon-skel" />
          {/* The lines wrapper, not bars straight in the body: the body is a
              ROW since the icon column landed beside them (apps.css), so bars
              placed directly in it would sit side by side. */}
          <span className="app-pcard-lines">
            <span className="skel-bar" style={{ width: "58%" }} />
            <span className="app-pcard-meta">
              <span className="skel-bar" style={{ width: "110px" }} />
            </span>
          </span>
        </span>
        <span className="app-pcard-thumb home-skel-body" />
      </span>
    );
  }
  return (
    <span className="fhb-card home-skel-card" aria-hidden="true">
      <span className="fhb-card-head">
        {/* `.fh-card-text` is a shrink-to-fit flex item everywhere else (its
            real content — the name/path text — decides its width, and here
            it's the head row's ONLY child, since the icon is deliberately
            gone); a percentage-width `.skel-bar` inside it has nothing to
            shrink-to-fit against, so `home-skel-text` grows it to fill the
            head row like the real text effectively does once it's long
            enough to need the ellipsis. */}
        <span className="fh-card-text home-skel-text">
          {/* Wider bar on top: a name reads longer than the path underneath it
              on every real card head, and matching that keeps the skeleton
              from looking like a title-less placeholder. */}
          <span className="skel-bar" style={{ width: "72%" }} />
          <span className="skel-bar" style={{ width: "48%" }} />
        </span>
      </span>
      <span className="fhb-thumb home-skel-body" />
    </span>
  );
}

// A skeleton row is sized by `shown`, not by a guess or the section's peak
// `limit` — the same number of cards the real row will draw once the fetch
// lands (Home.tsx slices every strip to `shown`), so the swap from skeleton to
// content never changes the row's card count or width. Floored at 1: `shown`
// is 0 before the wrapper has been measured (see useStripCount), and a row of
// zero skeleton cards would render as nothing at all rather than as "loading".
export function SkeletonRow({
  count,
  label,
  variant,
}: {
  count: number;
  label: string;
  variant: "app" | "folder";
}) {
  return (
    <div className="home-row" role="status" aria-busy="true" aria-label={label}>
      {Array.from({ length: Math.max(1, count) }, (_, i) => (
        <SkeletonCard key={i} variant={variant} />
      ))}
    </div>
  );
}
