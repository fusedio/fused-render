// The Playground tab: pick a local model on the left, use it on the right.
//
// Everything else on /ai-models is ABOUT models — what is on disk, what could
// be, which backend serves it. This tab is the one that answers the first
// question a person has ("what can this thing actually do?") by letting them
// do it: a one-shot prompt for a text model, a prompt-to-picture stage for an
// image model, a record-and-transcribe stage for a speech model. Every stage
// is the same API-surface shape — input, Run, the result of that run — on the
// hero card's centered column. The stage is chosen
// by the selected model's capability, so a capability added server-side gets a
// named placeholder here rather than a blank.
//
// The sidebar is `GET /api/ai/catalog`, verbatim — the same payload every
// page's model picker reads (D323), so a model downloaded from the Local tab's
// Hub search is in the playground with no curation edit. Rows show the curated `nickname`
// (catalog.py) with the full label one hover away.
//
// SELECTING IS NOT LOADING. One model per capability is resident and loading
// evicts (AI-4), so a sidebar where every click moved gigabytes would turn
// browsing into eviction thrash. A click renders the stage and rewrites the
// URL; the weights move when the user acts — Download explicitly, or the first
// generation, which starts the load itself (the chat stage rides AI-5's 409
// dance, image and transcription load inside their own jobs).
//
// The URL carries the setup, never the transcript: `model` plus each stage's
// non-default settings, written with `replaceSearch` because browsing models
// is not history the back button should replay.
import { useEffect, useMemo, useRef, useState } from "react";
import { TextStage } from "./TextStage";
import { ImageStage } from "./ImageStage";
import { VideoStage } from "./VideoStage";
import { TranscribeStage } from "./TranscribeStage";
import { EmbedStage } from "./EmbedStage";
import { modelSizeHint, modelSizeLabel } from "@apps/ai_models/shared/modelSize";
import { fitNote } from "@apps/ai_models/shared/fitNote";
import { formatSize } from "@platform/lib/format";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { CAPABILITY_ORDER } from "@apps/ai_models/lib/aiModelGroups";
import { PLAYGROUND_GROUPS } from "./groups";
import { buildAppAnnotation, modelName } from "./appSeed";
import { capabilityIcon, unsupportedIcon } from "./capabilityIcons";
import { pickPlaygroundModel, playgroundModels } from "./pick";
import { PanelSlotContext } from "./controls";
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
import { cn } from "@platform/lib/utils";
import { Alert, AlertDescription } from "@platform/shadcn/ui/alert";
import { Card, CardHeader, CardTitle, CardAction, CardContent } from "@platform/shadcn/ui/card";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import { EntityList } from "@platform/ui/flow/EntityRow";
import { StatusDot } from "@platform/ui/flow/StatusIcon";
import { Muted, Tiny } from "@platform/ui/flow/Typography";
import { bucketText, type StatusBucket } from "@platform/ui/status-colors";
import {
  Binary,
  Check,
  ChevronDown,
  Copy,
  Cpu,
  Download,
  HardDriveDownload,
  Sparkles,
} from "lucide-react";

// What the groups are called HERE: the capability vocabulary is exact
// ("automatic-speech-recognition") and `capabilityLabel` is faithful to it
// ("Speech to text") — this tab names the WORK instead ("Text generation"),
// which is the vocabulary the Home strip's cards already use
// (PLAYGROUND_GROUPS, shared with them so one capability has one name). An
// unknown capability falls back to the shared label, so a new runner appears
// (plainly named) instead of vanishing.
const GROUP_LABELS: Record<string, string> = Object.fromEntries(
  PLAYGROUND_GROUPS.map((g) => [g.capability, g.label]),
);
function groupLabel(capability: string): string {
  return GROUP_LABELS[capability] ?? capabilityLabel(capability);
}

