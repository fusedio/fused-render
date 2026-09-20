// HOW FULL THE MODEL'S CONTEXT WINDOW IS — Claude Code's own arithmetic.
//
// Every number and every sentence in this file is a mirror of the CLI's, read
// out of the installed binary (2.1.278) and written down in
// `.claude-design/context-meter/claude-code-parity.md`. The point of mirroring
// rather than inventing is that the reader ALREADY has a meter for this — the
// CLI's statusline and its `/context` — and two meters over one conversation
// that disagree by ten points are worse than one meter.
//
// The two halves disagree ON PURPOSE, and §8 of the spec is where the CLI says
// so: the percentage on the pill is input-only over the FULL model window
// (statusline parity), while the warning line above the box counts output too
// and measures against the AUTO-COMPACT window. Same conversation, two
// questions — "how much of the model's head is in use" and "how long until the
// CLI compacts this" — and they have different denominators.
//
// Nothing here touches React and nothing here fetches: it is handed the model
// id and the latest reply's `usage`.
import type { ContextUsage } from "../protocol/types";

/** The default context window (`mxe`). */
export const WINDOW_DEFAULT = 200_000;
/** What a native-1M model, or a `[1m]` qualifier, buys (`ret`). */
export const WINDOW_1M = 1_000_000;
/** The cap on the reserved-output subtraction (`e$t`). */
export const OUTPUT_RESERVE = 20_000;
/** Auto-compact head-room below the output reserve (`K0t`). */
export const COMPACT_HEADROOM = 13_000;
/** The warning appears this far before the compact threshold. */
export const WARN_MARGIN = 20_000;
/** The model-default auto-compact cap for the models that enforce one (`Mj`). */
export const AUTOCOMPACT_CAP = 200_000;

/** A trailing `[…]` qualifier, so an id can be read without it. */
const QUALIFIER = /\[[^\]]*\]\s*$/;
/** `EF`'s own test, literally: `/\[1m\]/i` anywhere on the id, which is how it
 *  works on an alias (`opus[1m]`) and a full id (`claude-opus-4-8[1m]`) alike. */
const ONE_M = /\[1m\]/i;

/** Explicitly NOT 1M-capable (`o_e`): every `claude-3-*`, Haiku 4.5, and Opus
 *  4.0/4.1/4.5. Listed first, so a family rule below cannot promote them. */
const NOT_1M: readonly RegExp[] = [/claude-3-/, /haiku/, /opus-4-[015](?!\d)/];
/** Native 1M today: Sonnet 5, the Fable models, Opus 4.7+ and Opus 5 — plus
 *  the bare ALIASES the composer's pill offers (`fable`, `opus`, `sonnet`),
 *  which name exactly those models. `haiku` is not here and falls through to
 *  the 200k default, which is also what its full id does. */
const NATIVE_1M: readonly RegExp[] = [
  /fable/,
  /^sonnet$/,
  /sonnet-5/,
  /^opus$/,
  /opus-5/,
  /opus-4-[78](?!\d)/,
];
/** `cIr` — the models pinned to the 200k AUTO-COMPACT window whatever their own
 *  window is. Opus 5 is the interesting one: a million-token head that the CLI
 *  still compacts at 167k. */
const ENFORCED: readonly RegExp[] = [
  /sonnet-4-6/,
  /opus-4-6/,
  /opus-4-8/,
  /opus-5/,
];

function ident(model: string | null | undefined): string {
  return (model || "").trim().toLowerCase();
}

/**
 * `uf(model)` — the FULL window the model runs with, in tokens.
 *
 * Unknown, empty, null: the 200k default, exactly as the CLI's last branch
 * does. Guessing LARGE on an unknown id would draw room that is not there,
 * which is the one direction this number must never be wrong in.
 */
export function modelWindow(model: string | null | undefined): number {
  const raw = ident(model);
  if (!raw) return WINDOW_DEFAULT;
  if (ONE_M.test(raw)) return WINDOW_1M;
  const base = raw.replace(QUALIFIER, "");
  if (NOT_1M.some((re) => re.test(base))) return WINDOW_DEFAULT;
  return NATIVE_1M.some((re) => re.test(base)) ? WINDOW_1M : WINDOW_DEFAULT;
}

