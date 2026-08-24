// The Playground tab: pick a local model on the left, use it on the right.
//
// Everything else on /ai-models is ABOUT models — this tab is the one that
// answers "what can this thing actually do?" by letting them do it. Every
// stage is the same API-surface shape — input, Run, the result of that run.
// The stage is chosen by the selected model's capability, so a capability
// added server-side gets a named placeholder here rather than a blank.
//
// The whole tab is ONE bordered workbench of panes (nav rail | header +
// stage + settings rail), the way shadcn's own playground reads — borders
// separate regions; cards are not stacked inside cards.
//
// The sidebar is `GET /api/ai/catalog`, verbatim (D323). Rows show the
// curated `nickname` (catalog.py) with the full label one hover away.
//
// SELECTING IS NOT LOADING. One model per capability is resident and loading
// evicts (AI-4). A click renders the stage and rewrites the URL; the weights
// move when the user acts — Download explicitly, or the first generation.
//
// The URL carries the setup, never the transcript: `model` plus each stage's
// non-default settings, written with `replaceSearch` because browsing models
// is not history the back button should replay.
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDownIcon, DownloadIcon, SparklesIcon } from "lucide-react";
import { TextStage } from "./TextStage";
import { ImageStage } from "./ImageStage";
import { VideoStage } from "./VideoStage";
import { TranscribeStage } from "./TranscribeStage";
import { EmbedStage } from "./EmbedStage";
import { modelSizeHint, modelSizeLabel } from "@apps/ai_models/shared/modelSize";
import { formatSize } from "@platform/lib/format";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { CAPABILITY_ORDER } from "@apps/ai_models/lib/aiModelGroups";
import { PLAYGROUND_GROUPS } from "./groups";
import { buildAppAnnotation, modelName } from "./appSeed";
import { capabilityIcon, unsupportedIcon } from "./capabilityIcons";
import { pickPlaygroundModel, playgroundModels } from "./pick";
import { hubModelUrl } from "@apps/ai_models/local/hub";
import { readParam, resetParams, writeParams } from "@apps/ai_models/lib/params";
import { isBusy, refreshAiRuntime, useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { cancelJob, fetchJobs, isRunning, type Job } from "@platform/lib/jobs";
import {
  downloadAiModel,
  getAiCatalog,
  type AiUnsupportedModel,
  loadAiModel,
  unloadAiModel,
  type AiCatalogCapability,
  type AiCatalogModel,
} from "@platform/lib/api";
import { useUrlVersion } from "@platform/lib/hooks";
import { navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { Alert, AlertDescription } from "@apps/ai_models/ui/alert";
import { Badge } from "@apps/ai_models/ui/badge";
import { Button } from "@apps/ai_models/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@apps/ai_models/ui/collapsible";
import { cn } from "@apps/ai_models/ui/utils";

// What the groups are called HERE: this tab names the WORK ("Text
// generation") rather than the capability id — shared with the Home strip via
// PLAYGROUND_GROUPS so one capability has one name. An unknown capability
// falls back to the shared label, so a new runner appears instead of
// vanishing.
const GROUP_LABELS: Record<string, string> = Object.fromEntries(
  PLAYGROUND_GROUPS.map((g) => [g.capability, g.label]),
);
function groupLabel(capability: string): string {
  return GROUP_LABELS[capability] ?? capabilityLabel(capability);
}

// A sidebar row's download, counting — a RING, because the slot is a square
// the width of an icon. The arc is drawn with stroke-dasharray/-dashoffset on
// a circle rotated a quarter turn back, so 0% starts at twelve o'clock. An
// unmeasured pull spins a fixed quarter-arc instead: a ring frozen at 0 reads
// as a download that has stalled, which is the one thing it is not.
const RING_R = 6.5;
const RING_C = 2 * Math.PI * RING_R;

/** How far a pull has got, 0–1, or null while nothing can divide. The job's
 *  bytes and never the runtime's: only the worker doing the fetching knows. */
function downloadFraction(job?: Job): number | null {
  if (!job || job.unit !== "bytes" || !job.total || job.done === null) return null;
  return Math.min(1, job.done / job.total);
}

function DownloadRing({ job }: { job?: Job }) {
  const measured = downloadFraction(job);
  return (
    <svg
      className={cn("size-4 -rotate-90", measured === null && "animate-spin")}
      viewBox="0 0 16 16"
      aria-hidden="true"
    >
      <circle
        className="stroke-border"
        cx="8"
        cy="8"
        r={RING_R}
        fill="none"
        strokeWidth="2"
      />
      <circle
        className="stroke-primary"
        cx="8"
        cy="8"
        r={RING_R}
        fill="none"
        strokeWidth="2"
        strokeLinecap="round"
        strokeDasharray={RING_C}
        strokeDashoffset={measured === null ? RING_C * 0.75 : RING_C * (1 - measured)}
      />
    </svg>
  );
}

type CatalogLoad =
  | { status: "loading" }
  | { status: "ok"; capabilities: AiCatalogCapability[]; unsupported: AiUnsupportedModel[] }
  | { status: "error"; message: string };

export default function PlaygroundTab() {
  const [catalog, setCatalog] = useState<CatalogLoad>({ status: "loading" });
  const [actionError, setActionError] = useState<string | null>(null);
  const runtime = useAiRuntime();
  // `useUrlVersion`, not `useNavEpoch`: the selection is written with
  // `replaceSearch`, which fires only fused:urlchange.
  const urlVersion = useUrlVersion();

  // One fetch per mount, then again when a download lands: `downloaded` on the
  // rows is a disk fact and a finished pull is the one thing here that changes
  // it. A model that LEFT the downloading list is a pull that ended.
  const [catalogEpoch, setCatalogEpoch] = useState(0);
  const downloadingKey = runtime.downloading.map((d) => d.model).sort().join(" ");
  const previousDownloads = useRef<string>("");
  useEffect(() => {
    const before = previousDownloads.current;
    previousDownloads.current = downloadingKey;
    if (before && before.split(" ").some((m) => !downloadingKey.split(" ").includes(m))) {
      setCatalogEpoch((n) => n + 1);
    }
  }, [downloadingKey]);
  useEffect(() => {
    let alive = true;
    getAiCatalog().then(
      (data) =>
        alive &&
        setCatalog({
          status: "ok",
          capabilities: data.capabilities,
          unsupported: data.unsupported ?? [],
        }),
      (e: Error) => alive && setCatalog({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
  }, [catalogEpoch]);

  // Job rows while anything is live, so the size rule can read a running
  // pull's own measured total — matched by TITLE (the supervisor sets it to
  // the model id), the same join AiModels.tsx uses.
  const anyBusy = isBusy(runtime);
  const [jobs, setJobs] = useState<Job[]>([]);
  useEffect(() => {
    if (!anyBusy) {
      setJobs([]);
      return;
    }
    let alive = true;
    const tick = () => fetchJobs().then((s) => alive && setJobs(s.jobs), () => {});
    void tick();
    const timer = window.setInterval(tick, 1000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [anyBusy]);

  const capabilities = catalog.status === "ok" ? catalog.capabilities : [];
  // Downloaded, and runnable by nothing here. Drawn rather than dropped: a
  // model that silently is not in the sidebar reads as a download that failed.
  const unsupported = catalog.status === "ok" ? catalog.unsupported : [];

  // The rail's reading order lives in CAPABILITY_ORDER (shared with the
  // Models and Benchmark tabs). A capability missing from that list still
  // draws — it sorts after the named ones, in the order the server sent it.
  // This is also what the fallback selection reads.
  const railRows = useMemo(() => {
    const rank = (c: string) => {
      const i = CAPABILITY_ORDER.indexOf(c);
      return i === -1 ? CAPABILITY_ORDER.length : i;
    };
    return [...capabilities].sort((a, b) => rank(a.capability) - rank(b.capability));
  }, [capabilities]);

  // The selection lives in the URL. An unknown or absent id falls back to the
  // top section's default silently (PT-9: a stale link opens the page, not an
  // error).
  const asked = useMemo(() => readParam("model"), [urlVersion]);
  // `?cap=` names a capability, not a model — it only steers the fallback.
  const askedCap = useMemo(() => readParam("cap"), [urlVersion]);
  // Only a row the sidebar actually draws is selectable — both rules live in
  // `pick.ts`, with the sidebar reading the same `playgroundModels` below.
  const selected = useMemo(
    () => pickPlaygroundModel(railRows, asked, askedCap),
    [railRows, asked, askedCap],
  );

  // What the URL asked for, when this machine cannot give it.
  const blockedAsk = useMemo(() => {
    if (!asked && !askedCap) return null;
    const row =
      capabilities.find((r) => r.models.some((m) => m.id === asked)) ??
      capabilities.find((r) => r.capability === askedCap);
    if (!row || row.available) return null;
    const reason = row.reason?.trim() || "no engine for it is installed.";
    return { row, reason: /[.!?]$/.test(reason) ? reason : reason + "." };
  }, [capabilities, asked, askedCap]);

  const select = (id: string) => {
    setActionError(null);
    // CHANGING CAPABILITY CLEARS THE SETTINGS: `prompt`, `steps`, `seed` and
    // `w`/`h` are spelled the same across stages and mean different things.
    // Written as "keep the model" rather than as a list of keys to drop. A
    // move WITHIN a capability keeps everything on purpose.
    const nextCapability = railRows.find((row) =>
      row.models.some((model) => model.id === id),
    )?.capability;
    if (nextCapability && selected && nextCapability !== selected.row.capability) {
      resetParams({ model: id });
      return;
    }
    // cap dies with the first explicit pick.
    writeParams({ model: id, cap: null });
  };

  const residentRow = selected
    ? runtime.loaded.find((m) => m.capability === selected.row.capability)
    : undefined;
  const selectedResident = residentRow?.model === selected?.model.id ? residentRow : undefined;
  const selectedDownloading =
    !!selected && runtime.downloading.some((d) => d.model === selected.model.id);
  const jobForSelected = selected
    ? jobs.find((j) => j.owner === "server" && j.title === selected.model.id)
    : undefined;
  // Rows by MODEL, for the sidebar's own size cells — same title match as
  // `jobForSelected`, for the same reason.
  const jobByModel = useMemo(
    () => new Map(jobs.filter((j) => j.owner === "server").map((j) => [j.title, j])),
    [jobs],
  );

  // The sidebar rows and the header share this: same call, same error
  // surface (the stage's banner — the row has no room for a sentence).
  const runDownloadFor = async (id: string, capability: string) => {
    setActionError(null);
    try {
      await downloadAiModel(id, capability);
      refreshAiRuntime();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };
  const runDownload = () => {
    if (!selected) return;
    return runDownloadFor(selected.model.id, selected.row.capability);
  };
  // The download manager's ✕: a REQUEST and not a state change — the job row
  // stays until the worker honours it.
  const runCancelDownload = async (job: Job) => {
    setActionError(null);
    try {
      await cancelJob(job.id);
    } catch (e) {
      setActionError((e as Error).message);
    }
    refreshAiRuntime();
  };
  const runLoad = async () => {
    if (!selected) return;
    setActionError(null);
    try {
      await loadAiModel(selected.model.id, selected.row.capability);
      refreshAiRuntime();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };
  const runUnload = async () => {
    if (!selected) return;
    setActionError(null);
    try {
      await unloadAiModel(selected.model.id);
      refreshAiRuntime();
    } catch (e) {
      setActionError((e as Error).message);
    }
  };

  if (catalog.status === "loading") {
    return <p className="cc-empty">Reading the model catalog…</p>;
  }
  if (catalog.status === "error") {
    return <ErrorBanner>{catalog.message}</ErrorBanner>;
  }

  // The size to name for the selected model, wherever this page names one.
  const selectedSize = selected ? modelSizeHint(selected.model.size_gb, jobForSelected) : null;
  // The fit verdict, in words, for all three answers — null draws nothing.
  // The two bad answers are TINTED and the good one is not: "tight" and "no"
  // are the only verdicts that ask the reader to do something differently.
  const fitNote = !selected
    ? null
    : selected.model.fit === "easy"
      ? {
          text: "Runs comfortably here",
          tone: "",
          title:
            "Judged against this machine's memory — the weights leave room for everything else.",
        }
      : selected.model.fit === "tight"
        ? {
            text: "Tight fit on this machine",
            tone: "text-chart-3",
            title:
              "Judged against this machine's memory — close other heavy apps while it runs.",
          }
        : selected.model.fit === "no"
          ? {
              text: "Likely too big for this machine",
              tone: "text-destructive",
              title: "Judged against this machine's memory — it may crawl or fail to load.",
            }
          : null;

  // The running pull's own figures, for the header's ring and byte line.
  const downloadedFraction = downloadFraction(jobForSelected);
  // Whether the pull can be STOPPED, by the download manager's own rule.
  const stoppable =
    jobForSelected &&
    isRunning(jobForSelected) &&
    jobForSelected.cancellable &&
    !jobForSelected.cancel_requested &&
    !jobForSelected.stalled
      ? jobForSelected
      : null;
  const downloadedBytes = !!(
    jobForSelected && jobForSelected.unit === "bytes" && jobForSelected.total &&
    jobForSelected.done !== null
  );

  // One nav row in the model rail: a quiet ghost row, not a bordered card —
  // active is the filled one, exactly how shadcn's sidebar rows read.
  const drawModelRow = (row: AiCatalogCapability) => (model: AiCatalogModel) => {
    const active = selected?.model.id === model.id;
    const downloading = runtime.downloading.some((d) => d.model === model.id);
    const name = modelName(model);
    // The row is a div-as-button, not a <button>: the Download CTA lives
    // inside it, and a button inside a button is markup browsers are free to
    // mangle.
    const job = jobByModel.get(model.id);
    const size = modelSizeHint(model.size_gb, job);
    return (
      <div
        key={model.id}
        role="button"
        tabIndex={0}
        className={cn(
          "flex h-8 cursor-pointer items-center gap-2 rounded-md px-2 text-sm outline-none transition-colors",
          "hover:bg-accent hover:text-accent-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50",
          active ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground",
        )}
        aria-pressed={active}
        onClick={() => select(model.id)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            select(model.id);
          }
        }}
        title={model.label}
      >
        {/* Live from the supervisor, not the catalog's `loaded` snapshot — a
            dot that outlives an unload is a lie. */}
        {runtime.loaded.some((m) => m.model === model.id && m.state === "ready") && (
          <span
            className="size-1.5 shrink-0 rounded-full bg-chart-2"
            title="Loaded — answering from memory"
          />
        )}
        <span className="min-w-0 flex-1 truncate">{name}</span>
        <span
          className="shrink-0 text-xs text-muted-foreground/70"
          title={
            size ? `${size.text} download — judged against this machine's memory` : undefined
          }
        >
          {modelSizeLabel(model.size_gb, job)}
        </span>
        {/* On disk = nothing to say: the CTA exists only while there is an
            action to take. */}
        {!model.downloaded && (
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="size-6 shrink-0"
            disabled={downloading}
            aria-label={downloading ? "Downloading…" : `Download ${name}`}
            title={
              downloading
                ? downloadFraction(job) !== null
                  ? `Downloading — ${Math.floor((downloadFraction(job) as number) * 100)}%`
                  : "Downloading…"
                : `Download ${name}`
            }
            onClick={(e) => {
              // Selecting too is fine; a second click must not be.
              e.stopPropagation();
              select(model.id);
              void runDownloadFor(model.id, row.capability);
            }}
          >
            {downloading ? <DownloadRing job={job} /> : <DownloadIcon />}
          </Button>
        )}
      </div>
    );
  };

  return (
    <div className="pg-scope flex min-h-0 flex-1 overflow-hidden rounded-xl border bg-background text-foreground">
      {/* -- the model rail ------------------------------------------------ */}
      <aside
        className="flex w-56 shrink-0 flex-col gap-4 overflow-y-auto border-r p-3 md:w-64"
        aria-label="Models to try"
      >
        {railRows.map((row) => {
          // The catalog's curated half, in its own smallest-first order — the
          // RECOMMENDED subset of it (D425): see `pick.ts`. The uncurated
          // repos this disk happens to hold (D323's union) are still playable
          // but drawn after the curated run.
          const offered = playgroundModels(row);
          const curated = offered.filter((m) => m.source === "curated");
          const cached = offered.filter((m) => m.source !== "curated");
          const draw = drawModelRow(row);
          return (
            <Collapsible key={row.capability} defaultOpen className="group/cap">
              <CollapsibleTrigger className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium tracking-wide text-muted-foreground uppercase hover:text-foreground">
                <span className="[&_svg]:size-3.5">{capabilityIcon(row.capability)}</span>
                <span className="flex-1 text-left">{groupLabel(row.capability)}</span>
                <ChevronDownIcon className="size-3.5 transition-transform group-data-[state=closed]/cap:-rotate-90" />
              </CollapsibleTrigger>
              <CollapsibleContent className="flex flex-col gap-0.5 pt-1">
                {!row.available && (
                  // Visible with its reason, never hidden: an absent group and
                  // a ruled-out group look identical (HF-8).
                  <p className="px-2 py-1 text-xs text-muted-foreground/80">
                    {row.reason || "Not available on this machine."}
                  </p>
                )}
                {row.available && !offered.length && (
                  // `offered`, not `row.models`: a silent empty group is the
                  // one outcome this filter must not produce.
                  <p className="px-2 py-1 text-xs text-muted-foreground/80">
                    Nothing to try here yet — the Local tab is where a first model comes from.
                  </p>
                )}
                {/* Curated first, then the uncurated repos this disk happens
                    to hold — one run of rows, no divider: which half is on
                    this disk is legible from the Download glyphs. */}
                {row.available && curated.map(draw)}
                {row.available && cached.map(draw)}
              </CollapsibleContent>
            </Collapsible>
          );
        })}

        {unsupported.length > 0 && (
          // Everything downloaded appears, and what cannot run says so.
          // Last, and collapsed by DEFAULT: it is a reference list, not a
          // menu — nothing in it is selectable.
          <Collapsible className="group/cap">
            <CollapsibleTrigger className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium tracking-wide text-muted-foreground uppercase hover:text-foreground">
              <span className="[&_svg]:size-3.5">{unsupportedIcon()}</span>
              <span className="flex-1 text-left">Not supported</span>
              <ChevronDownIcon className="size-3.5 transition-transform group-data-[state=closed]/cap:-rotate-90" />
            </CollapsibleTrigger>
            <CollapsibleContent className="flex flex-col gap-1 pt-1">
              <p className="px-2 py-1 text-xs text-muted-foreground/80">
                On this disk, and nothing here runs it. The AI Models page is where these can
                be deleted.
              </p>
              {unsupported.map((model) => (
                // The row shape matches the ones above — but a plain div: no
                // role, no tabIndex, no click. A control that looks pressable
                // and is not teaches the wrong thing.
                <div
                  key={model.id}
                  className="flex flex-col gap-0.5 rounded-md px-2 py-1.5 text-muted-foreground/80"
                >
                  <span className="flex items-baseline gap-2 text-sm">
                    <span className="min-w-0 flex-1 truncate">{model.label}</span>
                    <span className="shrink-0 text-xs">{modelSizeLabel(model.size_gb)}</span>
                  </span>
                  <span className="truncate text-xs">{model.id}</span>
                  {/* What it IS, when the repo said. Null gets no chip. */}
                  {model.task && (
                    <span className="pt-0.5">
                      <Badge variant="outline" className="text-[11px]">
                        {model.task}
                      </Badge>
                    </span>
                  )}
                  {/* The server's own sentence (ai/tasks.py); empty for a repo
                      we could not identify. */}
                  {model.reason && <p className="text-xs">{model.reason}</p>}
                </div>
              ))}
            </CollapsibleContent>
          </Collapsible>
        )}
      </aside>

      {/* -- the stage ----------------------------------------------------- */}
      <section className="flex min-h-0 min-w-0 flex-1 flex-col">
        {!selected ? (
          <div className="flex flex-1 items-center justify-center p-6 text-sm text-muted-foreground">
            {blockedAsk
              ? `${groupLabel(blockedAsk.row.capability)} is not available here — ${blockedAsk.reason}`
              : "No models to try yet — the Local tab is where a first one comes from."}
          </div>
        ) : (
          <>
            {/* The model header: name, repo, the facts as badges, and the
                lifecycle actions — one slim bordered band, not a card. */}
            <header className="flex flex-col gap-2 border-b px-6 py-4">
              <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
                <div className="min-w-0">
                  <h1 className="truncate text-lg font-semibold tracking-tight">
                    {modelName(selected.model)}
                  </h1>
                  {/* The full repo id — a link only when it IS a repo id:
                      llama.cpp entries are keyed by bare .gguf filename, and
                      huggingface.co/<filename> is a 404 dressed as a link. */}
                  {selected.model.id.includes("/") ? (
                    <a
                      className="block truncate text-xs text-muted-foreground hover:underline"
                      href={hubModelUrl(selected.model.id)}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {selected.model.id}
                    </a>
                  ) : (
                    <span className="block truncate text-xs text-muted-foreground">
                      {selected.model.id}
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {!selected.model.downloaded && !selectedDownloading && (
                    <Button type="button" variant="outline" size="sm" onClick={runDownload}>
                      <DownloadIcon data-icon="inline-start" />
                      Download{selectedSize ? ` (${selectedSize.text})` : ""}
                    </Button>
                  )}
                  {/* The pull, IN THE BUTTON'S OWN SLOT: a ring and the two
                      byte figures in the width of a button. Hovering the
                      progress swaps in the way out (Cancel), which stays in
                      the DOM and focusable while invisible so it is reachable
                      by tab and not only by pointer. */}
                  {selectedDownloading && (
                    <div
                      className="group/dl flex h-8 items-center gap-2 rounded-md border border-dashed px-3 text-xs text-muted-foreground"
                      title={
                        downloadedFraction !== null
                          ? `Downloading — ${Math.floor(downloadedFraction * 100)}%`
                          : "Downloading…"
                      }
                    >
                      <DownloadIcon className="size-3.5" aria-hidden="true" />
                      <span className="grid">
                        <span
                          className={cn(
                            "col-start-1 row-start-1 flex items-center gap-1.5",
                            stoppable &&
                              "group-hover/dl:opacity-0 group-focus-within/dl:opacity-0",
                          )}
                        >
                          <DownloadRing job={jobForSelected} />
                          {downloadedBytes && (
                            <span>
                              {formatSize(jobForSelected?.done as number)} /{" "}
                              {formatSize(jobForSelected?.total as number)}
                            </span>
                          )}
                        </span>
                        {stoppable && (
                          <button
                            type="button"
                            className="col-start-1 row-start-1 text-destructive underline opacity-0 group-hover/dl:opacity-100 group-focus-within/dl:opacity-100 focus:opacity-100"
                            title={`Stop downloading ${selected.model.id}`}
                            onClick={() => void runCancelDownload(stoppable)}
                          >
                            Cancel
                          </button>
                        )}
                      </span>
                    </div>
                  )}
                  {selected.model.downloaded && !selectedResident && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={runLoad}
                      title="Optional — the first generation loads it too"
                    >
                      Load
                    </Button>
                  )}
                  {selectedResident && selectedResident.state === "ready" && (
                    <Button type="button" variant="outline" size="sm" onClick={runUnload}>
                      Unload
                    </Button>
                  )}
                  {/* The playground's exit ramp: everything tried here is one
                      `fused.ai` call in a page, and this hands the /apps
                      composer an annotation naming the model and the tuned
                      settings. The one filled button in the header — it is
                      the destination action, not a lifecycle chore. */}
                  <Button
                    type="button"
                    size="sm"
                    title="Open the app builder with this model and your settings pre-filled"
                    onClick={() =>
                      navigateUrl(
                        "/apps?annot=" +
                          encodeURIComponent(
                            JSON.stringify(
                              buildAppAnnotation(selected.model, selected.row.capability),
                            ),
                          ),
                      )
                    }
                  >
                    <SparklesIcon data-icon="inline-start" />
                    Build an app
                  </Button>
                </div>
              </div>
              {(selected.model.params ||
                selected.model.quantization ||
                selected.model.size_gb != null ||
                fitNote) && (
                <div className="flex flex-wrap items-center gap-1.5">
                  {selected.model.params && (
                    <Badge variant="secondary" title="Parameters">
                      {selected.model.params}
                    </Badge>
                  )}
                  {selected.model.quantization && (
                    <Badge variant="secondary" title="Quantization">
                      {selected.model.quantization}
                    </Badge>
                  )}
                  {selected.model.size_gb != null && (
                    <Badge variant="secondary" title="Download size">
                      {modelSizeLabel(selected.model.size_gb, jobForSelected)}
                    </Badge>
                  )}
                  {/* WILL IT RUN HERE — the server's verdict over the weights
                      and this machine's RAM. All three answers are drawn:
                      this fact answers "can my machine run this", which is
                      asked OF EVERY MODEL. `null` draws nothing. */}
                  {fitNote && (
                    <Badge variant="outline" className={fitNote.tone} title={fitNote.title}>
                      {fitNote.text}
                    </Badge>
                  )}
                  {/* The curator's sentence — the rail clamps it, this line
                      does not. */}
                  {selected.model.note && (
                    <span className="basis-full pt-0.5 text-xs text-muted-foreground">
                      {selected.model.note}
                    </span>
                  )}
                </div>
              )}
            </header>

            {(actionError || (blockedAsk && selected)) && (
              <div className="flex flex-col gap-2 px-6 pt-4">
                {actionError && (
                  <Alert variant="destructive">
                    <AlertDescription>{actionError}</AlertDescription>
                  </Alert>
                )}
                {blockedAsk && (
                  // Not destructive: nothing failed — the link simply named a
                  // task this machine cannot run, and the stage below is the
                  // substitute, said out loud.
                  <Alert>
                    <AlertDescription>
                      {groupLabel(blockedAsk.row.capability)} is not available here —{" "}
                      {blockedAsk.reason} Showing {groupLabel(selected.row.capability)} instead.
                    </AlertDescription>
                  </Alert>
                )}
              </div>
            )}

            {selected.row.capability === "text-generation" ? (
              <TextStage
                key={selected.model.id}
                model={selected.model.id}
                modelLabel={modelName(selected.model)}
                downloaded={selected.model.downloaded}
              />
            ) : selected.row.capability === "text-to-image" ? (
              <ImageStage
                key={selected.model.id}
                model={selected.model.id}
                entry={selected.model}
              />
            ) : selected.row.capability === "text-to-video" ? (
              <VideoStage
                key={selected.model.id}
                model={selected.model.id}
                entry={selected.model}
                traits={selected.row.videoTraits}
              />
            ) : selected.row.capability === "automatic-speech-recognition" ? (
              <TranscribeStage key={selected.model.id} model={selected.model.id} />
            ) : selected.row.capability === "embeddings" ? (
              <EmbedStage
                key={selected.model.id}
                model={selected.model.id}
                downloaded={selected.model.downloaded}
              />
            ) : (
              // A capability a future runner adds before this tab learns it:
              // named, never blank.
              <div className="flex flex-1 items-center justify-center p-6 text-sm text-muted-foreground">
                No playground for {groupLabel(selected.row.capability)} yet — the Local tab can
                still load and manage this model.
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
