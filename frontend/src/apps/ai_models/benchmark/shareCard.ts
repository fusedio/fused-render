// The SHARE CARD: the comparison chart, this machine's hardware, and the
// fused render mark, redrawn as one self-contained PNG (SPEC AI-14).
//
// A benchmark number is only meaningful WITH the machine that produced it, so
// the hardware block is not decoration here — it is the caption without which
// the chart is unshareable. "62 tok/s" pasted into a chat says nothing; "62
// tok/s, macOS arm64, 12 cores, 32 GB, mps" is a result somebody else can
// compare against.
//
// **Drawn on a canvas from the same data the chart has, NOT rasterized from the
// DOM.** The two DOM routes both fail here: `html2canvas`-style serialization
// would need the app's whole cascade re-resolved into inline styles (the chart
// is CSS flex + percentage widths — see ComparisonChart.tsx), and
// SVG-foreignObject rasterization silently drops anything it cannot fetch,
// which is the same reliably-blank failure `platform/lib/appShot.ts` documents
// for the export path. Tab capture (appShot's answer) is wrong for a different
// reason: it needs the browser's share prompt, photographs the surrounding
// page chrome, and cannot include a hardware caption that is not on screen.
// The chart's inputs are `bars` + `metric` + `machine` — a few dozen numbers —
// so redrawing them is both exact and cheap.
//
// **The card is always DARK, whatever theme the app is in.** It is a poster,
// not a screenshot: every shared card should look like the same artefact, and
// the lime mark reads as branding on dark and as a highlighter stain on white.
// That is why the palette below is literal hex rather than a `getComputedStyle`
// read of the app's tokens — the values are the dark palette's own
// (styles/tokens.css), copied deliberately, because a canvas cannot reference a
// CSS variable and a theme-following card would ship two different brands.
import { formatSize } from "@platform/lib/format";
import {
  formatMetricSpecValue,
  middleEllipsis,
  niceAxisTicks,
  shortModelName,
  type ComparisonBar,
  type MetricSpec,
} from "@apps/ai_models/lib/benchmark";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { type AiBenchmarkMachine } from "@platform/lib/api";
import logoMark from "@assets/logo-black-bg-transparent.png";

// The dark palette, value for value from styles/tokens.css' `:root` block.
const INK = {
  bg: "#131417",
  fg: "#e8eaed",
  muted: "#9aa0a6",
  border: "#2a2d33",
  accent: "#E5FF44",
};

const SANS = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';
const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace';

// Geometry, in CSS pixels — the whole card is then drawn at `SCALE` device
// pixels per unit, so the PNG is crisp on a retina screen and in a chat client
// that shows it at half size.
const SCALE = 2;
const WIDTH = 900;
const PAD = 28;
// The name column, matching the on-screen chart's own 26-character clamp
// (ComparisonChart.tsx) at the mono size used below.
const NAME_W = 208;
const NAME_GAP = 12;
// Reserved to the right of the plot so a full-length bar's value label still
// fits inside the card rather than running off its edge — the same
// bar-then-label order the on-screen chart uses.
const VALUE_W = 96;
const ROW_H = 22;
const ROW_GAP = 6;
const BAR_H = 12;

/** This machine, in one line — the caption that makes the chart shareable.
 *
 *  Every part is omitted when the server did not report it (`cpuCount` and
 *  `totalMemoryBytes` are both nullable, and Windows reports no RAM at all —
 *  see `benchmark._total_memory_bytes`), because a card that says
 *  "— cores" reads as a bug in the card rather than as a gap in the data. */
export function hardwareLine(machine: AiBenchmarkMachine | null, device: string | null): string {
  if (!machine) return device ?? "";
  const parts: string[] = [platformLabel(machine.platform)];
  if (machine.arch) parts.push(machine.arch);
  if (machine.cpuCount) parts.push(`${machine.cpuCount} cores`);
  if (machine.totalMemoryBytes) parts.push(`${formatSize(machine.totalMemoryBytes)} RAM`);
  // The device the weights actually landed on — `mps`, `cuda`, `cpu`. Part of
  // the hardware story, not of the chart's: the same laptop benchmarks very
  // differently on `cpu` than on `mps`, so a card without it invites exactly
  // the wrong comparison.
  if (device) parts.push(device);
  return parts.filter(Boolean).join(" · ");
}

/** `platform.system()`'s answer, as a person names it. Only the one rename
 *  worth making — "Darwin" is the kernel, and nobody shares a benchmark taken
 *  on "Darwin". Linux and Windows already say what they are. */