/** Where the auto-compact window came from. We can only ever see two of the
 *  CLI's six sources — the env var, the setting and the server-side tables are
 *  not ours to read — and the distinction that matters to the copy is the one
 *  `Dv` itself draws: `"auto"` means "nobody set a window, use the model's",
 *  and everything else is an ENFORCED one. */
export type WindowSource = "model-default" | "auto";

export interface AutoCompactWindow {
  window: number;
  source: WindowSource;
  /** `source !== "auto"` — what the warning line's two spellings turn on. */
  enforced: boolean;
}

/** `Dv(model)` with only the sources this app can observe: the model default
 *  for the `cIr` set, the model's own window for everybody else. */
export function autoCompactWindow(
  model: string | null | undefined,
): AutoCompactWindow {
  const base = ident(model).replace(QUALIFIER, "");
  const full = modelWindow(model);
  if (base && ENFORCED.some((re) => re.test(base))) {
    return {
      window: Math.min(full, AUTOCOMPACT_CAP),
      source: "model-default",
      enforced: true,
    };
  }
  return { window: full, source: "auto", enforced: false };
}

export interface ContextThresholds {
  /** `uf` — the denominator of the PILL's percentage. */
  model: number;
  /** `Dv(...).window`. */
  window: number;
  /** `rG` = window − 20k. */
  effective: number;
  /** `Zde` = effective − 13k: where auto-compact fires. */
  compactAt: number;
  /** compactAt − 20k: where the line above the box appears. */
  warnAt: number;
  enforced: boolean;
}

/**
 * Every threshold for one model, in one read.
 *
 * Worked, and pinned by the tests: a 1M model with no enforced window gives
 * `effective 980_000`, `compactAt 967_000` — the docs' "about 967K tokens by
 * default". A 200k model gives `180_000` / `167_000`, warning at `147_000`.
 */
export function contextThresholds(
  model: string | null | undefined,
): ContextThresholds {
  const { window, enforced } = autoCompactWindow(model);
  const effective = window - OUTPUT_RESERVE;
  const compactAt = effective - COMPACT_HEADROOM;
  return {
    model: modelWindow(model),
    window,
    effective,
    compactAt,
    warnAt: compactAt - WARN_MARGIN,
    enforced,
  };
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : 0;
}

/**
 * `VFt` — the INPUT side of one reply: fresh input plus the cache it wrote plus
 * the cache it read.
 *
 * This is the whole prompt that went up the wire, and therefore the whole of
 * the window in use: every request re-sends the conversation, so summing the
 * turns would count the same tokens once per turn. The docs say it verbatim of
 * the statusline's percentage — "calculated from input tokens only… it does not
 * include `output_tokens`".
 */
export function contextInput(usage: ContextUsage | null | undefined): number {
  if (!usage) return 0;
  return (
    count(usage.input_tokens) +
    count(usage.cache_creation_input_tokens) +
    count(usage.cache_read_input_tokens)
  );
}

/** `m0` — input plus output, which is what the AUTO-COMPACT arithmetic counts:
 *  what came back this turn goes up as input on the next one. */
export function contextTotal(usage: ContextUsage | null | undefined): number {
  if (!usage) return 0;
  return contextInput(usage) + count(usage.output_tokens);
}

/**
 * `IKt` — the pill's percentage: input-only over the FULL model window,
 * rounded and clamped to 0..100.
 */
export function usedPct(
  model: string | null | undefined,
  usage: ContextUsage | null | undefined,
): number {
  const window = modelWindow(model);
  if (window <= 0) return 0;
  const pct = Math.round((contextInput(usage) / window) * 100);
  return Math.min(100, Math.max(0, pct));
}

/** `fHe` minus the two states we cannot be in: this app never turns
 *  auto-compact off, so there is no `blocked` rung and no red. */
export type ContextLevel = "ok" | "warn" | "compact";

