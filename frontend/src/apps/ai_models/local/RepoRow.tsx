// One cached repo, as a row: what it is, what it costs, what it is doing right
// now, and the three controls that act on it (Load/Unload, Try, Delete) — plus
// the (i) that opens everything else.
//
// Nothing about a row is page state: everything it draws arrives as a prop,
// and everything it does leaves as a callback, which is what lets the page keep
// ONE call site for it (`row()` in LocalTab) across two differently-grouped
// sections.
import { Trash2Icon } from "lucide-react";
import { ModelInfoButton } from "./ModelInfo";
import { ModelRow, modelName, type RowStatus } from "./ModelRow";
import { SwitchEngines } from "./RecommendedRow";
import { hubUrl } from "./hub";
import { DownloadGlyph, ModelProgress } from "@apps/ai_models/shared/ModelProgress";
import { unloadCountdown } from "@apps/ai_models/lib/engines";
import { type AiLoadedModel, type AiModelRepo } from "@platform/lib/api";
import { isRunning, type Job } from "@platform/lib/jobs";
import { formatSize, formatMtimeFull, timeAgo } from "@platform/lib/format";
import { navigateUrl } from "@platform/lib/router";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { StatusBadge } from "@platform/ui/flow/StatusIcon";
import { Tiny } from "@platform/ui/flow/Typography";
import { bucketText } from "@platform/ui/status-colors";
import { cn } from "@platform/lib/utils";
import {
  PARTIAL_TAG,
  emptyShell,
  jobFraction,
  loadRefusalShort,
  partialNote,
  resumable,
} from "@apps/ai_models/lib/aiModelGroups";
import { tabHref } from "@apps/ai_models/routes";

/** Where the Try button goes: the playground, with this model selected and
 *  nothing else carried over. */
function tryHref(repo: AiModelRepo): string {
  return tabHref("playground", "?model=" + encodeURIComponent(repo.id));
}

/** A state tag on the row's head: `part of X` (why there is no Load) or
 *  `partly downloaded` (why the button reads "Continue downloading"). Focusable
 *  and hinted, because the hover carries prose nothing else repeats. */
export function StateTag({ label, ariaLabel, hint }: { label: string; ariaLabel: string; hint: string }) {
  return (
    <Badge variant="outline" tabIndex={0} aria-label={ariaLabel} data-hint={hint} className="shrink-0">
      {label}
    </Badge>
  );
}

// The loud one. A loaded model is the only state on this page that costs
// something continuously — gigabytes of RAM, right now — so it has to be
// findable by SWEEPING the list. Green, filled, beside the name.
function LoadedBadge({ loaded }: { loaded: AiLoadedModel }) {
  return (
    <span
      data-hint={
        `${loaded.model} is loaded in memory` +
        (loaded.residentBytes ? ` — ${formatSize(loaded.residentBytes)} resident` : "")
      }
    >
      <StatusBadge status="installed">Loaded</StatusBadge>
    </span>
  );
}

/** Where a loaded model actually ended up, and — on a CPU — what that means.
 *  A healthy 4B model on the processor answers at a few words a second, and
 *  that is worth explaining rather than leaving as a mystery. */
function DeviceNote({ device }: { device: string }) {
  const cpu = device === "cpu";
  return (
    <Tiny
      className={cn("shrink-0", cpu && bucketText.orange)}
      data-hint={
        cpu
          ? "This model is running on the processor, not a graphics card — it " +
            "works, but expect a few words a second rather than an instant " +
            "answer. This machine either has no supported graphics card, or " +
            "the CPU engine was chosen deliberately — check the Engines tab " +
            "to switch to a CUDA or ROCm engine if one is available."
          : "This model is running on the graphics card."
      }
    >
      {cpu ? "on CPU — a few words a second" : `on ${device.toUpperCase()}`}
    </Tiny>
  );
}

// The live state of one model: downloading (bytes, from the job row), loading
// (no percentage — an invented bar reads as frozen), ready (with its resident
// memory and device), and error (with what went wrong).
function RuntimeChip({
  loaded,
  job,
  stop,
}: {
  loaded?: AiLoadedModel;
  job?: Job;
  stop?: { label: string; onStop: () => void };
}) {
  if (loaded?.state === "ready") {
    if (!loaded.residentBytes && !loaded.device) return null;
    // AI-13: null whenever the idle window is disabled.
    const countdown = unloadCountdown(loaded.unloadsInSeconds);
    return (
      <span className="flex min-w-0 items-center gap-2">
        {loaded.residentBytes ? (
          <Tiny
            className="shrink-0"
            data-hint={
              "Resident memory of the model's process. Not the model's size: it " +
              "counts shared pages too and moves while it generates."
            }
          >
            {formatSize(loaded.residentBytes)} in memory
            {countdown ? ` — ${countdown}` : ""}
          </Tiny>
        ) : null}
        {loaded.device && <DeviceNote device={loaded.device} />}
      </span>
    );
  }
  if (loaded?.state === "error") {
    return (
      <Tiny className={cn("truncate", bucketText.red)} data-hint={loaded.error ?? undefined}>
        Failed to load{loaded.error ? ` — ${loaded.error}` : ""}
      </Tiny>
    );
  }
  return <ModelProgress detail={loaded?.detail} job={job} stop={stop} className="min-w-0 flex-1" />;
}

