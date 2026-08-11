// The Discover half of /ai-models: what the Hugging Face Hub has, told apart
// from what this disk already holds.
//
// Two things make this worth having inside the app rather than in a browser
// tab. First, the JOIN: huggingface.co cannot tell you that the model you are
// reading about is already in your cache, was last read three weeks ago, and
// would cost nothing to open — this page can, because its sibling tab already
// measured exactly that. Second, the SIZE: a cache fills up with multi-GB
// checkpoints nothing on screen mentions, so "≈16 GB" belongs next to a model's
// name before anyone decides to fetch it, not after.
//
// **Downloading now lives here** (SPEC §40, D258). The read-only posture this
// tab shipped with was not squeamishness — it was that a download needs a
// progress surface, a cancel, and somewhere to put a half-finished pull, and
// none of that existed yet. It does now: the app runs the download, the manager
// shows it, and the ✕ really stops it.
//
// **Nothing reaches the network until this tab is open.** The app is a local
// file explorer; a page that quietly queried a third party on mount would be a
// surprise. Selecting Discover is the consent, the caption says which host is
// being asked, and the query is debounced so a burst of typing is one request.
import { useEffect, useRef, useState } from "react";
import {
  downloadAiModel,
  getAiCatalog,
  getHubTasks,
  searchHubModels,
  type AiCatalogCapability,
  type HubModel,
  type HubSort,
  type HubTask,
} from "@platform/lib/api";
import { refreshAiRuntime } from "./aiRuntime";
import { ModelProgress } from "./AiProgress";
import type { Job } from "@platform/lib/jobs";
import { formatSize, formatParams, timeAgo } from "@platform/lib/format";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

// Long enough that a typed word is one request rather than five, short enough
// that the results feel like they are following the query.
const DEBOUNCE_MS = 350;

const SORTS: { value: HubSort; label: string; title: string }[] = [
  { value: "downloads", label: "Downloads", title: "Most downloaded in the last month" },
  { value: "likes", label: "Likes", title: "Most liked on the Hub" },
  { value: "updated", label: "Updated", title: "Changed most recently" },
  { value: "created", label: "New", title: "Published most recently" },
];

function count(n: number | null): string | null {
  if (n === null || n === undefined) return null;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
}

function HubCard({ model }: { model: HubModel }) {
  const local = model.local;
  // Only a COMPLETE download opens locally. "partial" means blobs with no
  // materialised snapshot, so there is no revision for the model card to
  // describe — linking there would hand someone a view that cannot load, which
  // is the same mistake as calling the state "downloaded" in the first place.
  const here = local.state === "downloaded";
  const downloads = count(model.downloads);
  const likes = count(model.likes);
  // The Hub sends an ISO timestamp; timeAgo works in epoch seconds. An
  // unparseable one is a field the card leaves out, not a "NaN ago".
  const updatedAt = model.updated ? Date.parse(model.updated) : NaN;
  const updated = Number.isFinite(updatedAt) ? timeAgo(updatedAt / 1000) : null;

  return (
    <div className="cc-mdcard am-card am-hubcard">
      <div className="cc-mdcard-head">
        {/* The name goes to the Hub, downloaded or not — the same rule the
            cached cards follow, so a model's name means one destination
            everywhere on this page. Opening a copy you already have is the
            footer's "Explore". */}
        <a
          className="cc-mdcard-name am-card-name"
          href={model.url}
          target="_blank"
          rel="noopener noreferrer"
          title={`Open ${model.id} on the Hub`}
        >
          {model.id}
        </a>
        {model.gated && (
          <span
            className="cc-pill am-hub-gated"
            title="Gated: its licence has to be accepted on the Hub before this can be downloaded."
          >
            gated
          </span>
        )}
        <span className="am-card-size" title={sizeTitle(model)}>
          {model.estimatedSize ? `≈${formatSize(model.estimatedSize)}` : "—"}
        </span>
      </div>

      {(model.task || model.params || model.library) && (
        <div className="am-card-what">
          {model.task && (
            // The same sentence the cached cards hover with, from the same
            // table — a task means one thing across this page or it means
            // nothing.
            <span className="am-card-task" title={model.taskHelp ?? undefined}>
              {model.task}
            </span>
          )}
          {model.params !== null && (
            <span className="am-card-params" title={`${model.params.toLocaleString()} parameters`}>
              {formatParams(model.params)} params
            </span>
          )}
          {model.library && <span className="am-card-library">{model.library}</span>}
        </div>
      )}

      <div className="cc-mdcard-foot">
        <span className="cc-mdcard-meta">
          {downloads ? `${downloads} downloads` : null}
          {downloads && likes ? " · " : null}
          {likes ? `${likes} likes` : null}
          {(downloads || likes) && updated ? " · " : null}
          {updated ? `updated ${updated}` : null}
        </span>
        <span className="am-hub-state">
          {/* Only a COMPLETE download can be explored: "partial" means blobs
              with no materialised snapshot, so there is no revision for the
              model card to describe. */}
          {here && local.path && (
            <a
              className="am-card-explore"
              // The same URL the cached tab's Explore builds — a raw "#" + path
              // drops the mode, so a middle-click would land on the folder
              // listing rather than the model card.
              href={urlForFsPath(local.path, "?_mode=model_card")}
              title={`Explore ${model.id} here — ${local.path}`}
              onClick={(e) => {
                if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
                  return;
                e.preventDefault();
                navigate(local.path as string, { isDir: true, mode: "model_card" });
              }}
            >
              Explore
            </a>
          )}
          {local.state === "downloaded" && (
            <span
              className="am-hub-here"
              title={
                `Already in your cache${local.size ? ` — ${formatSize(local.size)} on disk` : ""}` +
                (local.lastUsed ? `, last read ${timeAgo(local.lastUsed)}` : "")
              }
            >
              on this machine
            </span>
          )}
          {local.state === "partial" && (
            <span
              className="am-hub-partial"
              title="A repo folder for this exists but has no complete revision — an interrupted download."
            >
              partly downloaded
            </span>
          )}
        </span>
      </div>
    </div>
  );
}

