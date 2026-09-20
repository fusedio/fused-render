// HOW FULL THE MODEL'S HEAD IS, in the composer's control row.
//
// Claude Code prints the same fact in its statusline ("Context left until
// auto-compact"), and it is the one number a reader needs to decide whether to
// keep going in this chat or start a fresh one — the decision that, unmade,
// ends with a conversation that compacts mid-thought.
//
// A PILL THAT IS NOT A CONTROL. It sits with the three select pills and is
// typed like them, but it is a <span>: no hover wash, no focus ring, nothing to
// press. Everything it could open is already in the one sentence it carries —
// `data-hint` for the pointer, the same words as `aria-label` for a screen
// reader (the ring and the digits are `aria-hidden`, so the sentence is said
// once).
//
// The ring is 12px of SVG, sized in `em` so it tracks the row's type rather
// than freezing at one zoom. The TRACK is the pills' own border colour, and the
// arc is `currentColor`, which `composer.css` steps from `--c-dim` through
// `--c-status-progress` to `--c-error` — one property, three levels, and both
// themes come free because all three are `--c-*` tokens.
import {
  contextFill,
  contextLevel,
  contextPercent,
  contextSentence,
} from "./context-window";

export interface ContextMeterProps {
  /** `HistoryResponse.context.tokens` — the whole prompt of the latest reply. */
  tokens: number;
  /** `contextWindowFor(…)`. */
  window: number;
  /** The row's tightest rungs: keep the ring, drop the digits, so the meter
   *  costs a glyph rather than a seat when the width runs out. */
  compact?: boolean;
}

/** The ring's geometry, in its own 16-unit box: r=6.5 leaves the 1.6-wide
 *  stroke inside the viewBox at every angle. */
const R = 6.5;
const CIRCUMFERENCE = 2 * Math.PI * R;

/**
 * Nothing at all until there is something to say. A chat with no reply yet
 * (`tokens: 0`, or no `context` on the wire) draws NO pill rather than "0%":
 * an empty meter in an empty conversation is chrome saying nothing, and the
 * reader learns to ignore the seat before it ever has news.
 */
export function ContextMeter({ tokens, window, compact }: ContextMeterProps) {
  if (!Number.isFinite(tokens) || tokens <= 0) return null;
  const fill = contextFill(tokens, window);
  const level = contextLevel(fill);
  const sentence = contextSentence(tokens, window);
  // Drawn from 12 o'clock, clockwise: the arc is a dashed stroke whose first
  // dash is the filled part, which is one element rather than an arc path with
  // its large-arc-flag arithmetic (and it degrades to an empty ring at 0).
  const dash = `${(CIRCUMFERENCE * fill).toFixed(2)} ${CIRCUMFERENCE.toFixed(2)}`;
  return (
    <span
      className={`c-ctxmeter is-${level}${compact ? " is-compact" : ""}`}
      data-hint={sentence}
      aria-label={sentence}
      role="img"
    >
      <svg
        className="c-ctxmeter-ring"
        viewBox="0 0 16 16"
        aria-hidden="true"
        focusable="false"
      >
        <circle className="c-ctxmeter-track" cx="8" cy="8" r={R} />
        <circle
          className="c-ctxmeter-arc"
          cx="8"
          cy="8"
          r={R}
          strokeDasharray={dash}
          // -90°, so the arc starts at the top like every other dial.
          transform="rotate(-90 8 8)"
        />
      </svg>
      {compact ? null : (
        <span className="c-ctxmeter-pct" aria-hidden="true">
          {contextPercent(fill)}%
        </span>
      )}
    </span>
  );
}
