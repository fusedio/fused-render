// The Playground tab: pick a local model on the left, use it on the right.
//
// Everything else on /ai-models is ABOUT models — what is on disk, what could
// be, which backend serves it. This tab is the one that answers the first
// question a person has ("what can this thing actually do?") by letting them
// do it: a chat box for a text model, a prompt-to-picture stage for an image
// model, a record-and-transcribe stage for a speech model. The stage is chosen
// by the selected model's capability, so a capability added server-side gets a
// named placeholder here rather than a blank.
//
// The sidebar is `GET /api/ai/catalog`, verbatim — the same payload every
// page's model picker reads (D323), so a model downloaded from Discover is in
// the playground with no curation edit. Rows show the curated `nickname`
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
import { PlaygroundChat } from "./PlaygroundChat";
import { PlaygroundImage } from "./PlaygroundImage";
import { PlaygroundTranscribe } from "./PlaygroundTranscribe";
import { ModelProgress } from "./AiProgress";
import { capabilityLabel } from "@shell/engines";
import { isBusy, refreshAiRuntime, useAiRuntime } from "./aiRuntime";
import {
  downloadAiModel,
  getAiCatalog,
  loadAiModel,
  unloadAiModel,
  type AiCatalogCapability,
  type AiCatalogModel,
} from "@platform/lib/api";
import { fetchJobs, type Job } from "@platform/lib/jobs";
import { useUrlVersion } from "@platform/lib/hooks";
import { replaceSearch } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

// What the groups are called HERE: the capability vocabulary is exact
// ("automatic-speech-recognition") and `capabilityLabel` is faithful to it
// ("Speech to text") — this tab is the one surface named for what a person
// DOES, so it gets the doing words. An unknown capability falls back to the
// shared label, so a new runner appears (plainly named) instead of vanishing.
const GROUP_LABELS: Record<string, string> = {
  "text-generation": "Chat",
  "text-to-image": "Images",
  "automatic-speech-recognition": "Transcription",
};

// One plain sentence under each group title, for the reader this tab exists
// for — someone with no AI vocabulary at all. It says what the models DO, in
// the words of the task, never of the technology.
const GROUP_BLURBS: Record<string, string> = {
  "text-generation": "Ask questions, write and rewrite text.",
  "text-to-image": "Turn a description into a picture.",
  "automatic-speech-recognition": "Turn speech into written words.",
};

function groupLabel(capability: string): string {
  return GROUP_LABELS[capability] ?? capabilityLabel(capability);
}

/** The display name everywhere on this tab: the curated nickname, or the label
 *  for a cached entry nobody curated. A fallback read, never a derivation. */
export function modelName(model: AiCatalogModel): string {
  return model.nickname || model.label;
}

/** Read one query param off the CURRENT url. */
export function readParam(key: string): string | null {
  return new URLSearchParams(location.search).get(key);
}

/** A numeric param, defensively: a shared link is exactly where a malformed or
 *  empty value arrives, and `Number("")` is 0 — a temperature nobody chose. */
export function numParam(key: string, fallback: number): number {
  const raw = readParam(key);
  if (raw === null || raw.trim() === "") return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : fallback;
}

/** Rewrite query params in place — null deletes. `replaceSearch`, not
 *  navigate: model browsing and slider drags must not stack history entries. */
export function writeParams(updates: Record<string, string | null>): void {
  const params = new URLSearchParams(location.search);
  for (const [key, value] of Object.entries(updates)) {
    if (value === null) params.delete(key);
    else params.set(key, value);
  }
  const search = params.toString();
  replaceSearch(location.pathname + (search ? "?" + search : ""));
}

type CatalogLoad =
  | { status: "loading" }
  | { status: "ok"; capabilities: AiCatalogCapability[] }
  | { status: "error"; message: string };

