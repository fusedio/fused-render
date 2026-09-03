// The two rows on this page for a model that is not (yet) a repo on this disk:
// the curation's recommendation, and a Hub search result. Same `ModelRow`
// skeleton as the disk row beside them; what differs is only what a model that
// is not here CANNOT have: no Load, no delete, no "used 4h ago".
//
// The download plumbing is one implementation for both, prop for prop: `busy`
// is the three-way `downloading ∪ starting ∪ settling` union the page computes,
// the progress comes from the same `ModelProgress`, and the ✕ is the download
// manager's own cancel.
import { useEffect, useRef, useState } from "react";
import { hubModelUrl } from "./hub";
import { InfoButton } from "./ModelInfo";
import { ModelRow, modelName, type RowStatus } from "./ModelRow";
import { PARTIAL_TAG, jobFraction, type ResultDisk, type SectionRunner } from "@apps/ai_models/lib/aiModelGroups";
import { tabHref } from "@apps/ai_models/routes";
import { gateChrome } from "@apps/ai_models/lib/hubSearchView";
import { hubSizeLabel, hubSizeTitle, knownTotalSize, lookupTotalSize } from "@apps/ai_models/lib/hubSize";
import { CancelButton } from "@apps/ai_models/shared/CancelButton";
import { DownloadGlyph, ModelProgress } from "@apps/ai_models/shared/ModelProgress";
import { modelSizeHint, modelSizeLabel } from "@apps/ai_models/shared/modelSize";
import { type AiCatalogModel, type HubModel } from "@platform/lib/api";
import { timeAgo } from "@platform/lib/format";
import { type Job } from "@platform/lib/jobs";
import { navigate, navigateUrl, urlForFsPath } from "@platform/lib/router";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { StatusBadge } from "@platform/ui/flow/StatusIcon";
import { Tiny } from "@platform/ui/flow/Typography";
import { bucketText } from "@platform/ui/status-colors";
import { cn } from "@platform/lib/utils";

/** The engine row in the (i): which backend loads this on this machine. What
 *  the row CLAIMS differs by card, and only in the hover: a curated model was
 *  picked FOR this runner; a search result only passed the server's TAG filter,
 *  so `capabilityOnly` says the repo's own format is settled when the files land. */
function engineRow(runner: SectionRunner | null, capabilityOnly = false) {
  if (!runner?.shortLabel) return { label: "Engine", value: null };
  return {
    label: "Engine",
    value: runner.shortLabel,
    hint: !runner.available
      ? `This is a ${runner.shortLabel} model, and it cannot be loaded here: ${runner.reason ?? "unavailable"}.`
      : capabilityOnly
        ? `This kind of model loads in the ${runner.shortLabel} engine here — the backend chosen for the capability on the Engines tab. Whether this repo ships the weight format that engine reads is settled when the download lands.`
        : `Loads in the ${runner.shortLabel} engine — the backend chosen for this capability on the Engines tab.`,
  };
}

/** The way out of an engine that cannot serve this model here — the warning
 *  coloured verb beside a dead button, landing on the tab where the setting
 *  lives. Shared by all three rows. */
export function SwitchEngines({ why }: { why: string }) {
  return (
    <a
      className={cn("shrink-0 text-xs underline-offset-4 hover:underline", bucketText.orange)}
      href={tabHref("engines", "")}
      data-hint={why}
      aria-label={`Switch engines — ${why}`}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        navigateUrl(tabHref("engines", ""));
      }}
    >
      Switch engines
    </a>
  );
}

function runnerWhy(runner: SectionRunner | null): string {
  return `${runner?.shortLabel ?? "This model"} cannot be loaded here: ${runner?.reason ?? "no engine serves this capability on this machine"}.`;
}

