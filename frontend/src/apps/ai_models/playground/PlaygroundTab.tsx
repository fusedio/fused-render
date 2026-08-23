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
import { TranscribeStage } from "./TranscribeStage";
import { EmbedStage } from "./EmbedStage";
import { modelSizeHint, modelSizeLabel } from "@apps/ai_models/shared/modelSize";
import { capabilityLabel } from "@apps/ai_models/lib/engines";
import { PLAYGROUND_GROUPS } from "./groups";
import { buildAppSeed, modelName } from "./appSeed";
import { capabilityIcon } from "./capabilityIcons";
import { pickPlaygroundModel, playgroundModels } from "./pick";
import { hubModelUrl } from "@apps/ai_models/local/hub";
import { readParam, writeParams } from "@apps/ai_models/lib/params";
import { isBusy, refreshAiRuntime, useAiRuntime } from "@apps/ai_models/lib/aiRuntime";
import { fetchJobs, type Job } from "@platform/lib/jobs";
import {
  downloadAiModel,
  getAiCatalog,
  loadAiModel,
  unloadAiModel,
  type AiCatalogCapability,
  type AiCatalogModel,
} from "@platform/lib/api";
import { useUrlVersion } from "@platform/lib/hooks";
import { navigateUrl } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

// What the groups are called HERE: the capability vocabulary is exact
// ("automatic-speech-recognition") and `capabilityLabel` is faithful to it
// ("Speech to text") — this tab is the one surface named for what a person
// DOES, so it gets the doing words (PLAYGROUND_GROUPS, shared with the Home
// strip). An unknown capability falls back to the shared label, so a new
// runner appears (plainly named) instead of vanishing.
const GROUP_LABELS: Record<string, string> = Object.fromEntries(
  PLAYGROUND_GROUPS.map((g) => [g.capability, g.label]),
);
function groupLabel(capability: string): string {
  return GROUP_LABELS[capability] ?? capabilityLabel(capability);
}


type CatalogLoad =
  | { status: "loading" }
  | { status: "ok"; capabilities: AiCatalogCapability[] }
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
      (data) => alive && setCatalog({ status: "ok", capabilities: data.capabilities }),
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

  // The selection lives in the URL. An unknown or absent id falls back to the
  // first capability's default silently (PT-9's posture: a stale link opens
  // the page, not an error) — and the fallback is `default`, never models[0],
  // which catalog.py's ordering rule makes the smallest vetted model.
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
    () => pickPlaygroundModel(capabilities, asked, askedCap),
    [capabilities, asked, askedCap],
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

  // The state line under the model name: what is TRUE right now, in words. The
  // sidebar dots carry the same facts; this is where they are spelled out.
  const stateLine = !selected
    ? ""
    : selectedResident
      ? selectedResident.state === "ready"
        ? "Loaded — answering from memory."
        : selectedResident.detail || "Loading…"
      : selectedDownloading
        ? "Downloading…"
        : selected.model.downloaded
          ? "Downloaded — loads on first use."
          : selectedSize
            ? `Not downloaded — ${selectedSize.text} to fetch.` +
              // The fit verdict, spelled out where the Download decision is
              // being made — the badge says "too big here", this says why.
              (selected.model.fit === "no"
                ? " This one is likely too big for this machine's memory — it may crawl or fail to load."
                : selected.model.fit === "tight"
                  ? " A tight fit for this machine — close other heavy apps while it runs."
                  : "")
            : "Not downloaded.";

  // Eviction is reversible, so it is a sentence rather than a modal — but it
  // must be said BEFORE the click that does it (AI-4): one resident model per
  // capability, and using this one stops that one.
  const evicts =
    selected && residentRow && residentRow.model !== selected.model.id
      ? `Using this stops ${residentRow.model.split("/").pop()}.`
      : null;

  return (
    <div className="pg-body">
      <aside className="pg-side" aria-label="Models to try">
        {capabilities.map((row) => {
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
            // The full name lives in the tooltip now — the row is one line, and
            // for a cached entry (where the label IS the display name) the
            // tooltip shows the repo id so it never just repeats the row.
            const fullName = model.label !== name ? model.label : model.id !== name ? model.id : null;
            // The row is a div-as-button, not a <button>: the Download CTA
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
                className={"pg-model" + (active ? " active" : "")}
                aria-pressed={active}
                onClick={() => select(model.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    select(model.id);
                  }
                }}
                title={fullName ?? model.label}
              >
                <span className="pg-model-name">
                  {/* Live from the supervisor, not the catalog's `loaded`
                      snapshot — a dot that outlives an unload is a lie. */}
                  {runtime.loaded.some((m) => m.model === model.id && m.state === "ready") && (
                    <span className="pg-model-live" title="Loaded — answering from memory" />
                  )}
                  {name}
                </span>
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
                    is an action to take. */}
                {!model.downloaded && (
                  <button
                    type="button"
                    className="pg-model-dl"
                    disabled={downloading}
                    onClick={(e) => {
                      // Selecting too is fine; a second click must not be.
                      e.stopPropagation();
                      select(model.id);
                      void runDownloadFor(model.id, row.capability);
                    }}
                  >
                    {downloading ? "Downloading…" : "Download"}
                  </button>
                )}
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
                  Nothing to try here yet — the Discover tab is where a first model comes from.
                </p>
              )}
              {row.available && curated.map(draw)}
              {row.available && cached.length > 0 && (
                <>
                  <p className="pg-side-cap">Your downloads</p>
                  {cached.map(draw)}
                </>
              )}
            </details>
          );
        })}
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
                  {/* No capability word or icon beside the name — the sidebar
                      group the row sits under already says it. */}
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
                    className="btn btn-primary"
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
                selected.model.size_gb != null) && (
                // One quiet line, not a labelled table: these three read fine
                // bare ("1.2B · MLX 4-bit · 668 MB") and the table treatment
                // outweighed the composer below it.
                <p className="pg-hero-meta">
                  {[
                    selected.model.params,
                    selected.model.quantization,
                    selected.model.size_gb != null
                      ? modelSizeLabel(selected.model.size_gb, jobForSelected)
                      : null,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              )}
              {/* The curator's sentence, in full — the sidebar clamps it.
                  For the zero-jargon reader this is the model introducing
                  itself; the mechanics (loaded, downloading) stay on the
                  quieter line below it. */}
              {selected.model.note && <p className="pg-stage-note">{selected.model.note}</p>}
              <p className="pg-stage-state">
                {stateLine}
                {evicts ? ` ${evicts}` : ""}
              </p>
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
          </>
        )}
      </div>
    </div>
  );
}