export default function AiModelsPlayground() {
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

  // Job rows while anything is live, so the header can draw the same progress
  // the Local tab draws — matched by TITLE (the supervisor sets it to the
  // model id), the same join AiModels.tsx uses and for the same reason: the id
  // derivation sanitises characters and must not be copied here.
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
  const selected = useMemo(() => {
    for (const row of capabilities) {
      const hit = row.models.find((m) => m.id === asked);
      if (hit) return { row, model: hit };
    }
    for (const row of capabilities) {
      const fallback =
        row.models.find((m) => m.id === row.default) ?? row.models[0];
      if (fallback) return { row, model: fallback };
    }
    return null;
  }, [capabilities, asked]);

  const select = (id: string) => {
    setActionError(null);
    writeParams({ model: id });
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

  const runDownload = async () => {
    if (!selected) return;
    setActionError(null);
    try {
      await downloadAiModel(selected.model.id, selected.row.capability);
      refreshAiRuntime();
    } catch (e) {
      setActionError((e as Error).message);
    }
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
          : selected.model.size_gb != null
            ? `Not downloaded — ${selected.model.size_gb} GB to fetch.`
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
          // The same recommendation foundation Discover's "Suggested models"
          // grid renders: the catalog's CURATED half, in the catalog's own
          // smallest-first order, notes and all. The uncurated repos this disk
          // happens to hold (D323's union) are still playable but sit apart
          // under their own quiet caption — they have no curator and no note,
          // and mixed in they read as recommendations nobody made.
          const curated = row.models.filter((m) => m.source === "curated");
          const cached = row.models.filter((m) => m.source !== "curated");
          const draw = (model: AiCatalogModel) => {
            const active = selected?.model.id === model.id;
            const resident = runtime.loaded.some(
              (m) => m.model === model.id && m.state === "ready",
            );
            return (
              <button
                type="button"
                key={model.id}
                className={"pg-model" + (active ? " active" : "")}
                aria-pressed={active}
                onClick={() => select(model.id)}
                title={model.label}
              >
                <span className="pg-model-name">
                  {resident ? (
                    <span className="pg-dot loaded" aria-hidden="true" />
                  ) : model.downloaded ? (
                    <span className="pg-dot disk" aria-hidden="true" />
                  ) : null}
                  {modelName(model)}
                </span>
                {/* The curator's sentence — why you would pick this one. The
                    same `note` Discover prints on its suggestion cards; for
                    the reader with no AI vocabulary it is the only line here
                    that answers "which one do I click". */}
                {model.note && <span className="pg-model-note">{model.note}</span>}
                <span className="pg-model-meta">
                  <span>{resident ? "Loaded" : model.downloaded ? "On this machine" : ""}</span>
                  <span>{model.size_gb != null ? `${model.size_gb} GB` : "—"}</span>
                </span>
              </button>
            );
          };
          return (
            <section key={row.capability} className="pg-group">
              <h4 className="pg-group-title">{groupLabel(row.capability)}</h4>
              {GROUP_BLURBS[row.capability] && (
                <p className="pg-group-blurb">{GROUP_BLURBS[row.capability]}</p>
              )}
              {!row.available && (
                // Visible with its reason, never hidden: an absent group and a
                // ruled-out group look identical, and HF-8 already paid for
                // that lesson once.
                <p className="pg-group-off">{row.reason || "Not available on this machine."}</p>
              )}
              {row.available && !row.models.length && (
                <p className="pg-group-off">Nothing to suggest yet.</p>
              )}
              {row.available && curated.map(draw)}
              {row.available && cached.length > 0 && (
                <>
                  <p className="pg-side-cap">Your downloads</p>
                  {cached.map(draw)}
                </>
              )}
            </section>
          );
        })}
      </aside>

      <div className="pg-stage">
        {actionError && <ErrorBanner>{actionError}</ErrorBanner>}
        {!selected ? (
          <p className="cc-empty">
            No models to try yet — the Discover tab is where a first one comes from.
          </p>
        ) : (
          <>
            <div className="pg-stage-head">
              <div>
                <h3 className="pg-stage-title">
                  {modelName(selected.model)}
                  <span className="pg-stage-kind"> · {groupLabel(selected.row.capability)}</span>
                </h3>
                {/* The curator's sentence, in full — the sidebar clamps it.
                    For the zero-jargon reader this is the model introducing
                    itself; the mechanics (loaded, downloading) stay on the
                    quieter line below it. */}
                {selected.model.note && <p className="pg-stage-note">{selected.model.note}</p>}
                <p className="pg-stage-state">
                  {stateLine}
                  {evicts ? ` ${evicts}` : ""}
                </p>
              </div>
              <div className="pg-stage-actions">
                {!selected.model.downloaded && !selectedDownloading && (
                  <button type="button" className="btn btn-secondary" onClick={runDownload}>
                    Download{selected.model.size_gb != null ? ` (${selected.model.size_gb} GB)` : ""}
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
            {(selectedDownloading || (selectedResident && selectedResident.state !== "ready")) && (
              <ModelProgress detail={selectedResident?.detail} job={jobForSelected} />
            )}
            {selected.row.capability === "text-generation" ? (
              <PlaygroundChat
                key={selected.model.id}
                model={selected.model.id}
                modelLabel={modelName(selected.model)}
                ready={!!selectedResident && selectedResident.state === "ready"}
                downloaded={selected.model.downloaded}
              />
            ) : selected.row.capability === "text-to-image" ? (
              <PlaygroundImage
                key={selected.model.id}
                model={selected.model.id}
                entry={selected.model}
              />
            ) : selected.row.capability === "automatic-speech-recognition" ? (
              <PlaygroundTranscribe key={selected.model.id} model={selected.model.id} />
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