// A sidebar row's download, counting. The row has one icon-sized corner for
// this and the glyph that lived there says only "you may fetch this" — so a
// pull started from the sidebar left the row it is about looking idle.
//
// A RING and not a bar: the slot is a square the width of an icon, and a 3px
// bar in it is four pixels of fill nobody can read. It replaces the glyph in
// place rather than joining it.
//
// The arc is drawn with `stroke-dasharray`/`-dashoffset` on a circle rotated a
// quarter turn back, so 0% starts at twelve o'clock. An unmeasured pull — no
// total yet — spins a fixed quarter-arc instead: a ring frozen at 0 reads as a
// download that has stalled, which is the one thing it is not.
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
      className={cn(
        "size-3.5 flex-none -rotate-90 overflow-visible",
        measured === null && "motion-safe:animate-spin",
      )}
      viewBox="0 0 16 16"
      aria-hidden="true"
    >
      <circle className="fill-none stroke-border stroke-2" cx="8" cy="8" r={RING_R} />
      <circle
        className="fill-none stroke-primary stroke-2 [stroke-linecap:round] motion-safe:transition-[stroke-dashoffset] motion-safe:duration-200"
        cx="8"
        cy="8"
        r={RING_R}
        strokeDasharray={RING_C}
        strokeDashoffset={measured === null ? RING_C * 0.75 : RING_C * (1 - measured)}
      />
    </svg>
  );
}

/** The fit verdict's colour bucket — the two bad answers are tinted, the good
 *  one is not loud (D461 reserves the RUNNING green for the loud treatment). */
function fitBucket(verdict: string | undefined): StatusBucket {
  return verdict === "easy" ? "green" : verdict === "tight" ? "orange" : "red";
}

/** A capability section of the sidebar: a <details>, its summary the icon +
 *  task-word header with a caret that turns when it opens. */
function SidebarGroup({
  icon,
  title,
  open,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  open?: boolean;
  children: React.ReactNode;
}) {
  return (
    <details className="group" open={open}>
      <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-semibold text-muted-foreground select-none hover:text-foreground [&::-webkit-details-marker]:hidden">
        <span className="inline-flex flex-none [&_svg]:size-[18px]">{icon}</span>
        <span>{title}</span>
        <ChevronDown className="size-3.5 flex-none motion-safe:transition-transform group-open:rotate-180" />
      </summary>
      <div className="mt-2 flex flex-col gap-2">{children}</div>
    </details>
  );
}

type CatalogLoad =
  | { status: "loading" }
  | { status: "ok"; capabilities: AiCatalogCapability[]; unsupported: AiUnsupportedModel[] }
  | { status: "error"; message: string };

