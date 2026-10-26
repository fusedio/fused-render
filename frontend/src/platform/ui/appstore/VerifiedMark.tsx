// The green "reviewed" tick that follows a listed app's name — the scalloped
// badge Instagram and X put after a verified account, in GitHub's outline
// colouring (green stroke + green check), scaled to the text (1em) so it sits
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
      {/* Twelve-point scalloped badge, drawn as a green outline over a faint
          green tint with a green check (GitHub's verified style, owner's
          pick) rather than a solid disc with a white check. */}
      <path
        fill="currentColor"
        fillOpacity="0.16"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
        d="M12 2.2l2.1 1.7 2.7-.5 1.1 2.5 2.5 1.1-.5 2.7L21.8 12l-1.7 2.1.5 2.7-2.5 1.1-1.1 2.5-2.7-.5L12 21.8l-2.1-1.7-2.7.5-1.1-2.5-2.5-1.1.5-2.7L2.2 12l1.7-2.1-.5-2.7 2.5-1.1 1.1-2.5 2.7.5z"
      />
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M8.2 12.3l2.6 2.6 5.2-5.4"
      />
    </svg>
  );
}
