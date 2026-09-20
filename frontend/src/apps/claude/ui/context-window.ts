// How big the window is, and how to say a number of tokens out loud.
//
// The arithmetic behind the composer's context meter, kept out of the component
// so the rules can be read and tested without a DOM. Nothing here knows about
// React, and nothing here fetches: it is given a model id and a count.

/** The default context window — every model this app offers, unqualified. */
export const WINDOW_DEFAULT = 200_000;
/** What a `[1m]` qualifier buys. */
export const WINDOW_1M = 1_000_000;

/** The CLI's context qualifier, the same trailing `[…]` `model-vocab`'s
 *  `QUALIFIER` strips — matched for its CONTENTS here rather than merely
 *  removed, because `[1m]` is the one thing on an id that changes the window's
 *  size. Anchored at the end, where the CLI writes it
 *  ("claude-opus-5[1m]", "fable[1m]"). */
const ONE_M = /\[\s*1m\s*\]$/i;

/**
 * The window `model` runs with, in tokens.
 *
 * TWO IDS CAN ANSWER THIS and the caller picks: the transcript's own
 * `context.model` (what the last reply was actually made with — the honest
 * answer for tokens already spent) and the composer pill's current value (the
 * only answer a chat that has not replied yet has). Both wear the qualifier the
 * same way, so one rule serves them.
 *
 * Unknown, empty, null — everything that is not explicitly a million-token id
 * — is the 200k default. Guessing LARGE on an unknown model would draw a meter
 * that says there is room when there is not, which is the one direction this
 * number must never be wrong in.
 */
export function contextWindowFor(model: string | null | undefined): number {
  const id = (model || "").trim();
  return ONE_M.test(id) ? WINDOW_1M : WINDOW_DEFAULT;
}

/**
 * THE WINDOW TO ACTUALLY DRAW AGAINST — the model's, unless the tokens already
 * spent prove a bigger one.
 *
 * `contextWindowFor` is only as good as the id it is handed, and in practice
 * neither id says `[1m]`: the transcript records the API's own model name
 * ("claude-fable-5-1" — measured against a live 1M session, which reports
 * 285k tokens under exactly that id), and the composer's pill offers the four
 * bare names with the qualifier folded off (`composer-defaults.MODELS`,
 * `listedModelIn`). The qualifier is a CLI spelling, not something the wire
 * carries back.
 *
 * So the count itself is the better witness, and in one direction it is
 * PROOF rather than a guess: 285k tokens cannot have fitted in a 200k window,
 * so that session is a million-token one whatever its id says. Escalation only
 * ever goes up, and only past a window that has already been exceeded — a
 * reading inside its window is left exactly where the id put it, so this can
 * never shrink a meter or invent headroom that has not been demonstrated.
 */
export function contextWindowOf(
  tokens: number,
  model: string | null | undefined,
): number {
  const window = contextWindowFor(model);
  if (!Number.isFinite(tokens)) return window;
  return tokens > window && window < WINDOW_1M ? WINDOW_1M : window;
}

/**
 * A token count in the fewest characters that stay honest: "950", "84k",
 * "1.2M".
 *
 * Rounded DOWN at every step (`Math.floor` on the unit, one decimal below 10),
 * so the meter never claims a thousand tokens that have not been spent and
 * "1.0M" cannot appear before a million actually has been. Below 1000 the
 * number is printed whole — there is nothing to shorten — and a negative or
 * non-finite input is "0", because the only thing worse than a wrong number
 * here is "NaNk".
 */
export function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  const unit = (value: number, suffix: string): string => {
    // One decimal only while it earns its place: 1.2M, 84k, 999k.
    const scaled = Math.floor(value * 10) / 10;
    return (scaled < 10 ? String(scaled) : String(Math.floor(value))) + suffix;
  };
  if (n >= 1_000_000) return unit(n / 1_000_000, "M");
  if (n >= 1_000) return unit(n / 1_000, "k");
  return String(Math.floor(n));
}

/** `tokens / window`, clamped to 0..1. A window of 0 or less is no window at
 *  all and reads as empty rather than as infinitely full. */
export function contextFill(tokens: number, window: number): number {
  if (!Number.isFinite(tokens) || !Number.isFinite(window) || window <= 0) return 0;
  if (tokens <= 0) return 0;
  return Math.min(1, tokens / window);
}

/** The meter's three steps, as a class suffix: how loud the ring is allowed to
 *  be. Quiet until 70% — a context bar that shouts at 40% is a context bar
 *  people stop reading — then the warning colour, then the error one at 90%,
 *  which is the point at which a long turn may not fit any more. */
export function contextLevel(fill: number): "ok" | "warn" | "full" {
  if (fill >= 0.9) return "full";
  if (fill >= 0.7) return "warn";
  return "ok";
}

/** The one sentence the meter says — its `data-hint` and its `aria-label`, the
 *  same words in both so the caption and the screen reader agree. */
export function contextSentence(tokens: number, window: number): string {
  const left = Math.max(0, window - tokens);
  return `Context ${formatTokens(tokens)} of ${formatTokens(window)} · ${formatTokens(left)} left`;
}

/** The percentage as the pill prints it: whole numbers, and never "100%" while
 *  a single token of room is left. */
export function contextPercent(fill: number): number {
  const pct = fill * 100;
  return pct >= 100 ? 100 : Math.max(0, Math.floor(pct));
}
