// The Usage tab of /ai-models (SPEC AI-12) — how many tokens `fused.ai` has
// generated in this server process, and when it generated them.
//
// The other three tabs answer questions about models at rest: what is on this
// disk, what could be, which backend would run it. None of them says whether
// anything is actually being GENERATED — so a page stuck in a render loop
// calling `fused.ai()` on every tick, or a chat box quietly re-asking the model
// after every keystroke, looked exactly like an idle machine. The graph is what
// makes that visible, and the shape of the bars is the finding: one prompt is a
// spike, a runaway loop is a wall.
//
// Everything here is **this process's own memory** (the server counts into a
// ring, writes nothing to disk, and forgets on restart), which is why the
// footer states when counting began rather than letting a number that resets at
// every app launch pass for a daily total.
//
// Numbers are OUTPUT tokens unless a line says otherwise: "generated" is the
// question, and it is also the only figure both tiers report — a local worker
// counts what it emitted and never sees a prompt token (SPEC AI-3), so input
// tokens are shown where they exist and left as "—" where nobody reported one.
//
// Volume is not the whole question, so three more counters share the tab, each
// answering something the token count alone cannot:
//   * FAILURES, by kind. A page whose every call times out has generated zero
//     tokens — indistinguishable from a page nobody opened until the failures
//     are on screen beside them, which is why they also mark the timeline.
//   * SPEED (tokens/second, and the seconds behind it). The number anybody
//     choosing between two local models actually wants, and the explanation
//     when a model that landed on the CPU (AI-11b) "feels broken".
//   * TIER. This page's subject is the local half of `fused.ai`, so Claude and
//     local models are counted apart — one merged figure answers neither
//     "what is this laptop doing" nor "what am I sending out".
//
// The one number here that is NOT measured is the ESTIMATED COST, and it is
// labelled that way everywhere it appears. This app knows what a model
// generated; it does not know what anybody is charging for it, so the rate is
// an input box per model rather than a table of prices that would go stale
// silently and be believed anyway. It defaults to $1 per million OUTPUT tokens
// and counts output only — a default nobody's real bill matches exactly, which
// is the point of it being editable and of the word "estimated" beside every
// figure it produces.
import { useEffect, useState, type CSSProperties } from "react";
import {
  getAiUsage,
  type AiUsage,
  type AiUsageBucket,
  type AiUsageCounts,
  type AiUsageTier,
} from "@platform/lib/api";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";

// The windows the graph offers. Three, because they are three different
// questions — "what is happening right now", "what has this session been
// doing", "what has this app done since I opened it" — and the longest is the
// server's whole retention (AI-12), so there is no window the picker can ask
// for that the store cannot fill.
const RANGES = [5, 15, 60] as const;

// Half a bucket. The newest column is always partial, so refreshing at the
// bucket width would leave it visibly stale for most of its life; at half of it
// the bar grows in two steps instead of appearing finished from birth.
const POLL_MS = 5000;

const fmt = (n: number) => n.toLocaleString();

// $ per MILLION output tokens. One rate, on output only: input tokens are not
// counted here at all, so a row's estimate is a single multiplication a reader
// can check in their head against the Generated column.
const DEFAULT_RATE = 1;
const PER = 1_000_000;

/** `output_tokens` at `rate` per million, as dollars — or null when the rate
 *  box has been emptied or typed into something that is not a number, which is
 *  a state to render blank rather than to guess a zero for. */
function cost(outputTokens: number, rate: string): number | null {
  const value = Number(rate);
  if (rate.trim() === "" || !Number.isFinite(value) || value < 0) return null;
  return (outputTokens / PER) * value;
}

/** Money, at a precision that does not round a real cost to nothing: fractions
 *  of a cent are the ordinary case for a local session, and "$0.00" beside
 *  40,000 tokens reads as a broken calculation rather than as a small bill. */
function money(dollars: number): string {
  if (dollars === 0) return "$0";
  if (dollars < 0.01) return `$${dollars.toFixed(4)}`;
  return `$${dollars.toFixed(2)}`;
}

/** Compact, for a label that has to fit over a bar: 940, 1.2k, 3.4M. */
function compact(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1e6) return `${Number((n / 1e3).toFixed(1))}k`;
  return `${Number((n / 1e6).toFixed(1))}M`;
}