function platformLabel(platform: string): string {
  return platform === "Darwin" ? "macOS" : platform;
}

/** The card's own provenance: the app version the runs were measured under and
 *  the day the card was made.
 *
 *  The VERSION is the measured one (a run's `appVersion`), never the running
 *  build — the app is part of what a benchmark measures, so a card redrawn
 *  after an upgrade must keep saying which version produced the numbers. */
export function provenanceLine(appVersion: string | null, now: Date): string {
  const day = now.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  return appVersion ? `fused render ${appVersion} · ${day}` : `fused render · ${day}`;
}

/** The line under the title: which metric these bars are, and which way it
 *  points.
 *
 *  **Direction is stated BOTH ways here, unlike the on-screen badge**
 *  (`metricUnitAndCue`, which deliberately says "lower is better" only for the
 *  metrics where the ordinary habit misreads, so that one cue is not buried).
 *  That reasoning holds beside a `<select>` a reader just used; it does not
 *  hold on a card that lands in somebody else's chat with no page around it,
 *  where "is a longer bar good here?" has no other way to be answered. The
 *  unit is left to the value labels, which all carry it. */
export function metricSubtitle(metric: MetricSpec): string {
  return `${metric.label} · ${metric.higherIsBetter ? "higher" : "lower"} is better`;
}

/** What the download is called. Slugged from the two things that identify the
 *  chart — its capability and its metric — so a folder of several cards is
 *  readable rather than "share (3).png". */
export function shareCardFilename(capability: string, metric: MetricSpec): string {
  return `fused-render-benchmark-${slug(capability)}-${slug(metric.key)}.png`;
}

/** The muted phrase that turns a bare capability label into the whole title —
 *  "Speech to text" becomes "Speech to text local AI benchmark". There is no
 *  longer a separate caption row to say this (it used to live right-aligned
 *  in the now-deleted brand row); folding it into the title keeps the card
 *  saying what kind of benchmark this is without spending a whole line on it.
 *  Sentence case, because it reads as a continuation of the label's phrase,
 *  not as its own heading. Pulled out as its own function so the drawing
 *  code can colour and size it separately from the label, and so a test can
 *  pin the exact wording without a canvas. */
export function titleSuffix(): string {
  return "local AI benchmark";
}

