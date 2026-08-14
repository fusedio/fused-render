// A few shimmer bars standing in for a block of content while it loads — the
// same .skel-bar pattern the listing's placeholder rows use (shell.css), pulled
// out so the pages that showed a bare "Loading…" share one voice. Deliberately
// approximate: a handful of ragged bars reads as "content is coming" without
// pretending to be a pixel-accurate ghost of the real layout.
//
// aria-busy + a label so a screen reader gets what the "Loading…" text used to
// say; the bars themselves are decoration.
const DEFAULT_WIDTHS = [72, 54, 63];

export function SkeletonLines({
  rows = 3,
  widths = DEFAULT_WIDTHS,
  label = "Loading",
}: {
  rows?: number;
  // Bar widths as percentages of the container, cycled when there are more rows
  // than entries.
  widths?: number[];
  label?: string;
}) {
  return (
    <div className="skel-lines" role="status" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="skel-bar" style={{ width: `${widths[i % widths.length]}%` }} />
      ))}
    </div>
  );
}

export default SkeletonLines;