const clock = (epochSeconds: number) =>
  new Date(epochSeconds * 1000).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });

/** "2h 14m", the length of the window these totals cover. Not `timeAgo` (which
 *  says "2h ago", a moment rather than a span) — the sentence is "counting for
 *  this long", and a duration is what it needs. */
function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/** One bar's hover text: the minute it covers, and what happened in it. */
function barTitle(bucket: AiUsageBucket, seconds: number): string {
  const when = `${clock(bucket.t)}–${clock(bucket.t + seconds)}`;
  const parts: string[] = [];
  if (bucket.completions) {
    parts.push(`${fmt(bucket.output_tokens)} generated`);
    if (bucket.input_tokens !== null) parts.push(`${fmt(bucket.input_tokens)} read`);
    parts.push(
      bucket.completions === 1 ? "1 completion" : `${bucket.completions} completions`,
    );
  }
  if (bucket.failures) parts.push(`${bucket.failures} failed`);
  return `${when} · ${parts.length ? parts.join(" · ") : "idle"}`;
}

/** The graph: one column per bucket, height by tokens generated.
 *
 *  Bars, not a line. The series is a COUNT PER INTERVAL, not a level being
 *  sampled — a line between two spikes draws a slope through minutes in which
 *  nothing happened, which is the one thing this graph must not imply.
 *
 *  Columns are laid out right-to-left from `now` at a fixed width, so a young
 *  process (whose series is shorter than the window, because the server emits
 *  nothing for time before it started counting) fills the right-hand side and
 *  leaves the past blank — rather than stretching ten minutes of history across
 *  an axis labelled with an hour.
 */
function UsageChart({ usage }: { usage: AiUsage }) {
  const columns = Math.max(
    1,
    Math.round((usage.window_minutes * 60) / usage.bucket_seconds),
  );
  const peak = usage.buckets.reduce((max, b) => Math.max(max, b.output_tokens), 0);
  const last = usage.buckets.length - 1;
  const style = {
    "--am-usage-col": `${100 / columns}%`,
    // No gap once the columns are thinner than a few pixels: a 1px gutter
    // between 360 bars is most of the graph.
    "--am-usage-gap": columns > 90 ? "0px" : "1px",
  } as CSSProperties;

  // The window reaches back further than this process does. That stretch is not
  // a quiet period — nobody was counting — so it is drawn as an explicit void
  // rather than as a run of zero bars under an axis label that would date them.
  const missing = Math.max(0, columns - usage.buckets.length);

  return (
    <div className="am-usage-chart">
      <div
        className="am-usage-bars"
        style={style}
        role="img"
        aria-label={`${fmt(usage.window.output_tokens)} tokens generated in the last ${usage.window_minutes} minutes`}
      >
        {missing > 0 && (
          <div
            className="am-usage-void"
            style={{ flex: `0 0 ${(missing / columns) * 100}%` }}
            title="Before this server started counting"
          />
        )}
        {usage.buckets.map((bucket, i) => (
          <div
            key={bucket.t}
            className="am-usage-slot"
            title={barTitle(bucket, usage.bucket_seconds)}
          >
            {/* A period in which calls FAILED is a period this graph would
                otherwise draw as quiet: no tokens, no bar. The tick is at the
                baseline, under whatever the bar did, because it is a different
                fact about the same ten seconds — not a smaller amount of the
                same thing. */}
            {bucket.failures > 0 && <div className="am-usage-fail" />}
            <div
              // The newest column is still filling — the bucket it stands for
              // has not ended — so it is drawn faint. Without that, the last
              // bar reads as a sudden drop in throughput on every single
              // refresh, which is the graph lying twice a second.
              className={"am-usage-bar" + (i === last ? " partial" : "")}
              style={{
                height:
                  peak > 0 && bucket.output_tokens > 0
                    ? `${Math.max(2, (bucket.output_tokens / peak) * 100)}%`
                    : "0",
              }}
            />
          </div>
        ))}
      </div>
      <div className="am-usage-axis">
        {/* The server's own clock on both ends (`now`, not Date.now()): the
            buckets were placed by it, and two clocks disagreeing by a second
            would put the last bar in the future. */}
        <span>{clock(usage.now - usage.window_minutes * 60)}</span>
        {/* The scale, in the axis row rather than floating over the plot: a
            label pinned above the tallest bar is a label ON the data, and the
            tallest bar is exactly what somebody is trying to read. */}
        <span>{peak > 0 ? `peak ${compact(peak)} / ${usage.bucket_seconds}s` : ""}</span>
        <span>now</span>
      </div>
    </div>
  );
}