/**
 * Which rung a total (INCLUDING output) sits on for this model.
 *
 * `compact` is not a warning about something that might happen — it is the
 * threshold at which the CLI compacts, so by the time the reader sees it the
 * next turn is the one that gets summarised.
 */
export function contextLevel(
  model: string | null | undefined,
  total: number,
): ContextLevel {
  const { compactAt, warnAt } = contextThresholds(model);
  if (!Number.isFinite(total) || total <= 0) return "ok";
  if (total >= compactAt) return "compact";
  if (total >= warnAt) return "warn";
  return "ok";
}

/**
 * THE ONE DIM LINE ABOVE THE BOX (`mae`), verbatim in both spellings, or "".
 *
 * The CLI has no colour ramp here while auto-compact is on: warn and compact
 * both render dim, and red is reserved for the case where auto-compact is OFF
 * and the conversation is about to hit a wall rather than a summary. This app
 * cannot turn auto-compact off, so this line is always dim — and deliberately
 * so, because a red line that appears on every long conversation is a red line
 * people stop reading.
 */
export function warnLine(
  model: string | null | undefined,
  usage: ContextUsage | null | undefined,
): string {
  const total = contextTotal(usage);
  if (contextLevel(model, total) === "ok") return "";
  const { compactAt, effective, enforced } = contextThresholds(model);
  if (enforced) {
    const left = Math.max(0, Math.round(((compactAt - total) / compactAt) * 100));
    return `${left}% until auto-compact`;
  }
  const left = Math.max(0, Math.round(((effective - total) / effective) * 100));
  return `${100 - left}% context used`;
}

/** A token count in the fewest characters that stay honest: "950", "285k",
 *  "1.2M". Rounded DOWN at every step, so the meter never claims a thousand
 *  tokens that were not spent. */
export function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  const unit = (value: number, suffix: string): string => {
    const scaled = Math.floor(value * 10) / 10;
    return (scaled < 10 ? String(scaled) : String(Math.floor(value))) + suffix;
  };
  if (n >= 1_000_000) return unit(n / 1_000_000, "M");
  if (n >= 1_000) return unit(n / 1_000, "k");
  return String(Math.floor(n));
}

/** Thousands separators, written out rather than left to `toLocaleString`:
 *  `/context` prints one grouping and this must print the same one whatever
 *  locale the machine is set to. */
