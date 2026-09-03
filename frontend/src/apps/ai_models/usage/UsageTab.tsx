// The Usage tab of /ai-models (SPEC AI-12) — how many tokens `fused.ai` has
// generated in this server process, and when it generated them.
//
// Everything here is **this process's own memory** (the server counts into a
// ring, writes nothing to disk, and forgets on restart), which is why the
// footer states when counting began rather than letting a number that resets at
// every app launch pass for a daily total.
//
// The page says INPUT TOKENS and OUTPUT TOKENS, in those words, everywhere —
// the API's own `usage` names (RH-11). Output is the figure the graph draws,
// because it is the one both tiers always report; input is "—" where nobody
// reported it (SPEC AI-3). Failures are counted BY KIND only — one number over
// several unrelated conditions is a number a reader cannot act on. The one
// number here that is NOT measured is the ESTIMATED COST, labelled that way
// everywhere it appears: $ per million output tokens, editable per model.
import { useEffect, useState } from "react";
import {
  getAiUsage,
  type AiUsage,
  type AiUsageBucket,
  type AiUsageCounts,
  type AiUsageTier,
} from "@platform/lib/api";
import { Badge } from "@platform/shadcn/ui/badge";
import { Card, CardContent } from "@platform/shadcn/ui/card";
import { Input } from "@platform/shadcn/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@platform/shadcn/ui/table";
import { ToggleGroup, ToggleGroupItem } from "@platform/shadcn/ui/toggle-group";
import { MetricGrid, Muted, SectionTitle, Stat, Tiny } from "@platform/ui/flow/Typography";
import { bucketFill } from "@platform/ui/status-colors";
import { cn } from "@platform/lib/utils";
import { ErrorNote } from "@apps/ai_models/shared/ErrorNote";
import { Loading } from "@apps/ai_models/shared/Loading";

// The windows the graph offers — three different questions, and the longest is
// the server's whole retention (AI-12).
const RANGES = [5, 15, 60] as const;

// Half a bucket: the newest column is always partial, so refreshing at the
// bucket width would leave it visibly stale for most of its life.
const POLL_MS = 5000;

const fmt = (n: number) => n.toLocaleString();

// $ per MILLION output tokens, as the STRING the box holds. "1.00" says the
// field takes cents where a bare "1" invites the reader to wonder.
const DEFAULT_RATE = "1.00";
const PER = 1_000_000;

/** `output_tokens` at `rate` per million, as dollars — or null when the rate
 *  box has been emptied or typed into something that is not a number. */
function cost(outputTokens: number, rate: string): number | null {
  const value = Number(rate);
  if (rate.trim() === "" || !Number.isFinite(value) || value < 0) return null;
  return (outputTokens / PER) * value;
}

/** Money, at a precision that does not round a real cost to nothing. */
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

/** "2h 14m", the length of the window these totals cover. */
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
    parts.push(`${fmt(bucket.output_tokens)} output`);
    if (bucket.input_tokens !== null) parts.push(`${fmt(bucket.input_tokens)} input`);
    parts.push(bucket.completions === 1 ? "1 completion" : `${bucket.completions} completions`);
  }
  if (bucket.failures) parts.push(`${bucket.failures} failed`);
  return `${when} · ${parts.length ? parts.join(" · ") : "idle"}`;
}

/** The graph: one column per bucket, height by tokens generated.
 *
 *  Bars, not a line: the series is a COUNT PER INTERVAL, and a line between two
 *  spikes draws a slope through minutes in which nothing happened. Columns are
 *  laid out right-to-left from `now` at a fixed width, so a young process fills
 *  the right-hand side and leaves the past as an explicit void. */