const TIER_LABEL: Record<AiUsageTier, string> = {
  claude: "Claude",
  local: "Local",
};

/** Per model, biggest generator first — the server's order, kept.
 *
 *  Speed is a per-MODEL column and not just a total, because that is the level
 *  it means something at: "this machine averages 30 tok/s" is a mixture of
 *  whatever ran, while "this 8B model runs at 24 tok/s here" is the number
 *  somebody is choosing between two downloads with.
 *
 *  So is the RATE, for a different reason: every model is priced differently
 *  and this app knows none of those prices, so the rate is a box per row rather
 *  than one figure for the table. `rates` is held by the caller so a poll — one
 *  every five seconds — cannot reset a number somebody is still typing.
 */
function UsageModels({
  usage,
  rates,
  onRate,
}: {
  usage: AiUsage;
  rates: Record<string, string>;
  onRate: (model: string, rate: string) => void;
}) {
  const rateFor = (model: string) => rates[model] ?? String(DEFAULT_RATE);
  // Rows whose rate box is empty or unparseable contribute nothing rather than
  // a zero, and the total says so by going blank itself only when NO row could
  // be priced — a half-priced table still has a real subtotal.
  const priced = usage.models
    .map((row) => cost(row.output_tokens, rateFor(row.model)))
    .filter((c): c is number => c !== null);
  const total = priced.length ? priced.reduce((a, b) => a + b, 0) : null;

  return (
    <table className="am-usage-table">
      <thead>
        <tr>
          <th>Model</th>
          <th>Completions</th>
          <th>Read</th>
          <th>Generated</th>
          <th>Speed</th>
          <th>Failed</th>
          {/* The unit is in the header, not in the box: a placeholder inside an
              input disappears the moment somebody types, and this is the one
              thing a reader has to know to trust the column beside it. */}
          <th>$/M output</th>
          <th>Est. cost</th>
        </tr>
      </thead>
      <tbody>
        {usage.models.map((row) => {
          const estimate = cost(row.output_tokens, rateFor(row.model));
          return (
            <tr key={row.model}>
              <td className="am-usage-model" title={row.model}>
                {/* The tier as a chip rather than as a column of its own: it is
                    readable off the id (a slash means a Hub repo, AI-1) and this
                    spares four characters of table width for saying so. The
                    overflow row gets no chip: it holds whatever mixture arrived
                    past the cap, and either label on it would be a guess. */}
                {row.tier !== null && (
                  <span className={"am-usage-tier am-usage-tier-" + row.tier}>
                    {TIER_LABEL[row.tier]}
                  </span>
                )}
                {row.model}
              </td>
              <td>{fmt(row.completions)}</td>
              {/* An em dash, never a 0: this model's tier never reported what it
                  read, and printing zero would be an answer instead of an
                  absence. Same rule for a speed nothing timed. */}
              <td>{row.input_tokens === null ? "—" : fmt(row.input_tokens)}</td>
              <td>{fmt(row.output_tokens)}</td>
              <td>{row.tokens_per_second === null ? "—" : `${row.tokens_per_second}/s`}</td>
              {/* Not error-styled. A failure count is a fact about a session,
                  not an alarm the page raises — colouring it red made a single
                  refused call read as something being wrong with the app. */}
              <td>{row.failures ? fmt(row.failures) : "—"}</td>
              <td>
                <input
                  className="am-usage-rate"
                  type="number"
                  min="0"
                  step="0.01"
                  inputMode="decimal"
                  aria-label={`Dollars per million output tokens for ${row.model}`}
                  value={rateFor(row.model)}
                  onChange={(e) => onRate(row.model, e.target.value)}
                />
              </td>
              <td>{estimate === null ? "—" : money(estimate)}</td>
            </tr>
          );
        })}
      </tbody>
      {usage.models.length > 1 && (
        <tfoot>
          <tr>
            <td colSpan={7}>Estimated total</td>
            <td>{total === null ? "—" : money(total)}</td>
          </tr>
        </tfoot>
      )}
    </table>
  );
}

