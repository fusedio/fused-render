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
import { formatSize } from "@platform/lib/format";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { PLAYGROUND_GROUPS } from "./groups";
import { buildAppSeed, modelName } from "./appSeed";
import { capabilityIcon, unsupportedIcon } from "./capabilityIcons";
import { pickPlaygroundModel, playgroundModels } from "./pick";
import { PlaygroundApps } from "./PlaygroundApps";
import { hubModelUrl } from "@apps/ai_models/local/hub";
import { readParam, writeParams } from "@apps/ai_models/lib/params";
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
import { MenuIcons } from "@platform/ui/MenuIcons";

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
// The arc is drawn with `stroke-dasharray`/`-dashoffset` on a circle rotated a
// quarter turn back, so 0% starts at twelve o'clock. An unmeasured pull — no
// total yet, which is the first second of every one of them and the whole of a
// venv build — spins a fixed quarter-arc instead: a ring frozen at 0 reads as a
// download that has stalled, which is the one thing it is not.
const RING_R = 6.5;
const RING_C = 2 * Math.PI * RING_R;

/** How far a pull has got, 0–1, or null while nothing can divide. The job's
 *  bytes and never the runtime's: only the worker doing the fetching knows.
 *  One function because the ring, the byte line and both titles must not be
 *  able to disagree about the same download. */
function downloadFraction(job?: Job): number | null {
  if (!job || job.unit !== "bytes" || !job.total || job.done === null) return null;
  return Math.min(1, job.done / job.total);
}