function sizeTitle(model: HubModel): string | undefined {
  if (!model.estimatedSize) {
    return "No safetensors metadata on the Hub for this repo, so its size can't be computed here.";
  }
  // The "≈" is doing real work: the bytes are recovered from the dtype →
  // parameter-count map the Hub publishes, which is the weights and not the
  // tokenizer, configs or extra formats sitting beside them.
  return (
    `≈${formatSize(model.estimatedSize)} of weights, computed from the parameter counts the Hub ` +
    "publishes. Other files in the repo are not included."
  );
}

// The curated shortlist, per capability, with what this machine can serve.
//
// These lists used to live inside the apps that used them — three MLX models in
// local_chat, one FLUX model hard-coded in the image worker — which put the
// curation where nobody browsing for a model would ever see it.
function Suggested({
  catalog,
  onDisk,
  downloading,
  jobByModel,
}: {
  catalog: AiCatalogCapability[];
  /** Repo ids with a MATERIALISED snapshot on this disk, or null while the walk
   *  is still running. Owned by the page, so both tabs mean one thing by it. */
  onDisk: Set<string> | null;
  downloading: Set<string>;
  jobByModel: Map<string, Job>;
}) {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The click is held until the RUNTIME confirms the pull, not until the POST
  // returns. Clearing on the reply put the card back to "Download" for the beat
  // before the next runtime poll, which reads as the button having done nothing
  // — and `onDisk` covers the other end, a "download" that was a cache hit and
  // finished before any poll ever saw it.
  const settled = pending !== null && (downloading.has(pending) || !!onDisk?.has(pending));
  useEffect(() => {
    if (settled) setPending(null);
  }, [settled]);
  if (!catalog.length) return null;

  const start = async (model: string, capability: string) => {
    setError(null);
    setPending(model);
    try {
      await downloadAiModel(model, capability);
      // The pull is the server's now. Asking the runtime for a fresh read is the
      // whole follow-up: the card's state comes from what is actually happening,
      // never from the fact that a button was pressed.
      refreshAiRuntime();
    } catch (e) {
      setError((e as Error).message);
      setPending(null);
    }
  };

  return (
    <section className="am-suggested">
      <h3 className="am-suggested-head">Suggested</h3>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {catalog.map((group) => (
        <div key={group.capability} className="am-suggested-group">
          <div className="am-suggested-cap">
            {group.capability.replace(/-/g, " ")}
            {/* Shown even when it cannot run here, with the reason: hiding a
                capability leaves someone hunting for a feature that never was. */}
            {!group.available && (
              <span className="am-suggested-why" title={group.reason ?? undefined}>
                unavailable — {group.reason}
              </span>
            )}
          </div>
          <div className="cc-mdgrid am-grid">
            {group.models.map((m) => {
              // Three states, and keeping them apart is the whole fix: HERE (a
              // materialised snapshot), COMING (a pull the server is running),
              // and neither. The bug this replaces collapsed the last two —
              // pressing Download made the card claim "✓ downloaded" while the
              // 4.6GB pull it had just started was on its first byte.
              const busy = downloading.has(m.id) || pending === m.id;
              const have = !!onDisk?.has(m.id);
              return (
                <div key={m.id} className="cc-mdcard am-card am-suggestcard">
                  <div className="cc-mdcard-head">
                    <a
                      className="cc-mdcard-name am-card-name"
                      href={`https://huggingface.co/${m.id}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={`Open ${m.id} on the Hub`}
                    >
                      {m.label}
                    </a>
                    {have && !busy && (
                      <span className="am-suggest-have" title={`${m.id} is already on this machine`}>
                        ✓ downloaded
                      </span>
                    )}
                    <span className="am-card-size">{m.size_gb} GB</span>
                  </div>
                  <div className="am-suggest-note">{m.note}</div>
                  {/* No `detail` override: the job says what it is doing
                      ("Fetching weights…", "Preparing MLX…") and a fixed word
                      here would paper over a venv build with "Downloading". */}
                  {busy && <ModelProgress job={jobByModel.get(m.id)} />}
                  <div className="cc-mdcard-foot">
                    <span className="cc-mdcard-meta cc-mono">{m.id}</span>
                    {!have && !busy && group.available && (
                      <button
                        type="button"
                        className="am-card-power"
                        onClick={() => start(m.id, group.capability)}
                        title={`Download ${m.id} (~${m.size_gb} GB)`}
                      >
                        Download
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </section>
  );
}

export default function AiModelsDiscover({
  onDisk,
  downloading,
  jobByModel,
}: {
  onDisk: Set<string> | null;
  downloading: Set<string>;
  jobByModel: Map<string, Job>;
}) {
  const [query, setQuery] = useState("");
  const [task, setTask] = useState("");
  const [sort, setSort] = useState<HubSort>("downloads");
  const [tasks, setTasks] = useState<HubTask[]>([]);
  const [catalog, setCatalog] = useState<AiCatalogCapability[]>([]);
  const [models, setModels] = useState<HubModel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [endpoint, setEndpoint] = useState<string | null>(null);
  // Bumped by the debounce so the fetch effect re-runs on a settled query
  // rather than on every keystroke.
  const [settled, setSettled] = useState({ q: "", task: "", sort: "downloads" as HubSort });
  const timer = useRef<number | null>(null);

  useEffect(() => {
    // The filter list is small, static and shared with the cached tab's
    // vocabulary. A failure here is not worth a banner — the search still
    // works, it just has no task menu.
    getHubTasks().then(
      (d) => setTasks(d.tasks),
      () => setTasks([]),
    );
  }, []);

  useEffect(() => {
    // Just the curation. Whether each entry is on this disk is the PAGE's
    // answer, arriving as `onDisk` — this used to run its own cache walk beside
    // the page's, which meant two definitions of "downloaded" and two moments
    // they were true. (It also awaited a one-result Hub search it then threw
    // away, so a machine with no network showed no suggestions at all.)
    getAiCatalog().then(
      (cat) => setCatalog(cat.capabilities),
      () => setCatalog([]),
    );
  }, []);

  useEffect(() => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setSettled({ q: query, task, sort }), DEBOUNCE_MS);
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, [query, task, sort]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    searchHubModels({ q: settled.q, task: settled.task, sort: settled.sort, limit: 24 }).then(
      (data) => {
        if (!alive) return;
        setLoading(false);
        // A reachable server that could not reach the Hub answers 200 with an
        // `error` — the request was fine, the far side was not, and the
        // difference is worth keeping.
        setError(data.error ?? null);
        setModels(data.models);
        setEndpoint(data.endpoint ?? null);
      },
      (e: Error) => {
        if (!alive) return;
        setLoading(false);
        setError(e.message);
      },
    );
    return () => {
      alive = false;
    };
  }, [settled]);

  const host = endpoint ? endpoint.replace(/^https?:\/\//, "") : "huggingface.co";

  return (
    <>
      {/* Suggested first, and only with no query: a curated handful is the
          answer to "what should I even get", which is the question someone has
          BEFORE they know what to type. Once they type, they have a better one. */}
      {!settled.q && !settled.task && (
        <Suggested
          catalog={catalog}
          onDisk={onDisk}
          downloading={downloading}
          jobByModel={jobByModel}
        />
      )}

      <div className="am-hub-controls">
        <input
          className="am-hub-search"
          type="search"
          value={query}
          placeholder="Search models on the Hub…"
          aria-label="Search models on the Hugging Face Hub"
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="am-hub-select"
          value={task}
          aria-label="Filter by task"
          onChange={(e) => setTask(e.target.value)}
          title={tasks.find((t) => t.tag === task)?.help ?? "Show only models for one kind of job"}
        >
          <option value="">Any task</option>
          {tasks.map((t) => (
            <option key={t.tag} value={t.tag} title={t.help ?? undefined}>
              {t.label}
            </option>
          ))}
        </select>
        <select
          className="am-hub-select"
          value={sort}
          aria-label="Sort results"
          onChange={(e) => setSort(e.target.value as HubSort)}
        >
          {SORTS.map((s) => (
            <option key={s.value} value={s.value} title={s.title}>
              {s.label}
            </option>
          ))}
        </select>
      </div>
      {/* Said plainly, once: this tab is the one place in the app that asks a
          third party a question. */}
      <p className="cc-caption am-hub-note">
        Searching {host}. Search results are read-only; the suggestions above can be downloaded.
      </p>

      {error && <ErrorBanner>{error}</ErrorBanner>}
      {loading && models === null && <p className="cc-empty">Asking {host}…</p>}
      {models !== null && models.length === 0 && !error && (
        <p className="cc-empty">
          {settled.q || settled.task
            ? "Nothing on the Hub matches that."
            : "The Hub returned no models."}
        </p>
      )}
      {models !== null && models.length > 0 && (
        <div className={"cc-mdgrid am-grid" + (loading ? " am-hub-stale" : "")}>
          {models.map((m) => (
            <HubCard key={m.id} model={m} />
          ))}
        </div>
      )}
    </>
  );
}