/** One tier's line under the tiles. Both tiers are always shown, including the
 *  one at zero: "you have never run a model locally" is a true and useful thing
 *  for THIS page to say, and an absent row would just read as a rendering gap. */
function TierLine({ tier, counts }: { tier: AiUsageTier; counts: AiUsageCounts }) {
  const idle = counts.completions === 0 && counts.failures === 0;
  return (
    <div className="am-usage-tier-line">
      <b>{tier === "claude" ? "Claude" : "Local models"}</b>{" "}
      {idle ? (
        <span className="am-usage-muted">nothing yet</span>
      ) : (
        <>
          {fmt(counts.output_tokens)} generated · {fmt(counts.completions)}{" "}
          {counts.completions === 1 ? "completion" : "completions"}
          {counts.tokens_per_second !== null && ` · ${counts.tokens_per_second}/s`}
          {counts.failures > 0 && ` · ${fmt(counts.failures)} failed`}
        </>
      )}
    </div>
  );
}

export default function AiModelsUsage() {
  const [minutes, setMinutes] = useState<number>(15);
  const [usage, setUsage] = useState<AiUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Model id -> the rate typed for it, as the STRING the box holds: a half-typed
  // "1." or an emptied box are states a number would erase under the cursor.
  // Held here, above the table, so the five-second poll re-renders rows without
  // touching what somebody is editing.
  const [rates, setRates] = useState<Record<string, string>>({});
  const setRate = (model: string, rate: string) =>
    setRates((prev) => ({ ...prev, [model]: rate }));

  // Poll, and keep the LAST good answer on a failure. A metrics read that lost
  // a race with a restarting server should leave the graph as it was, exactly
  // as the runtime poll does (aiRuntime.ts) — blanking a chart because one
  // request in a hundred failed is worse than a chart one tick behind.
  //
  // `setTimeout` after each response rather than an interval: the request is
  // cheap but it is not instant, and an interval shorter than a slow answer
  // stacks requests on a machine that is already busy generating.
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const controller = new AbortController();
    const tick = async () => {
      try {
        const next = await getAiUsage(minutes, { signal: controller.signal });
        if (!alive) return;
        setUsage(next);
        setError(null);
      } catch (e) {
        if (!alive || controller.signal.aborted) return;
        setError((e as Error).message);
      }
      if (alive) timer = window.setTimeout(tick, POLL_MS);
    };
    void tick();
    return () => {
      alive = false;
      controller.abort();
      window.clearTimeout(timer);
    };
    // The window is part of the request, so changing it restarts the poll. The
    // previous answer stays on screen until the new one lands — one frame of
    // the old window is better than a blank card.
  }, [minutes]);

  return (
    <div className="am-usage">
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!usage && !error && <SkeletonLines rows={3} label="Loading usage" />}
      {usage && (
        <>
          <div className="cc-mdcard am-usage-card">
            <div className="am-usage-head">
              <div className="am-usage-headline">
                <b>{fmt(usage.window.output_tokens)}</b> tokens generated
                <span className="am-usage-sub">
                  {" "}
                  in the last {usage.window_minutes} min ·{" "}
                  {fmt(usage.window.completions)}{" "}
                  {usage.window.completions === 1 ? "completion" : "completions"}
                  {usage.window.tokens_per_second !== null &&
                    ` · ${usage.window.tokens_per_second} tok/s`}
                </span>
                {/* Beside the headline, not in a corner: a window in which
                    calls failed is worth seeing without going looking. In the
                    caption's own colour, though — a failed call is a fact about
                    the session, not an alarm this page raises, and red made one
                    refused call read as the app being broken. */}
                {usage.window.failures > 0 && (
                  <span className="am-usage-sub"> · {fmt(usage.window.failures)} failed</span>
                )}
              </div>
              {/* Same segmented-control vocabulary as the page's tab strip,
                  because it is the same kind of control — one of a few, always
                  one chosen. Not in the page head with the tabs: it belongs to
                  the graph, and a second strip up there would read as four more
                  tabs. */}
              <div className="am-tabs am-usage-ranges" role="group" aria-label="Time range">
                {RANGES.map((r) => (
                  <button
                    key={r}
                    type="button"
                    className={"am-tab" + (minutes === r ? " active" : "")}
                    aria-pressed={minutes === r}
                    onClick={() => setMinutes(r)}
                  >
                    {r}m
                  </button>
                ))}
              </div>
            </div>
            <UsageChart usage={usage} />
          </div>

          {/* Failures count as "something happened": a process whose every call
              timed out has an empty graph and a great deal to explain, and the
              empty state would swallow exactly that. */}
          {usage.totals.completions === 0 && usage.totals.failures === 0 ? (
            // Not "no results": nothing has been asked of a model yet, and the
            // sentence has to say what would make a bar appear — otherwise an
            // empty graph reads as a broken one.
            <div className="cc-empty am-usage-empty">
              <p>Nothing generated yet.</p>
              <p>
                Every <code>fused.ai()</code> call a page makes — and the app&apos;s own AI
                features — is counted here while the server runs.
              </p>
            </div>
          ) : (
            <>
              <h2 className="cc-heading am-usage-heading">Since the server started</h2>
              <div className="am-usage-tiles">
                <div className="cc-mdcard am-usage-tile">
                  <div className="am-usage-tile-value">{fmt(usage.totals.output_tokens)}</div>
                  <div className="am-usage-tile-label">tokens generated</div>
                </div>
                <div className="cc-mdcard am-usage-tile">
                  <div className="am-usage-tile-value">
                    {usage.totals.input_tokens === null ? "—" : fmt(usage.totals.input_tokens)}
                  </div>
                  <div className="am-usage-tile-label">tokens read</div>
                </div>
                <div className="cc-mdcard am-usage-tile">
                  <div className="am-usage-tile-value">{fmt(usage.totals.completions)}</div>
                  <div className="am-usage-tile-label">completions</div>
                </div>
                <div className="cc-mdcard am-usage-tile">
                  {/* A zero here is a REAL answer — unlike a missing input
                      count — so it is printed. In the same weight as every
                      other tile: this counts calls that produced no text, which
                      is ordinary (a Stop, a model still downloading), and error
                      styling would make the tile shout on a healthy session. */}
                  <div className="am-usage-tile-value">{fmt(usage.totals.failures)}</div>
                  <div className="am-usage-tile-label">failed</div>
                </div>
                <div className="cc-mdcard am-usage-tile">
                  <div className="am-usage-tile-value">
                    {usage.totals.tokens_per_second === null
                      ? "—"
                      : fmt(usage.totals.tokens_per_second)}
                  </div>
                  <div className="am-usage-tile-label">tokens/sec, averaged</div>
                </div>
                <div className="cc-mdcard am-usage-tile">
                  <div className="am-usage-tile-value">
                    {usage.totals.seconds === null ? "—" : duration(usage.totals.seconds)}
                  </div>
                  <div className="am-usage-tile-label">spent generating</div>
                </div>
              </div>
              {/* WHICH failure, not just how many: a timeout, a missing claude
                  binary and a model still downloading send a user to three
                  different places, and only the type says which. */}
              {usage.failure_types.length > 0 && (
                <p className="am-usage-note am-usage-failtypes">
                  Failures:{" "}
                  {usage.failure_types
                    .map((f) => `${f.count} × ${f.type}`)
                    .join(" · ")}
                </p>
              )}
              <div className="am-usage-tierlines">
                <TierLine tier="claude" counts={usage.tiers.claude} />
                <TierLine tier="local" counts={usage.tiers.local} />
              </div>
              <UsageModels usage={usage} rates={rates} onRate={setRate} />
              {/* The assumption, written down beside the column that makes it.
                  Nobody's real bill is output-only at a flat rate, and the
                  figure is worth having anyway — as long as the page says which
                  arithmetic it did. */}
              <p className="am-usage-note">
                Estimated cost only: tokens generated × the rate you set, per model. Input
                tokens are not counted, and nothing here is a bill — set each rate to what
                your provider charges to make the estimate yours.
              </p>
            </>
          )}

          {/* The disclaimer is the point, not the small print: these are not
              billing figures and not a daily total. Two sentences and no
              mechanism — where the numbers live is this file's business, and
              what a reader needs is that they are bounded and by what. */}
          <p className="am-usage-note">
            Metrics reset when Fused Render restarts. {duration(usage.now - usage.since)} so far,
            since {clock(usage.since)}.
          </p>
        </>
      )}
    </div>
  );
}