export function RepoRow({
  repo,
  label,
  curated,
  loaded,
  job,
  busy,
  fetching,
  refusal,
  resumeCapability,
  onDeleteRepo,
  onDownload,
  onCancel,
  onLoad,
  onUnload,
}: {
  repo: AiModelRepo;
  /** The curated display name for the head (AI-2c): a human name is a curated
   *  field, never one mechanically derived from a repo id. `undefined` for a
   *  repo the curation does not name, in which case the head falls back to
   *  `modelName(repo.id)`. */
  label?: string;
  /** Whether the curation names this exact repo id — the seal beside the name. */
  curated: boolean;
  /** The resident worker for this repo, when it is one. */
  loaded: AiLoadedModel | undefined;
  /** Its download-manager row, while a bring-up is running. */
  job: Job | undefined;
  busy: boolean;
  /** True while a weights-only fetch for this repo is in flight. */
  fetching: boolean;
  /** Why Load is refused for this repo, or null (`aiModelGroups.loadRefusal`).
   *  It DISABLES the button and becomes its hint; it never removes it. */
  refusal: string | null;
  /** For a `repo.partial` row: which capability a RESUME would be filed under,
   *  or null when nothing on this page can say (D437). */
  resumeCapability: string | null;
  onDeleteRepo: () => void;
  /** Resume the unfinished download (D275). */
  onDownload: () => void;
  /** Stop the pull that is running for this repo. */
  onCancel: (job: Job) => void;
  onLoad: () => void;
  onUnload: () => void;
}) {
  const when = timeAgo(repo.lastUsed ?? repo.mtime);
  const live = loaded?.state === "ready";
  // WEIGHTS ARE COMING INTO MEMORY RIGHT NOW — not resident, not failed.
  const loading = !!loaded && !live && loaded.state !== "error";
  // Why Delete is not offered right now, or "". The server refuses these too
  // (`_require_not_in_use`); this is so the answer arrives before the dialog.
  const inUse = live
    ? "in memory — unload it first"
    : loaded
      ? `being loaded (${loaded.state})`
      : fetching
        ? "being downloaded"
        : "";
  const pulling = jobFraction(job) !== null;
  const partial = resumable(repo);
  // The leading icon says which of the disk states this row is in (D436): a
  // pull RUNNING is yellow and pulsing; idle, a half-fetched repo is orange.
  const status: RowStatus =
    loaded?.state === "error"
      ? "error"
      : pulling || loading
        ? "busy"
        : partial
          ? "partial"
          : "have";
  const statusLabel =
    status === "error"
      ? "Failed to load"
      : status === "busy"
        ? loading
          ? "Loading"
          : "Downloading"
        : status === "partial"
          ? PARTIAL_TAG
          : live
            ? "Loaded"
            : "Downloaded";
  // Both kinds of busy get a stop, and they are DIFFERENT calls: a download is
  // a job the manager cancels, a load is a worker process, and what stops one
  // of those is `unload`. A DOWNLOAD FIRST, when there is one.
  const stoppableJob =
    job && isRunning(job) && job.cancellable && !job.cancel_requested && !job.stalled ? job : null;
  const stop = stoppableJob
    ? { label: `Stop downloading ${repo.id}`, onStop: () => onCancel(stoppableJob) }
    : loading
      ? { label: `Stop loading ${repo.id}`, onStop: onUnload }
      : undefined;

  return (
    <ModelRow
      status={status}
      statusLabel={statusLabel}
      name={{ href: hubUrl(repo), text: label ?? modelName(repo.id), title: `Open ${repo.id} on the Hugging Face Hub` }}
      marked={curated}
      badges={
        <>
          {loaded?.state === "ready" && <LoadedBadge loaded={loaded} />}
          {/* Only when the kind is NOT the one the page already promises: a
              dataset or a Space in the same cache is not loadable. */}
          {repo.kind !== "model" && <Badge variant="secondary">{repo.kind}</Badge>}
          {/* THE TWO TAGS THAT ARE STATE, not identity: `part of X` (why there
              is no Load) and `partly downloaded` (why the button reads
              "Continue downloading" — it OUTRANKS every reading of the engine,
              D424). */}
          {repo.component ? (
            <StateTag
              label={`part of ${repo.component.owner}`}
              ariaLabel={`Part of ${repo.component.owner} — ${repo.component.what}`}
              hint={repo.component.what}
            />
          ) : partial ? (
            <StateTag label={PARTIAL_TAG} ariaLabel={`${PARTIAL_TAG} — ${partialNote(repo)}`} hint={partialNote(repo)} />
          ) : null}
        </>
      }
      slug={repo.id}
      size={{
        text: formatSize(repo.size),
        title: repo.mtime ? `Last changed ${formatMtimeFull(repo.mtime)}` : undefined,
      }}
      info={<ModelInfoButton repo={repo} />}
      // ONE fact, not five: how long since anything read this.
      meta={when ? <Tiny className="shrink-0">used {when}</Tiny> : null}
      progress={(loaded || job) && <RuntimeChip loaded={loaded} job={job} stop={stop} />}
      actions={
        <>
          {/* WHY THE LOAD BESIDE IT IS DEAD, in the strip. Two shapes: when the
              obstacle is which engine this capability is pointed at, a warning
              coloured `Switch engines` that lands on the tab where the setting
              lives; otherwise a muted phrase. A component says nothing here —
              its tag already does. Same gate as always: only where a DISABLED
              Load is drawn. */}
          {refusal && !live && !loading && !partial && (
            repo.engine && !repo.engine.available ? (
              <SwitchEngines why={refusal} />
            ) : repo.component ? null : (
              <Tiny className="max-w-56 truncate" data-hint={refusal}>
                {loadRefusalShort(repo) ?? refusal}
              </Tiny>
            )
          )}
          {live ? (
            <Button
              variant="outline"
              size="xs"
              disabled={busy}
              data-hint={`Unload ${repo.id} and give its memory back`}
              onClick={onUnload}
            >
              Unload
            </Button>
          ) : loading ? (
            <Button
              size="xs"
              disabled
              data-hint={`${repo.id} is loading into memory — the ✕ on the progress row stops it`}
              aria-label={`${repo.id} is loading`}
            >
              Loading…
            </Button>
          ) : partial && !resumeCapability ? (
            /* THE DEAD END, given a way out (D437): when the resume is
               impossible, the primary control becomes the act that IS possible.
               Same trash target, same confirm dialog. */
            <Button
              variant="destructive"
              size="xs"
              disabled={busy || !!inUse}
              data-hint={
                inUse
                  ? `Cannot delete ${repo.id}: ${inUse}`
                  : `Delete ${repo.id} — ${
                      emptyShell(repo)
                        ? "this download stopped before any of the model arrived, so there is nothing to resume"
                        : "Fused Render cannot tell what this is, so the download cannot be resumed"
                    }`
              }
              aria-label={`Delete ${repo.id} — this unfinished download cannot be resumed`}
              onClick={onDeleteRepo}
            >
              Delete
            </Button>
          ) : partial ? (
            /* "Continue downloading", not "Download" (D448): the server picks up
               from the part file on disk instead of starting over. */
            <Button
              size="xs"
              disabled={busy || fetching || !!job}
              data-hint={`Continue downloading ${repo.id} — it resumes from the ${formatSize(repo.fetchedBytes)} already here`}
              aria-label={`Continue downloading ${repo.id} — resume the unfinished download`}
              onClick={onDownload}
            >
              <DownloadGlyph />
              {fetching || job ? "Downloading…" : "Continue downloading"}
            </Button>
          ) : (
            /* Always rendered, disabled when it cannot be pressed: a control
               that vanishes teaches nothing. The reason again in the accessible
               name (WCAG 2.5.3). */
            <Button
              size="xs"
              disabled={busy || !!job || !!refusal}
              data-hint={refusal ?? `Load ${repo.id} into memory so it can answer`}
              aria-label={refusal ? `Load ${repo.id} — unavailable: ${refusal}` : `Load ${repo.id}`}
              onClick={onLoad}
            >
              {job ? "Loading…" : "Load"}
            </Button>
          )}
          {/* Into the Playground, pre-selected — only where the playground could
              actually serve it. An EXPLICIT search, not the current one: this
              link's whole job is to REPLACE whatever model the playground had. */}
          {repo.capability && !refusal && (
            <Button
              variant="outline"
              size="xs"
              nativeButton={false}
              render={
                <a
                  href={tryHref(repo)}
                  data-hint={`Try ${repo.id} in the Playground`}
                  onClick={(e) => {
                    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
                      return;
                    e.preventDefault();
                    navigateUrl(tryHref(repo));
                  }}
                />
              }
            >
              Try
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon-xs"
            className="hover:text-destructive"
            data-hint={inUse ? `Cannot delete ${repo.id}: ${inUse}` : `Delete ${repo.id}`}
            aria-label={`Delete ${repo.id}`}
            disabled={!!inUse}
            onClick={onDeleteRepo}
          >
            <Trash2Icon />
          </Button>
        </>
      }
    />
  );
}