function slug(text: string): string {
  return text
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

/** The card's height for `bars.length` rows — pure, so the layout can be
 *  reasoned about (and tested) without a canvas. */
export function shareCardHeight(barCount: number): number {
  const plot = barCount * ROW_H + Math.max(0, barCount - 1) * ROW_GAP;
  return (
    PAD + // top padding
    // No separate brand row any more — the wordmark moved to the footer and
    // the caption folded onto the title line below, so the card opens
    // directly on the title rather than spending a row on branding first.
    30 + // the title (capability label + its "local AI benchmark" suffix)
    20 + // the metric subtitle
    14 + // gap before the plot
    plot +
    22 + // the axis labels
    18 + // gap down to the footer rule
    16 + // the footer line
    PAD
  );
}

export interface ShareCardInput {
  capability: string;
  metric: MetricSpec;
  /** Already ranked best-first by `comparisonBars` — drawn in the order given,
   *  exactly as the on-screen chart draws it. */
  bars: ComparisonBar[];
  machine: AiBenchmarkMachine | null;
  /** The capability's common device (`commonDevice`), or null where the models
   *  disagree — in which case the card names no device rather than picking one
   *  model's and captioning every bar with it. */
  device: string | null;
  appVersion: string | null;
  /** Injected so the drawing is deterministic under a test. */
  now?: Date;
}

/** Draw the card and hand back its PNG bytes.
 *
 *  Rejects only if the canvas itself is unavailable or refuses to encode; a
 *  missing LOGO is not a failure. The footer's "fused render 0.4.53 · ..."
 *  text is drawn unconditionally regardless of whether the mark loaded, so a
 *  failed `loadLogo()` just means the footer ships without its mark, not that
 *  the card loses its branding — the card without its picture is still the
 *  result somebody asked to share. */
export async function renderShareCard(input: ShareCardInput): Promise<Blob> {
  const { bars, metric } = input;
  const height = shareCardHeight(bars.length);
  const canvas = document.createElement("canvas");
  canvas.width = WIDTH * SCALE;
  canvas.height = height * SCALE;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("This browser would not give us a canvas to draw the card on.");
  ctx.scale(SCALE, SCALE);
  ctx.textBaseline = "top";

  ctx.fillStyle = INK.bg;
  ctx.fillRect(0, 0, WIDTH, height);

  let y = PAD;
  const mark = await loadLogo();

  // -- title + metric --------------------------------------------------------
  // The capability label carries the heading weight; the "local AI benchmark"
  // suffix rides beside it in the muted colour, so the two read as one phrase
  // ("Speech to text local AI benchmark") rather than a label plus an
  // unrelated caption. The suffix's x is `label`'s MEASURED width, not a
  // guessed offset — the label's text varies per capability ("Text
  // generation" vs "Embeddings"), so a fixed offset would either collide with
  // a wide label or leave a gap after a narrow one.
  //
  // Both runs are drawn at the SAME 24px size — weight and colour do the
  // distinguishing, not size. A smaller suffix (tried first) reads as a
  // caption stapled onto a heading rather than as one phrase, and it also
  // reintroduces the baseline mismatch below: `textBaseline` is "top", so two
  // sizes sharing one `y` sit on different baselines and need a hand-eyeballed
  // nudge to line back up. One size sidesteps both problems at once, which is
  // why a future "shrink the suffix back down" edit should stop here.
  ctx.fillStyle = INK.fg;
  ctx.font = `600 24px ${SANS}`;
  const label = capabilityLabel(input.capability);
  ctx.fillText(label, PAD, y);
  const labelWidth = ctx.measureText(label).width;
  ctx.fillStyle = INK.muted;
  ctx.font = `24px ${SANS}`;
  ctx.fillText(` ${titleSuffix()}`, PAD + labelWidth, y);
  y += 30;
  ctx.fillStyle = INK.muted;
  ctx.font = `13px ${SANS}`;
  ctx.fillText(metricSubtitle(metric), PAD, y);
  y += 20 + 14;

  // -- the plot --------------------------------------------------------------
  // Same axis machinery the on-screen chart uses (`niceAxisTicks`), so the
  // shared card's gridlines are the ones the reader was looking at rather
  // than a second, differently-rounded scale.
  let peak = 0;
  for (const bar of bars) if (bar.value > peak) peak = bar.value;
  const ticks = niceAxisTicks(peak, metric, 4);
  const axisMax = ticks.length > 0 ? ticks[ticks.length - 1]!.value : peak || 1;
  const plotX = PAD + NAME_W + NAME_GAP;
  const plotW = WIDTH - PAD - VALUE_W - plotX;
  const plotTop = y;
  const plotH = bars.length * ROW_H + Math.max(0, bars.length - 1) * ROW_GAP;
  const at = (value: number) => plotX + (value / axisMax) * plotW;

  for (const tick of ticks) {
    const x = Math.round(at(tick.value)) + 0.5;
    ctx.beginPath();
    ctx.setLineDash(tick.value === 0 ? [] : [3, 3]);
    ctx.strokeStyle = INK.border;
    ctx.lineWidth = 1;
    ctx.moveTo(x, plotTop);
    ctx.lineTo(x, plotTop + plotH);
    ctx.stroke();
  }
  ctx.setLineDash([]);

  bars.forEach((bar, i) => {
    const top = plotTop + i * (ROW_H + ROW_GAP);
    const mid = top + ROW_H / 2;
    ctx.fillStyle = INK.fg;
    ctx.font = `12px ${MONO}`;
    ctx.textBaseline = "middle";
    ctx.fillText(middleEllipsis(shortModelName(bar.model), 26), PAD, mid);
    // `min-width: 2px` on screen, and the same floor here: a real measurement
    // that rounds to nothing must still leave a mark, or the card shows a row
    // with no bar at all where the number says otherwise.
    const width = Math.max(2, (bar.value / axisMax) * plotW);
    ctx.fillStyle = INK.accent;
    roundedRect(ctx, plotX, mid - BAR_H / 2, width, BAR_H, 3);
    ctx.fill();
    ctx.fillStyle = INK.muted;
    ctx.font = `12px ${SANS}`;
    ctx.fillText(formatMetricSpecValue(bar.value, metric), plotX + width + 8, mid);
    ctx.textBaseline = "top";
  });
  y = plotTop + plotH + 8;

  // The tick labels, pulled back onto the plot at both ends exactly as the
  // on-screen axis does (`.am-bench-compare-axis span:first-child/:last-child`)
  // — a centred label under the first or last gridline would hang off the card.
  ctx.fillStyle = INK.muted;
  ctx.font = `11px ${SANS}`;
  ticks.forEach((tick, i) => {
    ctx.textAlign = i === 0 ? "left" : i === ticks.length - 1 ? "right" : "center";
    ctx.fillText(tick.label, at(tick.value), y);
  });
  ctx.textAlign = "left";
  y += 14 + 18;

  // -- the footer: the machine, and where the card came from ------------------
  ctx.beginPath();
  ctx.strokeStyle = INK.border;
  ctx.moveTo(PAD, Math.round(y) + 0.5);
  ctx.lineTo(WIDTH - PAD, Math.round(y) + 0.5);
  ctx.stroke();
  y += 14;
  ctx.font = `12px ${SANS}`;
  ctx.fillStyle = INK.fg;
  ctx.fillText(hardwareLine(input.machine, input.device), PAD, y);
  ctx.fillStyle = INK.muted;
  ctx.textAlign = "right";
  const provenance = provenanceLine(input.appVersion, input.now ?? new Date());
  ctx.fillText(provenance, WIDTH - PAD, y);
  if (mark) {
    // The mark moved down here from the old brand row: it now sits flush
    // against the provenance text it used to sit above, so the two read as
    // one group at the card's bottom right rather than two unrelated
    // mentions of the brand. Sized for THIS line (14px, not the header's
    // 32px) — measured off the provenance text's actual width via
    // `measureText` rather than guessed, because the width changes with the
    // app version and the date's month name.
    const markSize = 14;
    const markGap = 6;
    const provenanceLeft = WIDTH - PAD - ctx.measureText(provenance).width;
    // `y` is the top of the 12px line (textBaseline stays "top"); +6 is that
    // line's vertical midpoint, so centring the mark there against half its
    // own size keeps the mark and the text visually centred on each other.
    ctx.drawImage(mark, provenanceLeft - markGap - markSize, y + 6 - markSize / 2, markSize, markSize);
  }
  ctx.textAlign = "left";

  return await encode(canvas);
}

function roundedRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

function encode(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error("The card could not be encoded as a PNG."))),
      "image/png",
    );
  });
}

