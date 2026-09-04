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
import { hubModelUrl } from "@apps/ai_models/local/hub";
import { readParam, resetParams, writeParams } from "@apps/ai_models/lib/params";
import { isBusy, refreshAiRuntime, useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { activeJobByModel, cancelJob, fetchJobs, isRunning, type Job } from "@platform/lib/jobs";
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
import { MenuIcons } from "@platform/ui/MenuIcons";
import { Card, CardHeader, CardTitle, CardAction, CardContent } from "@platform/shadcn/ui/card";
import { Badge } from "@platform/shadcn/ui/badge";
import { Button } from "@platform/shadcn/ui/button";
import {
  BlockedAsk,
  CapabilityGroup,
  CapabilityGroupNote,
  DownloadSwap,
  DownloadSwapBytes,
  DownloadSwapIcon,
  DownloadSwapLive,
  DownloadSwapRoot,
  DownloadSwapStop,
  heroCardClass,
  ModelDownloadButton,
  ModelFoot,
  ModelFull,
  ModelLive,
  ModelName,
  ModelRail,
  ModelRow,
  ModelRowHead,
  ModelSize,
  ModelTask,
  ModelWhy,
  PlaygroundBody,
  ProgressRing,
  StageFrame,
  StageScroller,
} from "@platform/ui/playground";
import { Binary, Check, Copy, Cpu, HardDriveDownload, Sparkles } from "lucide-react";

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

// A sidebar row's download, counting. The row has one 20px corner for this and
// the glyph that lived there says only "you may fetch this" — so a pull started
// from the sidebar (or from the stage, or from another tab) left the row it is
// about looking idle, with the percentage two hundred pixels away in the stage
// header.
//
// A RING and not a bar: the slot is a square the width of an icon, and a 3px
// bar in it is four pixels of fill nobody can read. It replaces the glyph in
// place rather than joining it, because the arrow and the ring make the same
// claim about the same model and the row has no room to make it twice.
//
// The arc, the quarter-turn and the idle spin are `ProgressRing`'s
// (platform/ui/playground): this file's job is only to say how far the pull has
// got, and one drawing serves the rail row's corner and the stage header alike.

/** How far a pull has got, 0–1, or null while nothing can divide. The job's
 *  bytes and never the runtime's: only the worker doing the fetching knows.
 *  One function because the ring, the byte line and both titles must not be
 *  able to disagree about the same download. */
function downloadFraction(job?: Job): number | null {
  if (!job || job.unit !== "bytes" || !job.total || job.done === null) return null;
  return Math.min(1, job.done / job.total);
}

function DownloadRing({ job }: { job?: Job }) {
  return <ProgressRing value={downloadFraction(job)} />;
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

  // The RAIL's reading order — text generation leads now (2026-08-24): it is
  // the capability a reader arrives at this app FOR, the one every other
  // capability is compared against by reputation, so it is what a reader
  // should find first, not the second thing after image generation. It used
  // to lead with images on the argument that this tab is not a catalogue but
  // a set of things you can DO, and a picture is the result you can judge at
  // a glance — that argument is not wrong, it just lost to a stronger one.
  //
  // **THE LIST ITSELF LIVES IN `CAPABILITY_ORDER`** (D475), read by all
  // three tabs with no private copy anywhere — this tab kept one for exactly
  // as long as it took to notice what two copies cost: the Models and
  // Benchmark tabs sorted by the other list, so the same five sections
  // appeared in two orders on one page and a reader moving between tabs
  // re-found every one. That is still the reason there is one shared
  // constant rather than three tabs each making their own case for a
  // leading capability — the 2026-08-24 reorder changed WHICH capability
  // leads, in the one place that decision lives, not whether each tab gets
  // to decide for itself again.
  //
  // A capability missing from that list still draws — it sorts after the named
  // ones, in the order the server sent it — so a capability added server-side
  // needs no edit anywhere.
  //
  // This is also what the FALLBACK SELECTION reads, so a bare visit to
  // /ai-models/playground opens on the first section of the rail rather than on
  // whichever capability the server happened to list first — now text
  // generation's own chat stage, matching the rail. `pickPlaygroundModel`
  // needs no change: its rule was always "the first usable row" (pick.ts),
  // and this is the order that phrase is about.
  const railRows = useMemo(() => {
    const rank = (c: string) => {
      const i = CAPABILITY_ORDER.indexOf(c);
      return i === -1 ? CAPABILITY_ORDER.length : i;
    };
    return [...capabilities].sort((a, b) => rank(a.capability) - rank(b.capability));
  }, [capabilities]);

  // The selection lives in the URL. An unknown or absent id falls back to the
  // TOP SECTION's default silently (PT-9's posture: a stale link opens the
  // page, not an error) — and the fallback is `default`, never models[0], which
  // catalog.py's ordering rule makes the smallest vetted model.
  const asked = useMemo(() => readParam("model"), [urlVersion]);
  // `?cap=` names a capability, not a model — the Home strip's cards land
  // here with only a task in mind. It only steers the fallback: an explicit
  // `model` always wins, and an unknown cap value falls through silently.
  const askedCap = useMemo(() => readParam("cap"), [urlVersion]);
  // Only a row the SIDEBAR ACTUALLY DRAWS is selectable — which since D425 is
  // narrower than "in the catalog": an unavailable capability renders its
  // reason in place of its model buttons (HF-8), and an unrecommended model
  // nobody has downloaded is not offered here at all. Both rules live in
  // `pick.ts`, with the sidebar reading the same `playgroundModels` below, so
  // the drawn list and the selectable list cannot come apart.
  const selected = useMemo(
    () => pickPlaygroundModel(railRows, asked, askedCap),
    [railRows, asked, askedCap],
  );

  // What the URL asked for, when this machine cannot give it. Home's strip is
  // the STATIC `PLAYGROUND_GROUPS` list, not the catalog, so every machine
  // shows a "Search by meaning" card whether or not it has an embeddings
  // engine — and answering that click by silently opening a chat box is a
  // worse answer than naming the reason. Same for a `?model=` link to a model
  // whose capability is ruled out here.
  //
  // That card stays ONE card even though the embeddings stage now has TWO modes
  // (lines, and pictures where the model has a vision tower — SPEC §40): the
  // modes are a property of the selected MODEL and not of the capability, so a
  // second static card would promise an image search on a machine whose
  // resolved model is a prose encoder. The dispatch below is unchanged for the
  // same reason — one capability, one stage, and the stage asks the entry.
  const blockedAsk = useMemo(() => {
    if (!asked && !askedCap) return null;
    const row =
      capabilities.find((r) => r.models.some((m) => m.id === asked)) ??
      capabilities.find((r) => r.capability === askedCap);
    if (!row || row.available) return null;
    // The reason comes off the catalog and is a server sentence that may or
    // may not be punctuated; this line puts another one after it.
    // The fallback must not restate the lead-in — "X is not available here —
    // it is not available on this machine" says nothing twice.
    const reason = row.reason?.trim() || "no engine for it is installed.";
    return { row, reason: /[.!?]$/.test(reason) ? reason : reason + "." };
  }, [capabilities, asked, askedCap]);

  const select = (id: string) => {
    setActionError(null);
    // CHANGING CAPABILITY CLEARS THE SETTINGS. Every stage writes its tuning
    // into one query namespace and nulls only the keys it owns, so a merge kept
    // the abandoned stage's — which is not merely untidy: `prompt`, `steps`,
    // `seed` and `w`/`h` are spelled the same across stages and mean different
    // things, so a scene written for an image model arrived as a sentence for a
    // chat one, and a step count tuned for a distilled renderer was read by a
    // video engine with its own grid. Whatever does NOT collide is dead weight
    // in a URL people share.
    //
    // Written as "keep the model" rather than as a list of keys to drop,
    // because the list of keys is the thing that goes stale: a stage that gains
    // a parameter tomorrow is already covered here.
    //
    // A move WITHIN a capability keeps everything on purpose — comparing two
    // image models at one size and seed is the whole point of the sidebar, and
    // the stage does not even remount for it.
    const nextCapability = railRows.find((row) =>
      row.models.some((model) => model.id === id),
    )?.capability;
    // Both writes PUSH a history entry: moving between models is moving between
    // places, and Back should return to the one before — with the settings it
    // had, since a stage's slider rewrites edit the entry it is on.
    if (nextCapability && selected && nextCapability !== selected.row.capability) {
      resetParams({ model: id }, "push");
      return;
    }
    // cap dies with the first explicit pick — leaving it would make a shared
    // URL claim a task the user has since clicked away from.
    writeParams({ model: id, cap: null }, "push");
  };

  const residentRow = selected
    ? runtime.loaded.find((m) => m.capability === selected.row.capability)
    : undefined;
  const selectedResident = residentRow?.model === selected?.model.id ? residentRow : undefined;
  const selectedDownloading =
    !!selected && runtime.downloading.some((d) => d.model === selected.model.id);
  // Rows by MODEL, for the sidebar's own size cells and for `jobForSelected`
  // below — `activeJobByModel` (Part A item 1 / C3 fix) is what keeps
  // presence here meaning "active": D663 keeps a finished job's row until
  // dismissed, so without that filter a model's card stayed "busy" for the
  // session's remainder once its pull or load finished, active again the
  // moment anything else on the page was.
  const jobByModel = useMemo(() => activeJobByModel(jobs), [jobs]);
  const jobForSelected = selected ? jobByModel.get(selected.model.id) : undefined;

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
  // it, and the next tick (one second — the poll is running because the runtime
  // is busy) brings "Cancelling…" from the row itself rather than from a local
  // guess about it. Same call the Local tab's cards make.
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

  // The size to name for the selected model, wherever this page names one —
  // never understating it, and null when there is nothing to say at all (see
  // `shared/modelSize`).
  const selectedSize = selected ? modelSizeHint(selected.model.size_gb, jobForSelected) : null;
  // The fit verdict, in words, for all three answers — null and only null draws
  // nothing (see `fitNote`'s own header for the wording rule, SPEC AI-16c).
  //
  // The two bad answers are TINTED and the good one is not. Amber and red are
  // the app's warning and error tokens and they are spent here for the reason
  // they exist: "tight" and "no" are the only verdicts that ask the reader to
  // do something differently. The verdict is a badge by the model's name now
  // (the shadcn redesign's ref put it there), with a small dot carrying the
  // hue — a dot on a quiet secondary badge, not a filled colour, so it stays
  // below the RUNNING green that D461 reserves for the loud treatment.
  const fitBadge = fitNote(selected?.model.fit);

  // The running pull's own figures, for the header's ring and byte line.
  // `!!` rather than the raw chain: a `total` of 0 makes `&&` yield the NUMBER
  // 0, which React renders as a literal "0".
  const downloadedFraction = downloadFraction(jobForSelected);
  // Whether the pull can be STOPPED, by the download manager's own rule rather
  // than a looser one (`CancelButton` states it): a job its reporter never
  // marked cancellable, or one already asked to stop, offers nothing — the
  // progress then simply stays put under the pointer.
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

  return (
    <PlaygroundBody>
      <ModelRail aria-label="Models to try">
        {railRows.map((row) => {
          // The catalog's curated half, in its own smallest-first order — but
          // the RECOMMENDED subset of it (D425), because this tab is where
          // someone types a sentence rather than shops for a download: see
          // `pick.ts`. The whole shortlist is the LOCAL tab's, drawn in its
          // capability carousels beside what this disk already holds, with the
          // Hub search above them for anything the curation never named (D426).
          //
          // The uncurated repos this disk happens to hold (D323's union) are
          // still playable but sit apart under their own quiet caption — they
          // have no curator, and mixed in they read as recommendations nobody
          // made.
          const offered = playgroundModels(row);
          const curated = offered.filter((m) => m.source === "curated");
          const cached = offered.filter((m) => m.source !== "curated");
          const draw = (model: AiCatalogModel) => {
            const active = selected?.model.id === model.id;
            const downloading = runtime.downloading.some((d) => d.model === model.id);
            const name = modelName(model);
            // The card is a div-as-button, not a <button>: the Download CTA
            // lives inside it, and a button inside a button is markup browsers
            // are free to mangle.
            // The advertised figure, or a running pull's own total when that
            // is larger (see `shared/modelSize`).
            const job = jobByModel.get(model.id);
            const size = modelSizeHint(model.size_gb, job);
            return (
              <ModelRow
                key={model.id}
                role="button"
                tabIndex={0}
                active={active}
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
                {/* The whole card, one line: nickname left, then the two
                    figures and the Download glyph hard right. No repo id under
                    the name — the stage header names the selected model in
                    full. */}
                <ModelRowHead>
                  <ModelName muted={!model.downloaded}>
                    {/* Live from the supervisor, not the catalog's `loaded`
                        snapshot — a dot that outlives an unload is a lie. */}
                    {runtime.loaded.some((m) => m.model === model.id && m.state === "ready") && (
                      <ModelLive title="Loaded — answering from memory" />
                    )}
                    {name}
                  </ModelName>
                  <ModelSize
                    title={
                      size
                        ? `${size.text} download — judged against this machine's memory`
                        : undefined
                    }
                  >
                    {modelSizeLabel(model.size_gb, job)}
                  </ModelSize>
                  {/* On disk = nothing to say: the CTA exists only while there
                      is an action to take. Last on the row, RIGHT of the two
                      figures it acts on: the facts read as a block that way
                      (name, size of model, size of download) and the one
                      control sits outside it, in the corner a reader's cursor
                      is already heading for.
                      A glyph rather than the word, sharing the file's one
                      download icon (MenuIcons.download, an arrow into a tray):
                      the word competed with the model's name for the eye, and
                      an arrow-into-tray is the same claim in a quarter of the
                      width. The label survives as `aria-label`/`title` — it
                      has to, because a screen reader gets nothing from a
                      decorative path, and the title is what carries the
                      running state a text button used to say out loud. */}
                  {!model.downloaded && (
                    <ModelDownloadButton
                      type="button"
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
                      {downloading ? <DownloadRing job={job} /> : MenuIcons.download}
                    </ModelDownloadButton>
                  )}
                </ModelRowHead>
              </ModelRow>
            );
          };
          return (
            <CapabilityGroup
              key={row.capability}
              open
              icon={capabilityIcon(row.capability)}
              title={groupLabel(row.capability)}
            >
              {!row.available && (
                // Visible with its reason, never hidden: an absent group and a
                // ruled-out group look identical, and HF-8 already paid for
                // that lesson once.
                <CapabilityGroupNote>
                  {row.reason || "Not available on this machine."}
                </CapabilityGroupNote>
              )}
              {row.available && !offered.length && (
                // `offered`, not `row.models`: a capability whose whole
                // shortlist is unrecommended and undownloaded has models in
                // the catalog and nothing to draw here, and a silent empty
                // group is the one outcome this filter must not produce. The
                // curation is meant to prevent it (catalog.py keeps at least
                // one recommended entry per list, and a test pins that) — this
                // line is what the failure looks like if it ever slips.
                <CapabilityGroupNote>
                  Nothing to try here yet — the Local tab is where a first model comes from.
                </CapabilityGroupNote>
              )}
              {/* Curated first, then the uncurated repos this disk happens to
                  hold — one run of cards, no divider. The "Your downloads"
                  caption that used to separate them said something the cards
                  no longer needed said: a curated entry not yet fetched wears
                  a Download button and a fetched one does not, so which half
                  is on this disk is legible from the cards themselves, and the
                  heading was a second answer to a question already answered. */}
              {row.available && curated.map(draw)}
              {row.available && cached.map(draw)}
            </CapabilityGroup>
          );
        })}

        {unsupported.length > 0 && (
          // **Everything downloaded appears, and what cannot run says so.**
          // These rows used to be absent — `/api/ai/catalog` only listed models
          // some capability could load — so a text-to-speech model or a depth
          // estimator was a multi-gigabyte download that simply was not in the
          // sidebar, which reads as a bug in the download rather than as an
          // answer. Last, and collapsed by DEFAULT (the only `<details>` here
          // that is): it is a reference list, not a menu — nothing in it is
          // selectable, so leaving it open would put dead cards between the
          // reader and the ones they came for.
          <CapabilityGroup icon={unsupportedIcon()} title="Not supported">
            <CapabilityGroupNote>
              On this disk, and nothing here runs it. The AI Models page is where these
              can be deleted.
            </CapabilityGroupNote>
            {unsupported.map((model) => (
              // A card, so the shape matches the ones above — but a plain div:
              // no role, no tabIndex, no click. There is nothing to select, and
              // a control that looks pressable and is not teaches the wrong
              // thing about every card beside it.
              <ModelRow key={model.id} state="off">
                <ModelRowHead>
                  <ModelName>{model.label}</ModelName>
                  {/* Top-right, as on the selectable cards above — same slot,
                      so the size reads the same however the card behaves.
                      `shared/modelSize`, like every other size cell on this
                      page, with no job: a repo already on the disk is not
                      downloading. Hand-formatting it here would be the second
                      copy of a rule that exists because the copies
                      disagreed. */}
                  <ModelSize>{modelSizeLabel(model.size_gb)}</ModelSize>
                </ModelRowHead>
                <ModelFull>{model.id}</ModelFull>
                {/* What it IS, when the repo said. Null is its own answer and
                    gets no chip: "we could not tell" is what the missing label
                    means, and inventing one would be a claim. */}
                {model.task && (
                  <ModelFoot>
                    <ModelTask>{model.task}</ModelTask>
                  </ModelFoot>
                )}
                {/* The server's own sentence, written per task beside the
                    classification it explains (`ai/tasks.py`). Empty for a repo
                    we could not identify — an explanation we have not earned is
                    worse than none — and then the line simply is not drawn. */}
                {model.reason && <ModelWhy>{model.reason}</ModelWhy>}
              </ModelRow>
            ))}
          </CapabilityGroup>
        )}
      </ModelRail>

      <StageScroller>
      {/* One frame owns the width story for everything on the stage: capped at
          840px, centered, gutters below that. The hero spans it fully and the
          work column share the same box, so top and bottom can
          never drift apart. */}
      <StageFrame>
        {actionError && <ErrorBanner>{actionError}</ErrorBanner>}
        {blockedAsk && selected && (
          // Not an ErrorBanner: nothing failed and nothing the user did is
          // wrong — the link simply named a task this machine cannot run, and
          // the stage below is the substitute, said out loud.
          <BlockedAsk>
            {groupLabel(blockedAsk.row.capability)} is not available here — {blockedAsk.reason}{" "}
            Showing {groupLabel(selected.row.capability)} instead.
          </BlockedAsk>
        )}
        {!selected ? (
          <p className="cc-empty">
            {blockedAsk
              ? `${groupLabel(blockedAsk.row.capability)} is not available here — ${blockedAsk.reason}`
              : "No models to try yet — the Local tab is where a first one comes from."}
          </p>
        ) : (
          <>
            <Card className={heroCardClass + " flex-none [--card-spacing:--spacing(6)]"}>
              <CardHeader>
                <div className="flex min-w-0 flex-col gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <CardTitle className="text-lg font-semibold tracking-tight">
                      {modelName(selected.model)}
                    </CardTitle>
                    {/* WILL IT RUN HERE — the server's verdict over the weights
                        and this machine's RAM (`_fit_verdict`), as a badge by
                        the name. ALL THREE answers are drawn: this fact answers
                        "can my machine run this", which is asked of every
                        model, and a row that answers only when the answer is
                        bad is silent exactly when it is being consulted.
                        `null` still draws nothing — a verdict over a size
                        nobody measured is a lie. */}
                    {fitBadge && (
                      <Badge variant="secondary" className="gap-1.5 font-normal" title={fitBadge.title}>
                        <span className={"size-1.5 rounded-full " + fitBadge.dot} />
                        {fitBadge.text}
                      </Badge>
                    )}
                  </div>
                  {/* The full repo id — author/name as Hugging Face knows it.
                      A link only when it IS a repo id: llama.cpp entries are
                      keyed by bare .gguf filename (formats.GGUF_RECIPES), and
                      huggingface.co/<filename> is a 404 dressed as a link.
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
                    <button
                      type="button"
                      onClick={() => {
                        navigator.clipboard?.writeText(selected.model.id).catch(() => {});
                        setCopiedRepo(true);
                        window.setTimeout(() => setCopiedRepo(false), 1600);
                      }}
                      className="shrink-0 cursor-pointer appearance-none border-0 bg-transparent p-0 rounded-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      title="Copy model ID"
                    >
                      {copiedRepo ? (
                        <Check className="size-3.5 text-emerald-500" />
                      ) : (
                        <Copy className="size-3.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100" />
                      )}
                      <span className="sr-only">Copy model ID</span>
                    </button>
                  </div>
                </div>
                <CardAction className="flex flex-wrap items-center justify-end gap-2">
                  {/* The model's own lifecycle first (Download / progress /
                      Load / Unload), the exit ramp last: the rightmost slot is
                      the header's landing edge, and "Build an app" is where
                      the whole playground points. All of them outline — one
                      state cluster, one weight. */}
                  {!selected.model.downloaded && !selectedDownloading && (
                    <Button variant="outline" size="sm" onClick={runDownload}>
                      Download{selectedSize ? ` (${selectedSize.text})` : ""}
                    </Button>
                  )}
                  {/* The pull, IN THE BUTTON'S OWN SLOT — icon, ring, bytes in
                      the width of a button, with Cancel swapped in under the
                      pointer (`:hover` / `:focus-within`, nothing moves).
                      Deliberately custom rather than a shadcn piece: the
                      one-cell two-drawings grid is the point. */}
                  {selectedDownloading && (
                    <DownloadSwapRoot
                      title={
                        downloadedFraction !== null
                          ? `Downloading — ${Math.floor(downloadedFraction * 100)}%`
                          : "Downloading…"
                      }
                    >
                      <DownloadSwapIcon>{MenuIcons.download}</DownloadSwapIcon>
                      <DownloadSwap>
                        <DownloadSwapLive>
                          <DownloadRing job={jobForSelected} />
                          {downloadedBytes && (
                            <DownloadSwapBytes>
                              {formatSize(jobForSelected?.done as number)} /{" "}
                              {formatSize(jobForSelected?.total as number)}
                            </DownloadSwapBytes>
                          )}
                        </DownloadSwapLive>
                        {stoppable && (
                          <DownloadSwapStop
                            type="button"
                            title={`Stop downloading ${selected.model.id}`}
                            onClick={() => void runCancelDownload(stoppable)}
                          >
                            Cancel
                          </DownloadSwapStop>
                        )}
                      </DownloadSwap>
                    </DownloadSwapRoot>
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
                  {/* The playground's exit ramp: everything tried here is one
                      `fused.ai` call in a page, and this hands the /apps
                      composer an annotation naming the model and the tuned
                      settings — shown as a chip, not dumped into the prompt
                      box — so the user just types the app they want. */}
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
              {/* No facts, no CardContent: an empty one still takes the card's
                  flex gap and leaves blank space under the header. */}
              {(selected.model.params ||
                selected.model.quantization ||
                selected.model.size_gb != null) && (
                <CardContent className="flex flex-col gap-4">
                  {/* Every fact carries its own divider bar, drawn by ::before
                      in the gap to its LEFT — and `overflow-x-clip` on the
                      list clips the bar off whichever fact starts a line, the
                      first included. A per-item `i > 0` separator can't do
                      that: when the row wraps, the wrapped item would open its
                      line with a stray bar. */}
                  <dl className="relative m-0 flex flex-wrap items-center gap-x-4 gap-y-2 overflow-x-clip text-sm">
                    {[
                      selected.model.params
                        ? {
                            icon: Cpu,
                            label: "Parameters",
                            value: selected.model.params,
                          }
                        : null,
                      selected.model.quantization
                        ? {
                            icon: Binary,
                            label: "Quantization",
                            value: selected.model.quantization,
                          }
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
                entry={selected.model}
              />
            ) : (
              // A capability a future runner adds before this tab learns it:
              // named, never blank — the same posture the group labels take.
              <p className="cc-empty">
                No playground for {groupLabel(selected.row.capability)} yet — the Local tab can
                still load and manage this model.
              </p>
            )}
          </>
        )}
      </StageFrame>
      </StageScroller>
    </PlaygroundBody>
  );
}
