// One status-bar chip (statusbar redesign): a label, an optional count, and
// an optional progress line along its bottom edge. Every chip in the bar —
// Models, Activity, Notifications — is this component, so they cannot drift
// apart in look or behaviour. Hover/pin logic lives in `lib/statusChip.ts`
// on the host wrapper; this is the button alone.
//
//   label     "Models" | "qwen2.5-7b" | "Erasing" | "Notifications"
//   count     0 hides the numeral. Models/Activity show it from 2 (one item
//             shows its own name instead); Notifications from 1. One grey
//             pill for every chip, the sidebar's own count style.
//   tone      idle      nothing here — muted text
//             on        something here — regular text
//             failure   a failed job — red text (existing `.is-failure` rule)
//   progress  undefined no line; null indeterminate sweep; 0..1 a fill
export type ChipTone = "idle" | "on" | "failure";

export interface StatusChipProps {
  label: string;
  count?: number;
  tone?: ChipTone;
  progress?: number | null;
  open: boolean;
  pinned?: boolean;
  /** Tooltip for the chip; also the accessible name's prefix. */
  title: string;
  onClick: () => void;
  /** The longer form for screen readers, e.g. "Models, 2 loaded". */
  ariaLabel?: string;
}

export default function StatusChip({
  label,
  count = 0,
  tone = count > 0 ? "on" : "idle",
  progress,
  open,
  pinned = false,
  title,
  onClick,
  ariaLabel,
}: StatusChipProps) {
  const indeterminate = progress === null;
  const width = typeof progress === "number" ? `${Math.round(progress * 100)}%` : undefined;
  return (
    <button
      type="button"
      className={
        "dl-toggle sc" +
        (tone === "idle" ? " is-idle" : "") +
        (tone === "failure" ? " is-failure" : "") +
        (pinned ? " is-pinned" : "")
      }
      onClick={onClick}
      aria-expanded={open}
      aria-label={ariaLabel}
      title={title}
    >
      <span className="dl-summary">{label}</span>
      {count > 0 && <span className="sc-num">{count}</span>}
      {progress !== undefined && (
        <span className="sc-progress" aria-hidden="true">
          <span
            className={"sc-progress-fill" + (indeterminate ? " is-indeterminate" : "")}
            style={width ? { width } : undefined}
          />
        </span>
      )}
    </button>
  );
}