function DownloadRing({ job }: { job?: Job }) {
  const measured = downloadFraction(job);
  return (
    <svg
      className={"pg-dl-ring" + (measured === null ? " pg-dl-ring-idle" : "")}
      viewBox="0 0 16 16"
      aria-hidden="true"
    >
      <circle className="pg-dl-ring-track" cx="8" cy="8" r={RING_R} />
      <circle
        className="pg-dl-ring-arc"
        cx="8"
        cy="8"
        r={RING_R}
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
  // `replaceSearch`, which fires only fused:urlchange — the nav epoch counts
  // pushes and pops and would leave a click's own rewrite unread.
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

  // The RAIL's reading order, which this tab now sets for itself: images lead.
  // It diverges from `CAPABILITY_ORDER` (lib/aiModelGroups.ts) deliberately.
  // That list is the order of the tabs that INVENTORY — Models and Benchmark
  // draw one section per capability and must agree with each other — and its
  // reasoning is about where a capability sits in a catalogue. This tab is not
  // a catalogue: it is four things you can DO, and the picture is the one whose
  // result you can judge at a glance, which is what earns it the top of the
  // rail. Text is not demoted for being lesser; it is the one everybody
  // already knows they can have.
  //
  // A capability missing from this list still draws — it sorts after these, in
  // the order the server sent it — so a capability added server-side needs no
  // edit here.
  //
  // This is also what the FALLBACK SELECTION reads, so a bare visit to
  // /ai-models/playground opens on the first section of the rail rather than on
  // whichever capability the server happened to list first. The two were worth
  // separating for exactly one commit — where the sections sit and what the
  // page opens on are different decisions — and the answer to the second one
  // is that a page whose first section is images and whose stage is a chat box
  // is a page arguing with itself. `pickPlaygroundModel` needs no change: its
  // rule was always "the first usable row" (pick.ts), and this is now the order
  // that phrase is about.
  const railRows = useMemo(() => {
    const order = [
      "text-to-image",
      "text-generation",
      "automatic-speech-recognition",
      "embeddings",
    ];
    const rank = (c: string) => {
      const i = order.indexOf(c);
      return i === -1 ? order.length : i;
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
    // cap dies with the first explicit pick — leaving it would make a shared
    // URL claim a task the user has since clicked away from.
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
  // Rows by MODEL, for the sidebar's own size cells — the same title match
  // `jobForSelected` uses one line up, and for the same reason: the job id
  // derivation sanitises characters and a second copy of that rule in
  // TypeScript would drift from the Python one. What a running pull's total does
  // to the size shown is `shared/modelSize`'s rule, not this file's.
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
  // nothing (see the fact itself).
  //
  // The two bad answers are TINTED and the good one is not. Amber and red are
  // the app's warning and error tokens and they are spent here for the reason
  // they exist: "tight" and "no" are the only verdicts that ask the reader to
  // do something differently. "easy" deliberately gets no green — green on this
  // page means RUNNING (the sidebar's live dot, the loaded badge, D461), and a
  // second meaning for one hue on one screen dilutes the one that matters,
  // doubly so when it would appear on nearly every model. Untinted-is-fine is
  // legible precisely because the other two are not.
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
            tone: " pg-fact-tight",
            title:
              "Judged against this machine's memory — close other heavy apps while it runs.",
          }
        : selected.model.fit === "no"
          ? {
              text: "Likely too big for this machine",
              tone: " pg-fact-no",
              title: "Judged against this machine's memory — it may crawl or fail to load.",
            }
          : null;

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
    <div className="pg-body">
      <aside className="pg-side" aria-label="Models to try">
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
              <div
                key={model.id}
                role="button"
                tabIndex={0}
                className={
                  "pg-model" + (active ? " active" : "") +
                  (model.downloaded ? "" : " pg-model-absent")
                }
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
                <span className="pg-model-head">
                  <span className="pg-model-name">
                    {/* Live from the supervisor, not the catalog's `loaded`
                        snapshot — a dot that outlives an unload is a lie. */}
                    {runtime.loaded.some((m) => m.model === model.id && m.state === "ready") && (
                      <span className="pg-model-live" title="Loaded — answering from memory" />
                    )}
                    {name}
                  </span>
                  {/* Parameter count, left of the download weight: the two
                      numbers a reader compares rows by, and they mean
                      different things — how much model, then how much to
                      fetch. Printed as the curator wrote it ("4B", "8B (~1B
                      active)"), never shortened here: catalog.py's AI-2c rule
                      is that this string is a value somebody owns.
                      Absent on a cached entry nobody curated, and the slot
                      then draws nothing rather than a "—" the stage header
                      would have to explain. */}
                  {model.params && (
                    <span className="pg-model-chip" title="Parameters">
                      {model.params}
                    </span>
                  )}
                  <span
                    className="pg-model-size"
                    title={
                      size
                        ? `${size.text} download — judged against this machine's memory`
                        : undefined
                    }
                  >
                    {modelSizeLabel(model.size_gb, job)}
                  </span>
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
                    <button
                      type="button"
                      className="pg-model-dl"
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
                    </button>
                  )}
                </span>
              </div>
            );
          };
          return (
            <details key={row.capability} className="pg-group" open>
              <summary className="pg-group-head">
                <span className="pg-group-icon">{capabilityIcon(row.capability)}</span>
                <span className="pg-group-title">{groupLabel(row.capability)}</span>
              </summary>
              {!row.available && (
                // Visible with its reason, never hidden: an absent group and a
                // ruled-out group look identical, and HF-8 already paid for
                // that lesson once.
                <p className="pg-group-off">{row.reason || "Not available on this machine."}</p>
              )}
              {row.available && !offered.length && (
                // `offered`, not `row.models`: a capability whose whole
                // shortlist is unrecommended and undownloaded has models in
                // the catalog and nothing to draw here, and a silent empty
                // group is the one outcome this filter must not produce. The
                // curation is meant to prevent it (catalog.py keeps at least
                // one recommended entry per list, and a test pins that) — this
                // line is what the failure looks like if it ever slips.
                <p className="pg-group-off">
                  Nothing to try here yet — the Local tab is where a first model comes from.
                </p>
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
            </details>
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
          <details className="pg-group">
            <summary className="pg-group-head">
              <span className="pg-group-icon">{unsupportedIcon()}</span>
              <span className="pg-group-title">Not supported</span>
            </summary>
            <p className="pg-group-off">
              On this disk, and nothing here runs it. The AI Models page is where these
              can be deleted.
            </p>
            {unsupported.map((model) => (
              // A card, so the shape matches the ones above — but a plain div:
              // no role, no tabIndex, no click. There is nothing to select, and
              // a control that looks pressable and is not teaches the wrong
              // thing about every card beside it.
              <div key={model.id} className="pg-model pg-model-off">
                <span className="pg-model-head">
                  <span className="pg-model-name">{model.label}</span>
                  {/* Top-right, as on the selectable cards above — same slot,
                      so the size reads the same however the card behaves.
                      `shared/modelSize`, like every other size cell on this
                      page, with no job: a repo already on the disk is not
                      downloading. Hand-formatting it here would be the second
                      copy of a rule that exists because the copies
                      disagreed. */}
                  <span className="pg-model-size">{modelSizeLabel(model.size_gb)}</span>
                </span>
                <span className="pg-model-full">{model.id}</span>
                {/* What it IS, when the repo said. Null is its own answer and
                    gets no chip: "we could not tell" is what the missing label
                    means, and inventing one would be a claim. */}
                {model.task && (
                  <span className="pg-model-foot">
                    <span className="pg-model-task">{model.task}</span>
                  </span>
                )}
                {/* The server's own sentence, written per task beside the
                    classification it explains (`ai/tasks.py`). Empty for a repo
                    we could not identify — an explanation we have not earned is
                    worse than none — and then the line simply is not drawn. */}
                {model.reason && <p className="pg-model-why">{model.reason}</p>}
              </div>
            ))}
          </details>
        )}
      </aside>

      <div className="pg-stage">
        {actionError && <ErrorBanner>{actionError}</ErrorBanner>}
        {blockedAsk && selected && (
          // Not an ErrorBanner: nothing failed and nothing the user did is
          // wrong — the link simply named a task this machine cannot run, and
          // the stage below is the substitute, said out loud.
          <p className="pg-blocked-ask">
            {groupLabel(blockedAsk.row.capability)} is not available here — {blockedAsk.reason}{" "}
            Showing {groupLabel(selected.row.capability)} instead.
          </p>
        )}
        {!selected ? (
          <p className="cc-empty">
            {blockedAsk
              ? `${groupLabel(blockedAsk.row.capability)} is not available here — ${blockedAsk.reason}`
              : "No models to try yet — the Local tab is where a first one comes from."}
          </p>
        ) : (
          <>
            <section className="pg-hero">
              <div className="pg-hero-head">
                <div className="pg-hero-names">
                  <h3 className="pg-stage-title">{modelName(selected.model)}</h3>
                  {/* The full repo id — author/name as Hugging Face knows it.
                      A link only when it IS a repo id: llama.cpp entries are
                      keyed by bare .gguf filename (formats.GGUF_RECIPES), and
                      huggingface.co/<filename> is a 404 dressed as a link. */}
                  {selected.model.id.includes("/") ? (
                    <a
                      className="pg-hero-repo"
                      href={hubModelUrl(selected.model.id)}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {selected.model.id}
                    </a>
                  ) : (
                    <span className="pg-hero-repo">{selected.model.id}</span>
                  )}
                </div>
                <div className="pg-stage-actions">
                  {/* The playground's exit ramp: everything tried here is one
                      `fused.ai` call in a page, and this hands the /apps
                      composer a seed naming the model, the tuned settings and
                      the call — the user finishes the sentence with the app
                      they want. */}
                  <button
                    type="button"
                    className="btn btn-secondary"
                    title="Open the app builder with this model and your settings pre-filled"
                    onClick={() =>
                      navigateUrl(
                        "/apps?seed=" +
                          encodeURIComponent(buildAppSeed(selected.model, selected.row.capability)),
                      )
                    }
                  >
                    Build an app with this AI
                  </button>
                  {!selected.model.downloaded && !selectedDownloading && (
                    <button type="button" className="btn btn-secondary" onClick={runDownload}>
                      Download{selectedSize ? ` (${selectedSize.text})` : ""}
                    </button>
                  )}
                  {/* The pull, IN THE BUTTON'S OWN SLOT. Pressing Download used
                      to empty this corner — the button's condition excludes a
                      running download — so the one place the eye was already
                      on went blank at the exact moment there was most to say,
                      and the only counting left on the page was the sidebar
                      row's size cell. `ModelProgress` is the drawing every
                      other card on /ai-models uses for this (shared/), reading
                      the same job row: bytes and a bar come from the worker
                      doing the fetching, never from the runtime, which knows
                      only that something is happening. No job row yet — the
                      poll is a second behind the click — is its "Preparing…",
                      not a blank. */}
                  {selectedDownloading && (
                    <div
                      className="pg-hero-dl"
                      title={
                        downloadedFraction !== null
                          ? `Downloading — ${Math.floor(downloadedFraction * 100)}%`
                          : "Downloading…"
                      }
                    >
                      {/* An icon, a ring, and the two byte figures — in the
                          width of a button, which is all this corner has.
                          Deliberately NOT `ModelProgress`, the drawing the
                          /ai-models cards share: that one leads with a pulsing
                          dot and the job's own sentence ("Fetching weights…")
                          and ends in a bar that wants a whole card's width.
                          Here the icon says which kind of work this is without
                          a word, and a ring says how far in the space the
                          sentence wanted. */}
                      <span className="pg-hero-dl-live">
                        <span className="pg-hero-dl-icon" aria-hidden="true">
                          {MenuIcons.download}
                        </span>
                        <DownloadRing job={jobForSelected} />
                        {downloadedBytes && (
                          <span className="pg-hero-dl-bytes">
                            {formatSize(jobForSelected?.done as number)} /{" "}
                            {formatSize(jobForSelected?.total as number)}
                          </span>
                        )}
                      </span>
                      {/* POINT AT THE PROGRESS, GET THE WAY OUT. A download is
                          the one thing on this card a reader changes their mind
                          about, and the figures are what they look at while
                          doing it — so the way to stop it lives under the
                          pointer that is already there, rather than as a fourth
                          button in a row that is only ever read while a
                          download is NOT running.

                          Both drawings share one grid cell, so the box is as
                          wide and as tall as the wider of the two at rest and
                          NOTHING MOVES on hover: a control that reflows the row
                          it appears in is a control the pointer slides off
                          (D452's argument about the recent-chats row, in one
                          box instead of one row).

                          Revealed by `:hover` and by `:focus-within`, and the
                          button stays in the DOM and focusable while invisible
                          — which is what makes the second of those fire, so the
                          way out is reachable by tab and not only by pointer. */}
                      {stoppable && (
                        <button
                          type="button"
                          className="btn btn-secondary pg-hero-dl-stop"
                          title={`Stop downloading ${selected.model.id}`}
                          onClick={() => void runCancelDownload(stoppable)}
                        >
                          Cancel
                        </button>
                      )}
                    </div>
                  )}
                  {selected.model.downloaded && !selectedResident && (
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={runLoad}
                      title="Optional — the first generation loads it too"
                    >
                      Load
                    </button>
                  )}
                  {selectedResident && selectedResident.state === "ready" && (
                    <button type="button" className="btn btn-secondary" onClick={runUnload}>
                      Unload
                    </button>
                  )}
                </div>
              </div>
              {(selected.model.params ||
                selected.model.quantization ||
                selected.model.size_gb != null ||
                fitNote) && (
                <dl className="pg-hero-facts">
                  {selected.model.params && (
                    <div className="pg-hero-fact">
                      <dt>Parameters</dt>
                      <dd>{selected.model.params}</dd>
                    </div>
                  )}
                  {selected.model.quantization && (
                    <div className="pg-hero-fact">
                      <dt>Quantization</dt>
                      <dd>{selected.model.quantization}</dd>
                    </div>
                  )}
                  {selected.model.size_gb != null && (
                    <div className="pg-hero-fact">
                      <dt>Download</dt>
                      <dd>{modelSizeLabel(selected.model.size_gb, jobForSelected)}</dd>
                    </div>
                  )}
                  {/* WILL IT RUN HERE — the server's verdict over the weights
                      and this machine's RAM (`_fit_verdict`), back on the card
                      after the state line that used to carry it was deleted for
                      being prose. A fact beside the others rather than a chip
                      by the name: this page spends its loud colours on what is
                      RUNNING (D461), and a coloured badge here would be the
                      same "quieter card" argument re-lost.

                      ALL THREE answers are drawn, reversing the warning-only
                      first cut. That version had the better argument on paper —
                      "easy" is the common case, and a mark on nearly every
                      model marks nothing (D448's chip) — and it was wrong about
                      what this fact is FOR. It is not a badge decorating a
                      model; it is the answer to "can my machine run this",
                      which is asked OF EVERY MODEL, and a row that answers only
                      when the answer is bad is silent exactly when it is being
                      consulted: on a 34GB laptop every model the sidebar offers
                      is "easy", so the fact was unreachable and read as
                      missing. Absence-as-good-news works for a badge nobody was
                      looking for, not for a question somebody asked. `null`
                      still draws nothing, for the server's own reason: a verdict
                      over a size nobody measured is the lie the "—" size cell
                      exists to avoid.

                      Its own fact, not a line on Download: fetching and
                      running are different questions, and the Download slot is
                      the one that turns into a progress ring mid-pull — which
                      is exactly when "will this even fit" is worth reading. */}
                  {fitNote && (
                    <div className="pg-hero-fact">
                      <dt>Memory</dt>
                      <dd className={"pg-hero-fact-judged" + fitNote.tone} title={fitNote.title}>
                        {fitNote.text}
                      </dd>
                    </div>
                  )}
                </dl>
              )}
              {/* The curator's sentence, in full — the sidebar clamps it.
                  For the zero-jargon reader this is the model introducing
                  itself; the mechanics (loaded, downloading) stay on the
                  quieter line below it. */}
              {selected.model.note && <p className="pg-stage-note">{selected.model.note}</p>}
            </section>
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
              // named, never blank — the same posture the group labels take.
              <p className="cc-empty">
                No playground for {groupLabel(selected.row.capability)} yet — the Local tab can
                still load and manage this model.
              </p>
            )}
            <PlaygroundApps capability={selected.row.capability} modelId={selected.model.id} />
          </>
        )}
      </div>
    </div>
  );
}