function UsageChart({ usage }: { usage: AiUsage }) {
  const columns = Math.max(1, Math.round((usage.window_minutes * 60) / usage.bucket_seconds));
  const peak = usage.buckets.reduce((max, b) => Math.max(max, b.output_tokens), 0);
  const last = usage.buckets.length - 1;
  // No gap once the columns are thinner than a few pixels: a 1px gutter
  // between 360 bars is most of the graph.
  const gap = columns > 90 ? "gap-0" : "gap-px";
  const missing = Math.max(0, columns - usage.buckets.length);

  return (
    <div className="flex flex-col gap-1">
      <div
        className={cn("flex h-36 items-end", gap)}
        role="img"
        aria-label={`${fmt(usage.window.output_tokens)} output tokens in the last ${usage.window_minutes} minutes`}
      >
        {missing > 0 && (
          <div
            className="h-full self-stretch bg-muted/30"
            style={{ flex: `0 0 ${(missing / columns) * 100}%` }}
            title="Before this server started counting"
          />
        )}
        {usage.buckets.map((bucket, i) => (
          <div
            key={bucket.t}
            className="relative flex h-full flex-1 flex-col justify-end"
            title={barTitle(bucket, usage.bucket_seconds)}
          >
            {/* A period in which calls FAILED is otherwise drawn as quiet: a
                tick at the baseline, under whatever the bar did. */}
            {bucket.failures > 0 && (
              <div className={cn("absolute inset-x-0 bottom-0 h-0.5", bucketFill.red)} />
            )}
            <div
              // The newest column is still filling, so it is drawn faint.
              className={cn("w-full bg-chart-1", i === last && "opacity-50")}
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
      <div className="flex justify-between border-t border-border pt-1 text-xs text-muted-foreground tabular-nums">
        {/* The server's own clock on both ends (`now`, not Date.now()). */}
        <span>{clock(usage.now - usage.window_minutes * 60)}</span>
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

/** Per model, biggest generator first — the server's order, kept. Speed and
 *  rate are per-MODEL columns because that is the level they mean something at. */
function UsageModels({
  usage,
  rates,
  onRate,
}: {
  usage: AiUsage;
  rates: Record<string, string>;
  onRate: (model: string, rate: string) => void;
}) {
  const rateFor = (model: string) => rates[model] ?? DEFAULT_RATE;
  const priced = usage.models
    .map((row) => cost(row.output_tokens, rateFor(row.model)))
    .filter((c): c is number => c !== null);
  const total = priced.length ? priced.reduce((a, b) => a + b, 0) : null;
  const num = "text-right tabular-nums";

  return (
    <div className="rounded-lg border border-border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Model</TableHead>
            <TableHead className="text-right">Completions</TableHead>
            <TableHead className="text-right">Input</TableHead>
            <TableHead className="text-right">Output</TableHead>
            <TableHead className="text-right">Speed</TableHead>
            {/* The unit is in the header, not in the box: a placeholder
                disappears the moment somebody types. */}
            <TableHead className="text-right">$/M output for est. cost</TableHead>
            <TableHead className="text-right">Est. cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {usage.models.map((row) => {
            const estimate = cost(row.output_tokens, rateFor(row.model));
            return (
              <TableRow key={row.model}>
                <TableCell className="max-w-[28rem] truncate font-mono text-xs" title={row.model}>
                  {/* The tier as a chip: readable off the id (a slash means a
                      Hub repo, AI-1). The overflow row gets no chip. */}
                  {row.tier !== null && (
                    <Badge variant="outline" className="mr-2 font-sans">
                      {TIER_LABEL[row.tier]}
                    </Badge>
                  )}
                  {row.model}
                </TableCell>
                <TableCell className={num}>{fmt(row.completions)}</TableCell>
                {/* An em dash, never a 0: an absence, not an answer. */}
                <TableCell className={num}>{row.input_tokens === null ? "—" : fmt(row.input_tokens)}</TableCell>
                <TableCell className={num}>{fmt(row.output_tokens)}</TableCell>
                <TableCell className={num}>
                  {row.tokens_per_second === null ? "—" : `${row.tokens_per_second}/s`}
                </TableCell>
                <TableCell className={num}>
                  <Input
                    type="number"
                    min="0"
                    step="0.01"
                    inputMode="decimal"
                    className="ml-auto h-7 w-24 text-right"
                    aria-label={`Dollars per million output tokens for ${row.model}`}
                    value={rateFor(row.model)}
                    onChange={(e) => onRate(row.model, e.target.value)}
                  />
                </TableCell>
                <TableCell className={num}>{estimate === null ? "—" : money(estimate)}</TableCell>
              </TableRow>
            );
          })}
        </TableBody>
        {usage.models.length > 1 && (
          <TableFooter>
            <TableRow>
              <TableCell colSpan={6}>Estimated total</TableCell>
              <TableCell className={num}>{total === null ? "—" : money(total)}</TableCell>
            </TableRow>
          </TableFooter>
        )}
      </Table>
    </div>
  );
}

/** One tier's line. Both tiers are always shown, including the one at zero:
 *  "you have never run a model locally" is a true and useful thing to say. */
function TierLine({ tier, counts }: { tier: AiUsageTier; counts: AiUsageCounts }) {
  return (
    <p className="text-sm">
      <b className="font-medium">{tier === "claude" ? "Claude" : "Local models"}</b>{" "}
      {counts.completions === 0 ? (
        // "nothing yet" means never asked; a tier whose calls all failed WAS
        // asked, and saying "nothing yet" would contradict the kinds line.
        <span className="text-muted-foreground">
          {counts.failures > 0 ? "no completions" : "nothing yet"}
        </span>
      ) : (
        <span className="text-muted-foreground">
          {fmt(counts.output_tokens)} output tokens · {fmt(counts.completions)}{" "}
          {counts.completions === 1 ? "completion" : "completions"}
          {counts.tokens_per_second !== null && ` · ${counts.tokens_per_second}/s`}
        </span>
      )}
    </p>
  );
}

function Tile({ value, label }: { value: string; label: string }) {
  return (
    <Card size="sm">
      <CardContent className="flex flex-col gap-1">
        <Stat>{value}</Stat>
        <Tiny>{label}</Tiny>
      </CardContent>
    </Card>
  );
}

export default function UsageTab() {
  const [minutes, setMinutes] = useState<number>(15);
  const [usage, setUsage] = useState<AiUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Model id -> the rate typed for it, held above the table so the poll
  // re-renders rows without touching what somebody is editing.
  const [rates, setRates] = useState<Record<string, string>>({});
  const setRate = (model: string, rate: string) => setRates((prev) => ({ ...prev, [model]: rate }));

  // Poll, and keep the LAST good answer on a failure. `setTimeout` after each
  // response rather than an interval, so a slow answer never stacks requests.
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
  }, [minutes]);

  return (
    <div className="flex flex-col gap-6">
      {error && <ErrorNote>{error}</ErrorNote>}
      {!usage && !error && <Loading rows={3} label="Loading usage" />}
      {usage && (
        <>
          <Card>
            <CardContent className="flex flex-col gap-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <Stat>{fmt(usage.window.output_tokens)}</Stat>
                  <span className="ml-1.5 text-sm">output tokens</span>
                  <Muted>
                    in the last {usage.window_minutes} min · {fmt(usage.window.completions)}{" "}
                    {usage.window.completions === 1 ? "completion" : "completions"}
                    {usage.window.tokens_per_second !== null && ` · ${usage.window.tokens_per_second} tok/s`}
                  </Muted>
                </div>
                {/* The window picker belongs to the graph, not the page head. */}
                <ToggleGroup
                  variant="outline"
                  size="sm"
                  spacing={0}
                  aria-label="Time range"
                  value={[String(minutes)]}
                  onValueChange={(v) => {
                    const next = Number(v[0]);
                    if (RANGES.includes(next as (typeof RANGES)[number])) setMinutes(next);
                  }}
                >
                  {RANGES.map((r) => (
                    <ToggleGroupItem key={r} value={String(r)} aria-pressed={minutes === r}>
                      {r}m
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
              </div>
              <UsageChart usage={usage} />
            </CardContent>
          </Card>

          {/* Failures count as "something happened": a process whose every call
              timed out has an empty graph and a great deal to explain. */}
          {usage.totals.completions === 0 && usage.totals.failures === 0 ? (
            <div className="py-6 text-center text-sm text-muted-foreground">
              <p>No output tokens yet.</p>
              <p>
                Every <code className="font-mono text-xs">fused.ai.text()</code> call a page makes — and the
                app&apos;s own AI features — is counted here while the server runs.
              </p>
            </div>
          ) : (
            <>
              <SectionTitle>Since the server started</SectionTitle>
              <MetricGrid className="xl:grid-cols-5">
                <Tile value={fmt(usage.totals.output_tokens)} label="output tokens" />
                <Tile
                  value={usage.totals.input_tokens === null ? "—" : fmt(usage.totals.input_tokens)}
                  label="input tokens"
                />
                <Tile value={fmt(usage.totals.completions)} label="completions" />
                <Tile
                  value={usage.totals.tokens_per_second === null ? "—" : fmt(usage.totals.tokens_per_second)}
                  label="tokens/sec, averaged"
                />
                <Tile
                  value={usage.totals.seconds === null ? "—" : duration(usage.totals.seconds)}
                  label="spent generating"
                />
              </MetricGrid>
              {/* WHICH failure, not just how many. */}
              {usage.failure_types.length > 0 && (
                <Muted>
                  Failures: {usage.failure_types.map((f) => `${f.count} × ${f.type}`).join(" · ")}
                </Muted>
              )}
              <div className="flex flex-col gap-1">
                <TierLine tier="claude" counts={usage.tiers.claude} />
                <TierLine tier="local" counts={usage.tiers.local} />
              </div>
              <UsageModels usage={usage} rates={rates} onRate={setRate} />
              <Tiny>
                Estimated cost only: output tokens × the rate you set, per model. Input tokens are not
                counted, and nothing here is a bill — set each rate to what your provider charges to make
                the estimate yours.
              </Tiny>
            </>
          )}

          <Tiny>
            Metrics reset when Fused Render restarts. {duration(usage.now - usage.since)} so far, since{" "}
            {clock(usage.since)}.
          </Tiny>
        </>
      )}
    </div>
  );
}
