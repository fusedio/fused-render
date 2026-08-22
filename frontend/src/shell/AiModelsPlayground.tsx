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
import { PLAYGROUND_GROUPS } from "@shell/playgroundGroups";
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
import { navigateUrl, replaceSearch } from "@platform/lib/router";
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
const GROUP_BLURBS: Record<string, string> = Object.fromEntries(
  PLAYGROUND_GROUPS.map((g) => [g.capability, g.blurb]),
);

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

/** The seed for the /apps composer: everything the Playground knows about the
 *  moment — which model, what it is good for, the settings the user dialled in
 *  (read off the URL, where every non-default already lives), and the page API
 *  that reaches it (`runtime.js`'s names — the seed is read by an app AUTHOR's
 *  session, and camelCase is that API's vocabulary). Ends mid-sentence on
 *  purpose: the user finishes it with what they actually want built. */
export function buildAppSeed(model: AiCatalogModel, capability: string): string {
  const name = model.nickname || model.label;
  const lines: string[] = [
    `Build a fused app around the local AI model "${name}" (${model.id}) — it runs fully offline on this machine.`,
  ];
  if (model.note) lines.push(`About this model: ${model.note}`);
  const opts = (pairs: [string, string | null][]) =>
    pairs
      .filter((p): p is [string, string] => p[1] !== null && p[1] !== "")
      .map(([k, v]) => `, ${k}: ${v}`)
      .join("");
  if (capability === "text-generation") {
    const extra = opts([
      ["temperature", readParam("temp")],
      ["topP", readParam("topp")],
      ["maxTokens", readParam("maxtok")],
      ["systemPrompt", readParam("system") ? JSON.stringify(readParam("system")) : null],
    ]);
    lines.push(
      "It generates text. Call it from the page with " +
        `fused.ai(prompt, { model: ${JSON.stringify(model.id)}${extra}, history, onChunk }) — ` +
        "it streams tokens through onChunk and resolves with { text, usage }. " +
        (extra ? "The options above are the settings I tuned in the Playground." : ""),
    );
  } else if (capability === "text-to-image") {
    const extra = opts([
      ["width", readParam("w")],
      ["height", readParam("h")],
      ["steps", readParam("steps") ?? (model.defaults?.steps != null ? String(model.defaults.steps) : null)],
      ["guidance", readParam("guidance")],
      ["seed", readParam("seed")],
    ]);
    lines.push(
      "It turns a text prompt into a picture. Call it from the page with " +
        `await fused.ai.image({ prompt, model: ${JSON.stringify(model.id)}${extra}, onProgress }) — ` +
        "it resolves with { url, seed, ... } and url renders straight into an <img>. " +
        (extra ? "The options above are the settings I tuned in the Playground." : ""),
    );
  } else if (capability === "automatic-speech-recognition") {
    const extra = opts([
      ["task", readParam("task") ? JSON.stringify(readParam("task")) : null],
      ["language", readParam("lang") ? JSON.stringify(readParam("lang")) : null],
      ["vad", readParam("vad") === "0" ? "false" : null],
      ["words", readParam("words") === "1" ? "true" : null],
    ]);
    lines.push(
      "It turns speech into text. Call it from the page with " +
        `await fused.ai.transcribe({ path, model: ${JSON.stringify(model.id)}${extra}, onSegment }) — ` +
        "path is an audio/video file on disk, segments stream through onSegment. " +
        (extra ? "The options above are the settings I tuned in the Playground." : ""),
    );
  }
  // Addressed to the CLAUDE SESSION the composer spawns, not to the user: the
  // `fused-render-ai` skill is the authoritative contract for these calls
  // (streaming shapes, the model_loading retry dance, error types, export
  // rules), and a seed that names the API without pointing at the contract
  // invites the session to improvise it.
  lines.push(
    "",
    "Before writing any AI code, load the `fused-render-ai` skill — it documents the " +
      "fused.ai contract: streaming, model loading and download progress, every error " +
      "type and how a page should respond, and what an exported app may call.",
    "",
    "The app I want: ",
  );
  return lines.join("\n");
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
  // `?cap=` names a capability, not a model — the Home strip's cards land
  // here with only a task in mind. It only steers the fallback: an explicit
  // `model` always wins, and an unknown cap value falls through silently.
  const askedCap = useMemo(() => readParam("cap"), [urlVersion]);
  const selected = useMemo(() => {
    for (const row of capabilities) {
      const hit = row.models.find((m) => m.id === asked);
      if (hit) return { row, model: hit };
    }
    const rows = [
      ...capabilities.filter((r) => r.capability === askedCap),
      ...capabilities,
    ];
    for (const row of rows) {
      const fallback =
        row.models.find((m) => m.id === row.default) ?? row.models[0];
      if (fallback) return { row, model: fallback };
    }
    return null;
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
            ? `Not downloaded — ${selected.model.size_gb} GB to fetch.` +
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
                  <span>{resident ? "Ready" : model.downloaded ? "On this machine" : ""}</span>
                  {/* The size, translated: "will this melt my laptop" is the
                      question a newcomer is actually asking of a GB figure,
                      so the verdict leads and the number stays for hover. */}
                  <span
                    className={"pg-fit" + (model.fit ? " " + model.fit : "")}
                    title={
                      model.size_gb != null
                        ? `${model.size_gb} GB download — judged against this machine's memory`
                        : undefined
                    }
                  >
                    {model.size_gb != null ? `${model.size_gb} GB` : "—"}
                    {model.fit === "easy"
                      ? " · runs easily"
                      : model.fit === "tight"
                        ? " · tight fit"
                        : model.fit === "no"
                          ? " · too big here"
                          : ""}
                  </span>
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