export function RecommendedRow({
  model,
  runner,
  busy,
  job,
  onDownload,
  onCancel,
}: {
  model: AiCatalogModel;
  /** Which backend would load it here, from the section. */
  runner: SectionRunner | null;
  /** A pull for this model is live: reported, just clicked, or settling. */
  busy: boolean;
  job: Job | undefined;
  onDownload: () => void;
  onCancel: (job: Job) => void;
}) {
  // One figure wherever this row names a size, and it never understates
  // (`shared/modelSize`).
  const size = modelSizeHint(model.size_gb, job);
  const arriving = jobFraction(job);
  const available = !!runner?.available;
  return (
    <ModelRow
      // A recommendation is by construction a model this machine does NOT have.
      status={busy || arriving !== null ? "busy" : "none"}
      statusLabel={busy ? "Downloading" : "Not downloaded"}
      hoverNote={model.note ?? undefined}
      name={{ href: hubModelUrl(model.id), text: model.label, title: `Open ${model.id} on the Hugging Face Hub` }}
      slug={model.id}
      marked
      info={<InfoButton name={model.id} rows={[engineRow(runner)]} />}
      size={{
        // What the download WILL cost. An unmeasured size is a dash, not a guess.
        text: modelSizeLabel(model.size_gb, job),
        title:
          size === null
            ? "Nobody has recorded this one's download size yet."
            : size.approx
              ? `About ${size.text} to download`
              : `${size.text} — the size this download itself is reporting, which is more than the recorded estimate`,
      }}
      progress={busy && <ModelProgress job={job} className="min-w-0 flex-1" />}
      actions={
        busy ? (
          <CancelButton id={model.id} job={job} onCancel={onCancel} />
        ) : (
          <>
            {!available && <SwitchEngines why={runnerWhy(runner)} />}
            {/* Offered only where it can end in a model that runs. */}
            <Button
              size="xs"
              disabled={!available}
              data-hint={
                available
                  ? `Download ${model.id}${size === null ? "" : ` (${size.approx ? "~" : ""}${size.text})`}`
                  : `${model.id} cannot be loaded here: ${runner?.reason ?? "no engine serves this capability on this machine"}.`
              }
              aria-label={
                available
                  ? `Download ${model.id}`
                  : `Download ${model.id} — unavailable: ${runner?.reason ?? "no engine serves this capability on this machine"}`
              }
              onClick={onDownload}
            >
              <DownloadGlyph />
              Download
            </Button>
          </>
        )
      }
    />
  );
}

function count(n: number | null): string | null {
  if (n === null || n === undefined) return null;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
}

/** One Hub search result: the same row, for a model the curation never named.
 *  Three things it has that a recommendation does not: the JOIN against this
 *  machine's own listing, the SIZE (asked of the Hub lazily when the reply had
 *  none), and the GATE (named, with the way through it, D316). */