export function groupDigits(n: number): string {
  if (!Number.isFinite(n)) return "0";
  const whole = String(Math.max(0, Math.round(n)));
  return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** The pill's whole sentence — its tooltip and its accessible name, the same
 *  words in both so the caption and the screen reader cannot drift. */
export function contextHint(
  model: string | null | undefined,
  usage: ContextUsage | null | undefined,
): string {
  const window = modelWindow(model);
  const used = contextInput(usage);
  const line = `Context: ${formatTokens(used)} of ${formatTokens(window)} tokens (${usedPct(model, usage)}%)`;
  // AN ESTIMATE IS LABELLED AS ONE. Straight after a compaction the CLI has no
  // usage to report at all until the next API call, so this reading is the
  // boundary row's own `postTokens` — a number the compactor predicted rather
  // than one the API charged.
  return usage?.compacted ? `${line} · compacted, estimate` : line;
}

// ---- `/context`, as much of it as this side of the wire can know ----------

/** One square of the grid. */
export interface ContextSquare {
  kind: "messages" | "buffer" | "free";
  glyph: string;
}

/** One legend row under the grid. */
export interface ContextLegendRow {
  kind: "messages" | "buffer" | "free";
  label: string;
  glyph: string;
  tokens: number;
  /** To one decimal place, as `/context` prints it. */
  pct: string;
  /** An honesty note the CLI does not need and we do — see `contextReport`. */
  note?: string;
}

export interface ContextReport {
  model: string;
  window: number;
  used: number;
  pct: number;
  columns: number;
  rows: number;
  squares: ContextSquare[];
  legend: ContextLegendRow[];
  /** `Context is N% full`, or "" below 80%. */
  suggestion: string;
}

const GLYPH_FULL = "⛁"; // a filled square, fullness >= 0.7
const GLYPH_PART = "⛀"; // ...and its lighter twin below that
const GLYPH_BUFFER = "⛝";
const GLYPH_FREE = "⛶";

/** `round`, min 1 for a segment that has any tokens at all: a category worth
 *  200 tokens still gets a square, because a row in the legend with nothing in
 *  the grid reads as a bug. Free space is NOT min-1 — it is the remainder. */
function squaresFor(tokens: number, window: number, total: number): number {
  if (tokens <= 0) return 0;
  return Math.max(1, Math.round((tokens / window) * total));
}

/**
 * Everything the meter's popover draws — the same facts `/context` prints, for
 * the one category this side of the wire can actually see.
 *
 * WHAT WE CANNOT SPLIT, we do not pretend to: the CLI knows what its system
 * prompt, its tools, its skills and its memory files each cost because it built
 * them; all we have is the total the API charged. So the whole input sum is the
 * `Messages` row, and it says `(all context in use)` rather than implying the
 * other nine categories were measured and came out at zero.
 */
export function contextReport(
  model: string | null | undefined,
  usage: ContextUsage | null | undefined,
): ContextReport {
  const id = (model || "").trim();
  const { model: modelWindow, compactAt, enforced } = contextThresholds(model);
  const columns = modelWindow >= WINDOW_1M ? 20 : 10;
  const rows = 10;
  const cells = columns * rows;
  const used = Math.max(0, Math.min(contextInput(usage), modelWindow));
  // `window − H`: the room the CLI holds back so a compaction has somewhere to
  // land. Drawn only when a window is actually enforced — with `source: "auto"`
  // the CLI prints no buffer row at all.
  const buffer = enforced ? Math.max(0, modelWindow - compactAt) : 0;

  let messageCells = squaresFor(used, modelWindow, cells);
  let bufferCells = squaresFor(buffer, modelWindow, cells);
  if (messageCells + bufferCells > cells) {
    // The arithmetic can overflow by a square at the rounding edges; the buffer
    // gives way first, because the number being read is the used one.
    messageCells = Math.min(messageCells, cells);
    bufferCells = Math.max(0, cells - messageCells);
  }
  const freeCells = Math.max(0, cells - messageCells - bufferCells);

  const exact = (used / modelWindow) * cells;
  const squares: ContextSquare[] = [];
  for (let i = 0; i < messageCells; i++) {
    // How full THIS square is: the one at the frontier is a part-square, and
    // the CLI draws it lighter below 0.7.
    const fullness = Math.min(1, Math.max(0, exact - i));
    squares.push({
      kind: "messages",
      glyph: fullness >= 0.7 ? GLYPH_FULL : GLYPH_PART,
    });
  }
  for (let i = 0; i < bufferCells; i++) {
    squares.push({ kind: "buffer", glyph: GLYPH_BUFFER });
  }
  for (let i = 0; i < freeCells; i++) {
    squares.push({ kind: "free", glyph: GLYPH_FREE });
  }

  const pctOf = (n: number): string => ((n / modelWindow) * 100).toFixed(1);
  const legend: ContextLegendRow[] = [];
  if (used > 0) {
    legend.push({
      kind: "messages",
      label: "Messages",
      glyph: GLYPH_FULL,
      tokens: used,
      pct: pctOf(used),
      note: "(all context in use)",
    });
  }
  if (buffer > 0) {
    legend.push({
      kind: "buffer",
      label: "Autocompact buffer",
      glyph: GLYPH_BUFFER,
      tokens: buffer,
      pct: pctOf(buffer),
    });
  }
  const free = Math.max(0, modelWindow - used - buffer);
  legend.push({
    kind: "free",
    label: "Free space",
    glyph: GLYPH_FREE,
    tokens: free,
    pct: pctOf(free),
  });

  const pct = usedPct(model, usage);
  return {
    model: id,
    window: modelWindow,
    used,
    pct,
    columns,
    rows,
    squares,
    legend,
    suggestion: pct >= 80 ? `Context is ${pct}% full` : "",
  };
}
