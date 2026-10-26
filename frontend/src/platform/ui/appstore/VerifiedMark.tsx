// The green "reviewed" tick that follows a listed app's name — the same
// shape Instagram and X put after a verified account: a scalloped green
// badge with a white check, scaled to the surrounding text (1em) so it sits
// in a 28px hero title and a 13px card name alike. Shown only for a row of
// the reviewed Marketplace (CONTRIB_LOOP.md §18): every row there passed
// the review check, so the mark means "Fused reviewed this release".
// Presentational; the caller decides when an app is listed.

export interface VerifiedMarkProps {
  /** Tooltip + accessible name. */
  title?: string;
  className?: string;
}

export function VerifiedMark({ title = "Reviewed by Fused", className }: VerifiedMarkProps) {
  return (
    <svg
      className={"as-verified" + (className ? " " + className : "")}
      viewBox="0 0 24 24"
      role="img"
      aria-label={title}
      focusable="false"
    >
      <title>{title}</title>
      {/* Twelve-point scalloped badge. */}
      <path
        fill="currentColor"
        d="M12 1.6l2.3 1.9 2.9-.6 1.2 2.8 2.8 1.2-.6 2.9 1.9 2.3-1.9 2.3.6 2.9-2.8 1.2-1.2 2.8-2.9-.6L12 22.4l-2.3-1.9-2.9.6-1.2-2.8-2.8-1.2.6-2.9L1.5 12l1.9-2.3-.6-2.9 2.8-1.2 1.2-2.8 2.9.6z"
      />
      <path
        fill="none"
        stroke="#fff"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M7.6 12.3l2.9 2.9 5.9-6"
      />
    </svg>
  );
}