export function HubResultRow({
  model,
  curated,
  runner,
  disk,
  authenticated,
  busy,
  job,
  onDownload,
  onCancel,
}: {
  model: HubModel;
  curated: boolean;
  runner: SectionRunner | null;
  /** What this machine already has of it, from the page's own walk. */
  disk: ResultDisk;
  /** Whether this machine holds a Hub token. */
  authenticated: boolean;
  busy: boolean;
  job: Job | undefined;
  /** Starts — or, on a partial, RESUMES — the pull. */
  onDownload: () => void;
  onCancel: (job: Job) => void;
}) {
  // The FALLBACK total, for a repo the Hub's dtype map could not measure: only a
  // row with no estimate asks at all, and it waits until it is on screen.
  const row = useRef<HTMLDivElement>(null);
  const wantsTotal = !model.estimatedSize;
  const [total, setTotal] = useState<number | null>((wantsTotal ? knownTotalSize(model.id) : null) ?? null);
  useEffect(() => {
    if (!wantsTotal) return;
    const known = knownTotalSize(model.id);
    if (known !== undefined) {
      setTotal(known);
      return;
    }
    const el = row.current;
    if (!el) return;
    let alive = true;
    // One request per visit to the viewport; cleared when the row leaves view
    // so a FAILED ask can be retried; a row whose ask succeeded stops observing.
    let asking = false;
    const io = new IntersectionObserver((entries) => {
      if (!entries.some((e) => e.isIntersecting)) {
        asking = false;
        return;
      }
      if (asking) return;
      asking = true;
      lookupTotalSize(model.id).then((bytes) => {
        if (!alive) return;
        setTotal(bytes);
        if (knownTotalSize(model.id) !== undefined) io.disconnect();
      });
    });
    io.observe(el);
    return () => {
      alive = false;
      io.disconnect();
    };
  }, [model.id, wantsTotal]);

  // The Hub's own measurement, deliberately NOT replaced by a running pull's
  // total: the size SORT ranks these rows by exactly what this cell shows.
  const size = hubSizeLabel(model, total);
  const gate = disk.state === "downloaded" ? null : gateChrome(model.gated, authenticated);
  const dl = count(model.downloads);
  const likes = count(model.likes);
  const updatedAt = model.updated ? Date.parse(model.updated) : NaN;
  const updated = Number.isFinite(updatedAt) ? timeAgo(updatedAt / 1000) : null;
  // A runner the catalog does not name is not a refusal here (the server
  // already dropped every row no registered runner serves, D313).
  const loadable = !runner || runner.available;
  const arriving = jobFraction(job);
  const status: RowStatus =
    arriving !== null || busy
      ? "busy"
      : disk.state === "downloaded"
        ? "have"
        : disk.state === "partial"
          ? "partial"
          : "none";
  const meta = [dl ? `${dl} downloads` : null, likes ? `${likes} likes` : null, updated ? `updated ${updated}` : null]
    .filter(Boolean)
    .join(" · ");

  return (
    <ModelRow
      status={status}
      statusLabel={
        status === "busy"
          ? "Downloading"
          : status === "have"
            ? "Downloaded"
            : status === "partial"
              ? PARTIAL_TAG
              : "Not downloaded"
      }
      rowRef={row}
      name={{ href: model.url, text: modelName(model.id), title: `Open ${model.id} on the Hub` }}
      slug={model.id}
      marked={curated}
      info={<InfoButton name={model.id} rows={[engineRow(runner, true)]} />}
      badges={
        <>
          {disk.state === "downloaded" && !busy && (
            <span data-hint={`${model.id} is already on this machine`}>
              <StatusBadge status="installed">downloaded</StatusBadge>
            </span>
          )}
          {/* The gate, named, with the whole of what to do about it on hover.
              It decides the action too — see the actions. */}
          {gate && (
            <Badge variant="outline" data-hint={gate.title}>
              {gate.pill}
            </Badge>
          )}
          {/* STATE rather than identity (D424): a half-fetched snapshot is what
              makes Download mean "resume". */}
          {disk.state === "partial" && (
            <Badge
              variant="outline"
              tabIndex={0}
              aria-label={`${PARTIAL_TAG} — Download picks this up from the bytes already here.`}
              data-hint={
                `${model.id} is a download that did not finish. Download picks it up from the ` +
                "bytes already here rather than starting over; the Local view's trash discards them."
              }
            >
              {PARTIAL_TAG}
            </Badge>
          )}
        </>
      }
      size={{ text: size ?? "—", title: hubSizeTitle(model, total) }}
      meta={meta ? <Tiny className="truncate">{meta}</Tiny> : null}
      progress={busy && <ModelProgress job={job} className="min-w-0 flex-1" />}
      actions={
        busy ? (
          <CancelButton id={model.id} job={job} onCancel={onCancel} />
        ) : (
          <>
            {/* The copy we already hold, opened where it lives — only for a
                COMPLETE download. */}
            {disk.path && (
              <Button
                variant="outline"
                size="xs"
                nativeButton={false}
                render={
                  <a
                    href={urlForFsPath(disk.path, "?_mode=model_card")}
                    data-hint={`Explore ${model.id} here — ${disk.path}`}
                    onClick={(e) => {
                      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
                        return;
                      e.preventDefault();
                      navigate(disk.path!, { isDir: true, mode: "model_card" });
                    }}
                  />
                }
              >
                Explore
              </Button>
            )}
            {/* A gate this machine cannot open gets the way to open it instead
                of a button that cannot start. */}
            {gate?.action && (
              <Button
                variant="outline"
                size="xs"
                nativeButton={false}
                render={<a href={model.url} target="_blank" rel="noopener noreferrer" data-hint={gate.title} />}
              >
                {gate.action}
              </Button>
            )}
            {!loadable && <SwitchEngines why={runnerWhy(runner)} />}
            {/* Nothing while the walk has not answered, and nothing on a copy we
                already have. */}
            {(disk.state === "absent" || disk.state === "partial") && (!gate || gate.canDownload) && (
              <Button
                size="xs"
                disabled={!loadable}
                data-hint={
                  !loadable
                    ? `${model.id} cannot be loaded here: ${runner?.reason ?? "unavailable"}.`
                    : disk.state === "partial"
                      ? `Resume downloading ${model.id} from the bytes already here`
                      : `Download ${model.id}${size ? ` (${size})` : ""}`
                }
                aria-label={disk.state === "partial" ? `Resume downloading ${model.id}` : `Download ${model.id}`}
                onClick={onDownload}
              >
                <DownloadGlyph />
                Download
              </Button>
            )}
          </>
        )
      }
    />
  );
}