// The mark, decoded once per session and reused — a share is a repeatable
// click, and re-decoding a 699² PNG on each press is work with no answer to
// give. `null` (never a throw) when the asset will not load, which the caller
// draws around.
let logo: Promise<HTMLImageElement | null> | null = null;

function loadLogo(): Promise<HTMLImageElement | null> {
  logo ??= new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = logoMark;
  });
  return logo;
}

/** How a card actually left the app. `cancelled` is the share sheet being
 *  dismissed — a deliberate "never mind", which must NOT then fall through to
 *  saving a file the reader just declined to send. */
export type ShareOutcome = "shared" | "copied" | "downloaded" | "cancelled";

/** Hand the PNG over, best channel first: the OS share sheet where the browser
 *  has one, the clipboard where it does not (the bytes are `image/png`, the one
 *  format `ClipboardItem` accepts — so unlike the Playground's rendered images
 *  a clipboard write is legitimate here), and a plain download as the floor
 *  that always works. */
export async function deliverShareCard(blob: Blob, filename: string): Promise<ShareOutcome> {
  const file = new File([blob], filename, { type: "image/png" });
  if (navigator.canShare?.({ files: [file] })) {
    try {
      await navigator.share({ files: [file] });
      return "shared";
    } catch (e) {
      // Dismissing the sheet rejects with AbortError. Anything else is a real
      // failure of the share channel, and falls through to the next one.
      if ((e as Error).name === "AbortError") return "cancelled";
    }
  }
  try {
    await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
    return "copied";
  } catch {
    // No clipboard permission, no ClipboardItem, or a non-secure context.
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  // Appended before the click, and removed right after: some browsers (most
  // reliably Firefox) only honour a synthetic `.click()` on an anchor that is
  // actually IN the document — a detached one is free to be silently ignored.
  // The revoke is deferred a tick past that, not run synchronously right
  // after `.click()`: revoking here raced the browser's own async read of the
  // blob URL to start the save, which could win and leave `deliverShareCard`
  // reporting "downloaded" for a save that never actually landed a file.
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
  return "downloaded";
}

/** What the button says after a delivery — the outcomes differ in WHERE the
 *  card went, and a single "Done" would leave a reader hunting for a file that
 *  is actually on their clipboard. */
export function shareOutcomeNote(outcome: ShareOutcome): string {
  switch (outcome) {
    case "shared":
      return "Shared";
    case "copied":
      return "Copied as an image";
    case "downloaded":
      return "Saved as a PNG";
    case "cancelled":
      return "";
  }
}