export default function PlaygroundTab() {
  const [catalog, setCatalog] = useState<CatalogLoad>({ status: "loading" });
  const [actionError, setActionError] = useState<string | null>(null);
  // The copy-repo tick, briefly. Not per-model: switching models mid-tick just
  // lets the tick lapse on its own.
  const [copiedRepo, setCopiedRepo] = useState(false);
  // Where the stages' settings panels portal to (controls.tsx, ConfigPanel):
  // the right-hand sibling of the scrolling stage column.
  const [panelSlot, setPanelSlot] = useState<HTMLElement | null>(null);
  const runtime = useAiRuntime();
  // `useUrlVersion`, not `useNavEpoch`: a model pick is PUSHED (so Back walks
  // through the models tried) and the nav epoch would hear that, but the
  // stages' own slider rewrites are `replaceSearch`, which fires only
  // fused:urlchange — and this counter hears all three.
  const urlVersion = useUrlVersion();

  // One fetch per mount, then again when a download lands: `downloaded` on the
  // rows is a disk fact and a finished pull is the one thing here that changes
  // it. The shrink-detection mirrors AiModels.tsx's — a model that LEFT the
  // downloading list is a pull that ended, however it ended.
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
  // the model id), the same join AiModels.tsx uses and for the same reason:
  // the id derivation sanitises characters and must not be copied here.
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
  // Downloaded, and runnable by nothing here. Drawn rather than dropped: this
  // sidebar is the only list of what is on the disk that this tab shows, and a
  // model that silently is not in it reads as a download that failed.
  const unsupported = catalog.status === "ok" ? catalog.unsupported : [];

  // The RAIL's reading order — **THE LIST ITSELF LIVES IN `CAPABILITY_ORDER`**
  // (D475), read by all three tabs with no private copy anywhere. A capability
  // missing from that list still draws — it sorts after the named ones, in the
  // order the server sent it — so a capability added server-side needs no edit
  // anywhere. This is also what the FALLBACK SELECTION reads, so a bare visit
  // to /ai-models/playground opens on the first section of the rail.
  const railRows = useMemo(() => {
    const rank = (c: string) => {
      const i = CAPABILITY_ORDER.indexOf(c);
      return i === -1 ? CAPABILITY_ORDER.length : i;
    };
    return [...capabilities].sort((a, b) => rank(a.capability) - rank(b.capability));
  }, [capabilities]);

  // The selection lives in the URL. An unknown or absent id falls back to the
  // TOP SECTION's default silently (PT-9's posture: a stale link opens the
  // page, not an error).
  const asked = useMemo(() => readParam("model"), [urlVersion]);
  // `?cap=` names a capability, not a model — the Home strip's cards land
  // here with only a task in mind. It only steers the fallback.
  const askedCap = useMemo(() => readParam("cap"), [urlVersion]);
  // Only a row the SIDEBAR ACTUALLY DRAWS is selectable — both rules live in
  // `pick.ts`, with the sidebar reading the same `playgroundModels` below.
  const selected = useMemo(
    () => pickPlaygroundModel(railRows, asked, askedCap),
    [railRows, asked, askedCap],
  );

  // What the URL asked for, when this machine cannot give it. Home's strip is
  // the STATIC `PLAYGROUND_GROUPS` list, not the catalog, so answering that
  // click by silently opening a chat box is a worse answer than naming the
  // reason. Same for a `?model=` link to a model whose capability is ruled out.
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
    // CHANGING CAPABILITY CLEARS THE SETTINGS. Every stage writes its tuning
    // into one query namespace and nulls only the keys it owns, so a merge kept
    // the abandoned stage's — and `prompt`, `steps`, `seed` and `w`/`h` are
    // spelled the same across stages and mean different things. Written as
    // "keep the model" rather than as a list of keys to drop. A move WITHIN a
    // capability keeps everything on purpose.
    const nextCapability = railRows.find((row) =>
      row.models.some((model) => model.id === id),
    )?.capability;
    // Both writes PUSH a history entry: moving between models is moving between
    // places, and Back should return to the one before.
    if (nextCapability && selected && nextCapability !== selected.row.capability) {
      resetParams({ model: id }, "push");
      return;
    }
    // cap dies with the first explicit pick.
    writeParams({ model: id, cap: null }, "push");
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
  // Rows by MODEL, for the sidebar's own size cells — the same title match
  // `jobForSelected` uses one line up.
  const jobByModel = useMemo(
    () => new Map(jobs.filter((j) => j.owner === "server").map((j) => [j.title, j])),
    [jobs],
  );

  // The sidebar cards and the stage header share this: same call, same error
  // surface (the stage's banner — the card has no room for a sentence).
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
  // The download manager's ✕, on the card the download is being watched from. A
  // REQUEST and not a state change: the job row stays until the worker honours
  // it, and the next tick brings "Cancelling…" from the row itself.
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
    return <Muted className="px-1 py-4">Reading the model catalog…</Muted>;
  }
  if (catalog.status === "error") {
    return (
      <Alert variant="destructive">
        <AlertDescription>{catalog.message}</AlertDescription>
      </Alert>
    );
  }

  // The size to name for the selected model, wherever this page names one —
  // never understating it, and null when there is nothing to say at all (see
  // `shared/modelSize`).
  const selectedSize = selected ? modelSizeHint(selected.model.size_gb, jobForSelected) : null;
  // The fit verdict, in words, for all three answers — null and only null draws
  // nothing (see `fitNote`'s own header for the wording rule, SPEC AI-16c). The
  // hue rides a status dot on a quiet secondary badge, never a filled colour.
  const fitBadge = fitNote(selected?.model.fit);

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
  // `!!` rather than the raw chain: a `total` of 0 makes `&&` yield the NUMBER
  // 0, which React renders as a literal "0".
  const downloadedBytes = !!(
    jobForSelected && jobForSelected.unit === "bytes" && jobForSelected.total &&
    jobForSelected.done !== null
  );

  return (
    <div className="flex min-h-0 flex-1 items-stretch gap-5 max-md:flex-col">
      {/* `pg-side` is a bare MARKER class — the AI tour (platform/lib/tours)
          spotlights the rail by that name; nothing styles it. */}
      <aside
        className="pg-side flex w-[300px] shrink-0 flex-col gap-6 overflow-y-auto border-r border-border pr-4 scrollbar-auto-hide max-md:max-h-[38vh] max-md:w-full max-md:border-r-0 max-md:border-b max-md:pr-0 max-md:pb-3"
        aria-label="Models to try"
      >
        {railRows.map((row) => {
          // The catalog's curated half, in its own smallest-first order — but
          // the RECOMMENDED subset of it (D425): see `pick.ts`. The uncurated
          // repos this disk happens to hold (D323's union) follow, playable but
          // uncurated.
          const offered = playgroundModels(row);
          const curated = offered.filter((m) => m.source === "curated");
          const cached = offered.filter((m) => m.source !== "curated");
          const draw = (model: AiCatalogModel) => {
            const active = selected?.model.id === model.id;
            const downloading = runtime.downloading.some((d) => d.model === model.id);
            const name = modelName(model);
            // The row is a div-as-button, not a <button>: the Download CTA
            // lives inside it, and a button inside a button is markup browsers
            // are free to mangle.
            const job = jobByModel.get(model.id);
            const size = modelSizeHint(model.size_gb, job);
            const live = runtime.loaded.some((m) => m.model === model.id && m.state === "ready");
            return (
              <div
                key={model.id}
                role="button"
                tabIndex={0}
                className={cn(
                  "flex w-full min-w-0 cursor-pointer items-center gap-2 border-b border-border px-3 py-2 text-left text-sm last:border-b-0 hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:outline-none",
                  active && "bg-accent/30",
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
                {/* Live from the supervisor, not the catalog's `loaded`
                    snapshot — a dot that outlives an unload is a lie. */}
                {live && <StatusDot status="live" label="Loaded — answering from memory" />}
                {/* NOT ON THIS DISK: the name greys to the weight of the
                    figures beside it, so "you have this one" is visible at a
                    glance down the rail. */}
                <span
                  className={cn(
                    "min-w-0 flex-1 truncate font-medium",
                    !model.downloaded && "text-muted-foreground",
                  )}
                >
                  {name}
                </span>
                <Tiny
                  className="flex-none tabular-nums"
                  title={
                    size
                      ? `${size.text} download — judged against this machine's memory`
                      : undefined
                  }
                >
                  {modelSizeLabel(model.size_gb, job)}
                </Tiny>
                {/* On disk = nothing to say: the CTA exists only while there
                    is an action to take. A glyph rather than the word; the
                    label survives as `aria-label`/`title`. */}
                {!model.downloaded && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-xs"
                    className="text-muted-foreground"
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
                    {downloading ? <DownloadRing job={job} /> : <Download />}
                  </Button>
                )}
              </div>
            );
          };
          return (
            <SidebarGroup
              key={row.capability}
              icon={capabilityIcon(row.capability)}
              title={groupLabel(row.capability)}
              open
            >
              {!row.available && (
                // Visible with its reason, never hidden: an absent group and a
                // ruled-out group look identical (HF-8).
                <Tiny className="block leading-snug">
                  {row.reason || "Not available on this machine."}
                </Tiny>
              )}
              {row.available && !offered.length && (
                <Tiny className="block leading-snug">
                  Nothing to try here yet — the Local tab is where a first model comes from.
                </Tiny>
              )}
              {/* Curated first, then the uncurated repos this disk happens to
                  hold — one run of rows, no divider: which half is on this
                  disk is legible from the rows themselves. */}
              {row.available && offered.length > 0 && (
                <EntityList>
                  {curated.map(draw)}
                  {cached.map(draw)}
                </EntityList>
              )}
            </SidebarGroup>
          );
        })}

        {unsupported.length > 0 && (
          // **Everything downloaded appears, and what cannot run says so.**
          // Last, and collapsed by DEFAULT (the only <details> here that is):
          // it is a reference list, not a menu.
          <SidebarGroup icon={unsupportedIcon()} title="Not supported">
            <Tiny className="block leading-snug">
              On this disk, and nothing here runs it. The AI Models page is where these
              can be deleted.
            </Tiny>
            <EntityList className="border-dashed bg-transparent">
              {unsupported.map((model) => (
                // A row, so the shape matches the ones above — but a plain
                // div: no role, no tabIndex, no click. There is nothing to
                // select.
                <div
                  key={model.id}
                  className="flex flex-col gap-0.5 border-b border-dashed border-border px-3 py-2 text-sm text-muted-foreground last:border-b-0"
                >
                  <span className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate font-medium">{model.label}</span>
                    <Tiny className="flex-none tabular-nums">{modelSizeLabel(model.size_gb)}</Tiny>
                  </span>
                  <span className="truncate font-mono text-xs">{model.id}</span>
                  {/* What it IS, when the repo said. Null gets no chip. */}
                  {model.task && <Tiny className="truncate">{model.task}</Tiny>}
                  {/* The server's own sentence (`ai/tasks.py`); empty for a repo
                      we could not identify, and then simply not drawn. */}
                  {model.reason && (
                    <Tiny className="mt-1 block text-[11.5px] leading-snug">{model.reason}</Tiny>
                  )}
                </div>
              ))}
            </EntityList>
          </SidebarGroup>
        )}
      </aside>

      {/* The stage: a scrolling column, and beside it the slot the stages'
          settings panels portal into (ConfigPanel). */}
      <PanelSlotContext.Provider value={panelSlot}>
        <div className="flex min-h-0 min-w-0 flex-1 items-stretch">
          <div className="min-h-0 min-w-0 flex-1 overflow-y-auto [overflow-x:clip] scrollbar-auto-hide">
            {/* One frame owns the width story for everything on the stage:
                capped at 840px, centered, gutters below that. */}
            <div className="mx-auto flex w-[min(840px,100%-32px)] flex-col gap-3 pb-4 max-md:w-full">
              {actionError && (
                <Alert variant="destructive">
                  <AlertDescription>{actionError}</AlertDescription>
                </Alert>
              )}
              {blockedAsk && selected && (
                // Not an alert: nothing failed and nothing the user did is
                // wrong — the link simply named a task this machine cannot
                // run, and the stage below is the substitute, said out loud.
                <Tiny className="block leading-snug">
                  {groupLabel(blockedAsk.row.capability)} is not available here — {blockedAsk.reason}{" "}
                  Showing {groupLabel(selected.row.capability)} instead.
                </Tiny>
              )}
              {!selected ? (
                <Muted className="py-4">
                  {blockedAsk
                    ? `${groupLabel(blockedAsk.row.capability)} is not available here — ${blockedAsk.reason}`
                    : "No models to try yet — the Local tab is where a first one comes from."}
                </Muted>
              ) : (
                <>
                  {/* The 1px top margin keeps the Card's ring (a box-shadow
                      drawn just OUTSIDE the box) from being clipped by the
                      scroller's top edge. */}
                  <Card className="mt-px mb-4 w-full flex-none [--card-spacing:--spacing(6)]">
                    <CardHeader>
                      <div className="flex min-w-0 flex-col gap-2">
                        <div className="flex flex-wrap items-center gap-2">
                          <CardTitle className="text-lg font-semibold tracking-tight">
                            {modelName(selected.model)}
                          </CardTitle>
                          {/* WILL IT RUN HERE — the server's verdict as a badge
                              by the name. ALL THREE answers are drawn; `null`
                              draws nothing. */}
                          {fitBadge && selected.model.fit && (
                            <Badge variant="secondary" className="gap-1.5 font-normal" title={fitBadge.title}>
                              <StatusDot bucket={fitBucket(selected.model.fit.verdict)} />
                              {fitBadge.text}
                            </Badge>
                          )}
                        </div>
                        {/* The full repo id — a link only when it IS a repo id:
                            llama.cpp entries are keyed by bare .gguf filename.
                            The copy button rides beside it, revealed on hover. */}
                        <div className="group flex min-w-0 items-center gap-1.5">
                          {selected.model.id.includes("/") ? (
                            <a
                              className="truncate font-mono text-xs text-muted-foreground no-underline transition-colors hover:text-foreground hover:underline"
                              href={hubModelUrl(selected.model.id)}
                              target="_blank"
                              rel="noopener noreferrer"
                            >
                              {selected.model.id}
                            </a>
                          ) : (
                            <span className="truncate font-mono text-xs text-muted-foreground">
                              {selected.model.id}
                            </span>
                          )}
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon-xs"
                            onClick={() => {
                              navigator.clipboard?.writeText(selected.model.id).catch(() => {});
                              setCopiedRepo(true);
                              window.setTimeout(() => setCopiedRepo(false), 1600);
                            }}
                            className="text-muted-foreground"
                            title="Copy model ID"
                          >
                            {copiedRepo ? (
                              <Check className={cn("size-3.5", bucketText.green)} />
                            ) : (
                              <Copy className="size-3.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100" />
                            )}
                            <span className="sr-only">Copy model ID</span>
                          </Button>
                        </div>
                      </div>
                      <CardAction className="flex flex-wrap items-center justify-end gap-2">
                        {/* The model's own lifecycle first (Download / progress /
                            Load / Unload), the exit ramp last. All outline —
                            one state cluster, one weight. */}
                        {!selected.model.downloaded && !selectedDownloading && (
                          <Button variant="outline" size="sm" onClick={runDownload}>
                            Download{selectedSize ? ` (${selectedSize.text})` : ""}
                          </Button>
                        )}
                        {/* The pull, IN THE BUTTON'S OWN SLOT — icon, ring,
                            bytes, with Cancel swapped in under the pointer.
                            One grid cell, two drawings, so the box measures
                            the wider of the two at rest and hover moves
                            nothing. Opacity, not display: a display-toggled
                            button cannot be focused. */}
                        {selectedDownloading && (
                          <div
                            className="group/dl flex items-center gap-2 self-center text-xs text-muted-foreground"
                            title={
                              downloadedFraction !== null
                                ? `Downloading — ${Math.floor(downloadedFraction * 100)}%`
                                : "Downloading…"
                            }
                          >
                            <Download className="size-3.5 flex-none" aria-hidden="true" />
                            <span className="grid items-center justify-items-start *:[grid-area:1/1]">
                              <span className="flex items-center gap-2 motion-safe:transition-opacity group-hover/dl:pointer-events-none group-hover/dl:opacity-0 group-focus-within/dl:pointer-events-none group-focus-within/dl:opacity-0">
                                <DownloadRing job={jobForSelected} />
                                {downloadedBytes && (
                                  <span className="whitespace-nowrap tabular-nums">
                                    {formatSize(jobForSelected?.done as number)} /{" "}
                                    {formatSize(jobForSelected?.total as number)}
                                  </span>
                                )}
                              </span>
                              {stoppable && (
                                <Button
                                  type="button"
                                  variant="link"
                                  size="xs"
                                  className="pointer-events-none h-auto px-0 text-xs text-destructive/85 opacity-0 underline underline-offset-2 hover:text-destructive motion-safe:transition-opacity group-hover/dl:pointer-events-auto group-hover/dl:opacity-100 group-focus-within/dl:pointer-events-auto group-focus-within/dl:opacity-100"
                                  title={`Stop downloading ${selected.model.id}`}
                                  onClick={() => void runCancelDownload(stoppable)}
                                >
                                  Cancel
                                </Button>
                              )}
                            </span>
                          </div>
                        )}
                        {selected.model.downloaded && !selectedResident && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={runLoad}
                            title="Optional — the first generation loads it too"
                          >
                            Load
                          </Button>
                        )}
                        {selectedResident && selectedResident.state === "ready" && (
                          <Button variant="outline" size="sm" onClick={runUnload}>
                            Unload
                          </Button>
                        )}
                        {/* The playground's exit ramp: hands the /apps composer
                            an annotation naming the model and the tuned
                            settings. */}
                        <Button
                          variant="outline"
                          size="sm"
                          title="Open the app builder with this model and your settings pre-filled"
                          onClick={() =>
                            navigateUrl(
                              "/apps?annot=" +
                                encodeURIComponent(
                                  JSON.stringify(buildAppAnnotation(selected.model, selected.row.capability)),
                                ),
                            )
                          }
                        >
                          <Sparkles data-icon="inline-start" />
                          Build an app
                        </Button>
                      </CardAction>
                    </CardHeader>
                    {/* No facts, no CardContent: an empty one still takes the
                        card's flex gap. */}
                    {(selected.model.params ||
                      selected.model.quantization ||
                      selected.model.size_gb != null) && (
                      <CardContent className="flex flex-col gap-4">
                        {/* Every fact carries its own divider bar, drawn by
                            ::before in the gap to its LEFT — and `overflow-x-clip`
                            clips the bar off whichever fact starts a line. */}
                        <dl className="relative m-0 flex flex-wrap items-center gap-x-4 gap-y-2 overflow-x-clip text-sm">
                          {[
                            selected.model.params
                              ? { icon: Cpu, label: "Parameters", value: selected.model.params }
                              : null,
                            selected.model.quantization
                              ? { icon: Binary, label: "Quantization", value: selected.model.quantization }
                              : null,
                            selected.model.size_gb != null
                              ? {
                                  icon: HardDriveDownload,
                                  label: "Download",
                                  value: modelSizeLabel(selected.model.size_gb, jobForSelected),
                                }
                              : null,
                          ]
                            .filter((f) => f !== null)
                            .map((f) => (
                              <div
                                key={f.label}
                                className="relative flex items-center gap-2 before:absolute before:top-1/2 before:-left-2 before:h-4 before:w-px before:-translate-y-1/2 before:bg-border"
                              >
                                <f.icon className="size-4 shrink-0 text-muted-foreground" />
                                <dt className="text-muted-foreground">{f.label}</dt>
                                <dd className="m-0 font-medium tabular-nums">{f.value}</dd>
                              </div>
                            ))}
                        </dl>
                      </CardContent>
                    )}
                  </Card>
                  {selected.row.capability === "text-generation" ? (
                    <TextStage
                      key={selected.model.id}
                      model={selected.model.id}
                      modelLabel={modelName(selected.model)}
                      downloaded={selected.model.downloaded}
                      entry={selected.model}
                    />
                  ) : selected.row.capability === "text-to-image" ? (
                    <ImageStage key={selected.model.id} model={selected.model.id} entry={selected.model} />
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
                      entry={selected.model}
                    />
                  ) : (
                    // A capability a future runner adds before this tab learns
                    // it: named, never blank.
                    <Muted className="py-4">
                      No playground for {groupLabel(selected.row.capability)} yet — the Local tab can
                      still load and manage this model.
                    </Muted>
                  )}
                </>
              )}
            </div>
          </div>
          {/* The settings slot. Empty (hidden) until a stage opens its cog. */}
          <div ref={setPanelSlot} className="flex min-h-0 shrink-0 empty:hidden" />
        </div>
      </PanelSlotContext.Provider>
    </div>
  );
}
