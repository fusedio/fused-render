// The Discover half of /ai-models: models this machine can run, whether they
// come from the curated shortlist or from a search of the Hub.
//
// Two things make this worth having inside the app rather than in a browser
// tab. First, the JOIN: huggingface.co cannot tell you that the model you are
// reading about is already in your cache, was last read three weeks ago, and
// would cost nothing to open — this page can, because its sibling tab already
// measured exactly that. Second, the SIZE: a cache fills up with multi-GB
// checkpoints nothing on screen mentions, so "≈16 GB" belongs next to a model's
// name before anyone decides to fetch it, not after.
//
// **Everything on this tab is ACTIONABLE, and that is what the tab is for**
// (D313, narrowed by D316). It used to be two features stacked: a curated list
// you could download from, and under it a Hub search whose results were
// read-only — with a caption admitting as much. The search returned whatever
// the Hub returned, which on a page that runs four kinds of model meant
// embedding models and fill-mask models, none of which could be acted on. The
// search is still here, because "what is out there" is a real question the
// shortlist cannot answer; what changed is that the server now constrains it to
// repos a registered runner can load (`routers/hub_models.py`), so a result is
// a card with a working Download button rather than a link to somewhere else.
//
// A GATED repo is a result, and that is the one place the rule bends on
// purpose: a licence you accept by signing in is a step the user can take, so
// the card names the gate and offers the way through it (`gateChrome`) rather
// than the search pretending the model is not there. Private repos still go —
// nothing an ordinary account does reaches one.
//
// **The two are one surface, not two.** The search box is at the TOP — it is
// the thing you came here to type in, and it was previously below three
// screenfuls of suggestions — and results REPLACE the curated sections rather
// than appearing under them. A curated shortlist is the answer to "what should
// I even get", which is the question you have BEFORE you know what to type;
// once you have typed, you have a better one, and showing both would ask the
// reader which of two grids is answering them.
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
import {
  discoverChrome,
  gateChrome,
  resultsSummary,
  suggestedSummary,
} from "@shell/discoverView";

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

/** What every card on this tab needs to know about a pull it might start.
 *
 *  One object rather than five props threaded twice, because the suggested
 *  cards and the search results answer the SAME four-state question (see
 *  `cardState`) and a second copy of that reasoning is how the two grids would
 *  come to disagree about a model sitting in both of them.
 */
interface Downloads {
  /** Repo ids with a MATERIALISED snapshot on this disk, or null while the walk
   *  is still running. Owned by the page, so both tabs mean one thing by it. */
  onDisk: Set<string> | null;
  downloading: Set<string>;
  /** Pulls that have STOPPED being reported and whose confirming walk has not
   *  landed yet — the far end of the same gap `pending` covers at the near end. */
  settling: Set<string>;
  jobByModel: Map<string, Job>;
  /** The id this session last pressed Download on, until something else can
   *  speak for it. */
  pending: string | null;
  start: (model: string, capability: string) => void;
}

// FOUR states, and every one of them was a bug at some point:
//
//   unknown — the cache walk has not answered yet. Neither the ✓ nor the
//     button, because both would be a claim. Treating null as an empty set
//     showed Download on a model already on disk for the length of the first
//     walk.
//   busy    — a pull is running. This spans three sources, and it needs all
//     three: `pending` from the click until the runtime poll sees it,
//     `downloading` while the runtime reports it, and `settling` from the moment
//     it stops being reported until the walk that confirms it lands. Drop any
//     one and the Download button flickers back on live work.
//   have    — a materialised snapshot. The ✓.
//   neither — the button.
function cardState(id: string, d: Downloads) {
  return {
    known: d.onDisk !== null,
    busy: d.downloading.has(id) || d.pending === id || d.settling.has(id),
    have: !!d.onDisk?.has(id),
  };
}

/** Download / ✓ / nothing, for one repo. The same control on both grids. */
function DownloadButton({
  id,
  capability,
  sizeHint,
  downloads,
}: {
  id: string;
  capability: string;
  /** What the button's hover promises it will cost, when anyone knows. */
  sizeHint: string | null;
  downloads: Downloads;
}) {
  const { known, busy, have } = cardState(id, downloads);
  if (!known || have || busy) return null;
  return (
    <button
      type="button"
      className="am-card-power"
      onClick={() => downloads.start(id, capability)}
      title={sizeHint ? `Download ${id} (${sizeHint})` : `Download ${id}`}
    >
      Download
    </button>
  );
}

// One search result. Same .cc-mdcard/.am-card as everything else on the page —
// a model should not look like two different things depending on which grid
// found it.
function HubCard({
  model,
  downloads,
  authenticated,
}: {
  model: HubModel;
  downloads: Downloads;
  /** Whether this machine holds a Hub token. It belongs to the SEARCH, not to
   *  the model, which is why it arrives beside the row rather than in it. */
  authenticated: boolean;
}) {
  const { busy, have } = cardState(model.id, downloads);
  // What the Hub asks before it will hand this one over, when it asks anything.
  const gate = gateChrome(model.gated, authenticated);
  // Only a COMPLETE download opens locally. "partial" means blobs with no
  // materialised snapshot, so there is no revision for the model card to
  // describe — linking there would hand someone a view that cannot load.
  const here = model.local.state === "downloaded" && model.local.path;
  const dl = count(model.downloads);
  const likes = count(model.likes);
  // The Hub sends an ISO timestamp; timeAgo works in epoch seconds. An
  // unparseable one is a field the card leaves out, not a "NaN ago".
  const updatedAt = model.updated ? Date.parse(model.updated) : NaN;
  const updated = Number.isFinite(updatedAt) ? timeAgo(updatedAt / 1000) : null;
  const size = model.estimatedSize ? `≈${formatSize(model.estimatedSize)}` : null;

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
        {have && !busy && (
          <span className="am-suggest-have" title={`${model.id} is already on this machine`}>
            ✓ downloaded
          </span>
        )}
        {/* The gate, named, with the whole of what to do about it on hover.
            This is NOT the pill D313 deleted: that one announced a problem and
            left a Download button beside it that would 403. Here the gate
            decides the action too — see the footer. */}
        {gate && !have && (
          <span className="am-card-gate" title={gate.title}>
            {gate.pill}
          </span>
        )}
        <span className="am-card-size" title={sizeTitle(model)}>
          {size ?? "—"}
        </span>
      </div>

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

      {busy && <ModelProgress job={downloads.jobByModel.get(model.id)} />}

      <div className="cc-mdcard-foot">
        <span className="cc-mdcard-meta">
          {dl ? `${dl} downloads` : null}
          {dl && likes ? " · " : null}
          {likes ? `${likes} likes` : null}
          {(dl || likes) && updated ? " · " : null}
          {updated ? `updated ${updated}` : null}
        </span>
        <span className="cc-mdcard-actions">
          {here && (
            <a
              className="am-card-explore-link"
              // The same URL the Local tab's Explore builds — a raw "#" + path
              // drops the mode, so a middle-click would land on the folder
              // listing rather than the model card.
              href={urlForFsPath(model.local.path as string, "?_mode=model_card")}
              title={`Explore ${model.id} here — ${model.local.path}`}
              onClick={(e) => {
                if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
                  return;
                e.preventDefault();
                navigate(model.local.path as string, { isDir: true, mode: "model_card" });
              }}
            >
              Explore
            </a>
          )}
          {/* A gate this machine cannot open gets the way to open it instead of
              a button that cannot start. The link goes to the model's own Hub
              page, which is where both the licence and the access request
              live. */}
          {gate?.action && !have && (
            <a
              className="am-card-power am-card-gate-link"
              href={model.url}
              target="_blank"
              rel="noopener noreferrer"
              title={gate.title}
            >
              {gate.action}
            </a>
          )}
          {(!gate || gate.canDownload) && (
            <DownloadButton
              id={model.id}
              capability={model.capability}
              sizeHint={size}
              downloads={downloads}
            />
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
  downloads,
}: {
  catalog: AiCatalogCapability[];
  downloads: Downloads;
}) {
  return (
    <>
      {catalog.map((group) => (
        <div className="am-subgroup" key={group.capability}>
          {/* The Local tab's CAPABILITY heading, exactly: an ALL-CAPS title
              with the one secondary fact about the group at the far right —
              there a byte subtotal, here WHICH backend will load these — and
              no rule of its own, because the rule belongs to the section this
              sits inside.

              It was an `.am-section-head` with a rule for a while, which drew
              it identically to the heading naming the whole view: same caps,
              same weight, same muted right-hand suffix, same full-width line.
              Two levels rendered as twins is no levels, and "TEXT GENERATION"
              read as a sibling of "Suggested models" rather than as one of its
              parts. Before that it was a lowercase run of inline text ("text
              generation via MLX LM") that read as a caption belonging to the
              first card rather than a heading over all of them. */}
          <div className="am-subgroup-head">
            <h3 className="am-subgroup-title">{group.capability.replace(/-/g, " ")}</h3>
            {/* WHICH backend will load these, named. One capability can have
                two runners now (text generation: MLX on Apple Silicon, PyTorch
                everywhere else), and the shortlist below differs completely
                between them — so a heading that said only "text generation"
                left the reader with no way to tell which list they were looking
                at, or why it was not the one in the docs. Since D302 that
                backend is a CHOICE rather than only a hardware fact, and the
                title says where from: "my suggested models changed" is
                otherwise an unexplainable event. */}
            {group.available && group.runnerShortLabel && (
              <span
                className="am-suggested-runner"
                title="Chosen on the Engines tab. Each backend loads its own model format, so this list changes with it."
              >
                via {group.runnerShortLabel}
              </span>
            )}
            {/* Shown even when it cannot run here, with the reason: hiding a
                capability leaves someone hunting for a feature that never was. */}
            {!group.available && (
              <span className="am-suggested-why" title={group.reason ?? undefined}>
                unavailable — {group.reason}
              </span>
            )}
          </div>
          {/* No runner note here. What running on a backend is LIKE — the
              memory ceiling on MLX FLUX, MLX Whisper's GPU speed — is now a
              line under that engine's row on the Engines tab (`engineNote`).
              Three of the six runners have one, so on this tab it appeared
              under some capability headings and not others, which made the
              sections look blotchy and the sentences read as noise rather than
              as the one thing worth knowing; and the FLUX line is a CAUTION
              about a choice, so it belongs beside the control that changes it.
              `runnerNote` is still on the catalog payload — it is the same
              string, rendered somewhere better. */}
          <div className="cc-mdgrid am-grid">
            {group.models.map((m) => {
              const { busy, have } = cardState(m.id, downloads);
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
                    {/* An unmeasured size is a dash, never a guess — the same
                        rule the search result cards follow above. */}
                    <span
                      className="am-card-size"
                      title={
                        m.size_gb === null
                          ? "Nobody has recorded this one's download size yet."
                          : undefined
                      }
                    >
                      {m.size_gb === null ? "—" : `${m.size_gb} GB`}
                    </span>
                  </div>
                  <div className="am-suggest-note">{m.note}</div>
                  {/* No `detail` override: the job says what it is doing
                      ("Fetching weights…", "Preparing MLX…") and a fixed word
                      here would paper over a venv build with "Downloading". */}
                  {busy && <ModelProgress job={downloads.jobByModel.get(m.id)} />}
                  <div className="cc-mdcard-foot">
                    <span className="cc-mdcard-meta cc-mono">{m.id}</span>
                    {group.available && (
                      <DownloadButton
                        id={m.id}
                        capability={group.capability}
                        sizeHint={m.size_gb === null ? null : `~${m.size_gb} GB`}
                        downloads={downloads}
                      />
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </>
  );
}

export default function AiModelsDiscover({
  onDisk,
  downloading,
  settling,
  jobByModel,
}: {
  onDisk: Set<string> | null;
  downloading: Set<string>;
  settling: Set<string>;
  jobByModel: Map<string, Job>;
}) {
  const [query, setQuery] = useState("");
  const [task, setTask] = useState("");
  const [sort, setSort] = useState<HubSort>("downloads");
  const [tasks, setTasks] = useState<HubTask[]>([]);
  const [catalog, setCatalog] = useState<AiCatalogCapability[] | null>(null);
  const [models, setModels] = useState<HubModel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [endpoint, setEndpoint] = useState<string | null>(null);
  const [authenticated, setAuthenticated] = useState(false);
  // Bumped by the debounce so the fetch effect re-runs on a settled query
  // rather than on every keystroke.
  const [settled, setSettled] = useState({ q: "", task: "", sort: "downloads" as HubSort });
  const timer = useRef<number | null>(null);
  const searchBox = useRef<HTMLInputElement>(null);

  // Whether the page is ANSWERING A QUERY or offering a starting point, and the
  // captions that have to agree with it. One answer, five consumers — see
  // `discoverView.ts` for why that is a module rather than five `&&`s.
  const chrome = discoverChrome(settled.q, settled.task);
  const searching = chrome.view === "results";
  // The SAME rule asked of the live controls rather than the settled query,
  // for the one piece of chrome that belongs to the box instead of to the grid:
  // the ✕ inside the search field. Everything else on the page describes what
  // is RENDERED and must wait for the debounce; the ✕ describes what is TYPED,
  // and a clear button that arrives 350ms after the first letter — or sits
  // there for 350ms after emptying the box — is the control contradicting the
  // field it lives in.
  const live = discoverChrome(query, task);

  /** Back to the curated view, in one act.
   *
   *  BOTH inputs, and that is the whole requirement. A control that emptied the
   *  text and left the task filter set would leave the reader looking at an
   *  empty search box, a page of results, and no suggestions — having done
   *  exactly what the page told them to. The sort is deliberately untouched: it
   *  does not push the tab out of the curated state, so resetting it would be
   *  the button doing something nobody asked for.
   *
   *  Focus returns to the box because it is where the next thing happens, and
   *  because the two controls that call this are one of them and a key pressed
   *  inside it.
   */
  const clearSearch = () => {
    setQuery("");
    setTask("");
    searchBox.current?.focus();
  };

  useEffect(() => {
    // The filter list is small and comes from the server because only the
    // server knows which pipeline tags a registered runner can serve (D313) —
    // a hardcoded menu here would offer filters for models the app cannot load,
    // which is the whole complaint this constraint answers. A failure is not
    // worth a banner: the search still works, it just has no task menu.
    getHubTasks().then(
      (d) => setTasks(d.tasks),
      () => setTasks([]),
    );
  }, []);

  useEffect(() => {
    // Just the curation. Whether each entry is on this disk is the PAGE's
    // answer, arriving as `onDisk` — this used to run its own cache walk beside
    // the page's, which meant two definitions of "downloaded" and two moments
    // they were true.
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
    // No query, no request. The search fires on what someone typed, and an
    // empty box is the curated tab — asking the Hub for "the most downloaded
    // models" to then not render them would be an outbound call for nothing.
    if (!settled.q && !settled.task) {
      setModels(null);
      setError(null);
      setLoading(false);
      return;
    }
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
        // Whether this machine holds a Hub token — never the token, only the
        // fact. It decides what a gated card offers (`gateChrome`), and it
        // comes from the same reply as the rows so the two cannot describe
        // different moments.
        setAuthenticated(!!data.authenticated);
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

  // The click is held until something ELSE can speak for the pull — the runtime
  // reporting it, `settling` carrying it through the walk, or the walk finding
  // it on disk (a "download" that was a cache hit and finished before any poll
  // saw it). Clearing on the POST's reply put the card back to "Download" for
  // the beat before the next runtime poll, which reads as the button having
  // done nothing.
  const spokenFor =
    pending !== null &&
    (downloading.has(pending) || settling.has(pending) || !!onDisk?.has(pending));
  useEffect(() => {
    if (spokenFor) setPending(null);
  }, [spokenFor]);

  const start = async (model: string, capability: string) => {
    setDownloadError(null);
    setPending(model);
    try {
      await downloadAiModel(model, capability);
      // The pull is the server's now. Asking the runtime for a fresh read is the
      // whole follow-up: the card's state comes from what is actually happening,
      // never from the fact that a button was pressed.
      refreshAiRuntime();
    } catch (e) {
      setDownloadError((e as Error).message);
      setPending(null);
    }
  };

  const downloads: Downloads = { onDisk, downloading, settling, jobByModel, pending, start };

  // The host this page is asking, named and reachable. The server reports the
  // endpoint it actually used (HF_ENDPOINT, validated http(s) there), so a
  // machine pointed at a mirror says the mirror's name and links to the mirror
  // — the caption exists to disclose who is being asked, and a name that went
  // somewhere else would defeat it.
  const hostUrl = endpoint || "https://huggingface.co";
  const host = hostUrl.replace(/^https?:\/\//, "");

  // The muted fact beside the heading, in the slot the capability sections put
  // "via MLX Whisper" in. Counting what is ON SCREEN, in both cases: the rows
  // the server let through after its supported-tag pass, and the cards the
  // catalog actually renders — a number here that disagreed with the grid under
  // it would be worse than no number.
  const suggestedCount = (catalog ?? []).reduce((n, g) => n + g.models.length, 0);
  const summary = searching
    ? resultsSummary(settled.q, models?.length ?? null, host)
    : suggestedCount > 0
      ? suggestedSummary(suggestedCount)
      : null;

  return (
    <>
      {/* At the TOP, above everything. It is what someone came to this tab to
          use, and it spent its life under three screenfuls of suggestion cards
          where the only way to find it was to scroll past the thing it was
          supposed to be an alternative to. */}
      <div className="am-hub-controls">
        <div className="am-hub-field">
          <input
            ref={searchBox}
            className="am-hub-search"
            type="search"
            value={query}
            placeholder="Search models on the Hub…"
            aria-label="Search models on the Hugging Face Hub"
            onChange={(e) => setQuery(e.target.value)}
            // Escape is the reflex for "put this back", and in this box it
            // clears the TASK FILTER too — the same one act the ✕ performs, for
            // the same reason. Not stopPropagation: nothing else on this page
            // listens for Escape while a text field has focus, and swallowing
            // it would break the next overlay that does.
            onKeyDown={(e) => {
              if (e.key !== "Escape" || !live.showsReset) return;
              e.preventDefault();
              clearSearch();
            }}
          />
          {/* Inside the box, and it clears BOTH inputs. The native
              type="search" ✕ is hidden in CSS precisely because it does not:
              it empties the text and leaves a task filter behind, which is the
              exact failure that looks broken — the box is empty, the reader has
              done the obvious thing, and the suggestions still are not back.
              Its visibility follows the LIVE controls rather than the settled
              query, because it belongs to the box: appearing 350ms after the
              first keystroke, or lingering that long after a clear, is the
              control disagreeing with the field it sits in. */}
          {live.showsReset && (
            <button
              type="button"
              className="am-hub-clear"
              onClick={clearSearch}
              aria-label="Clear the search and show suggested models"
              title="Clear the search and the task filter (Esc)"
            >
              ✕
            </button>
          )}
        </div>
        <select
          className="field-control am-hub-select"
          value={task}
          aria-label="Filter by task"
          onChange={(e) => setTask(e.target.value)}
          title={tasks.find((t) => t.tag === task)?.help ?? "Show only models for one kind of job"}
        >
          {/* "Any task" means any task THIS APP RUNS — the menu beside it holds
              only those (D313), and so does an unfiltered search. */}
          <option value="">Any task</option>
          {tasks.map((t) => (
            <option key={t.tag} value={t.tag} title={t.help ?? undefined}>
              {t.label}
            </option>
          ))}
        </select>
        <select
          className="field-control am-hub-select"
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
      {downloadError && <ErrorBanner>{downloadError}</ErrorBanner>}
      {error && <ErrorBanner>{error}</ErrorBanner>}

      {/* ONE section, whichever face is showing, named at the top — the Local
          tab's exact shape: a section title over a rule, with the capabilities
          as quieter, unruled subgroups inside it.

          The heading exists because the two faces of this tab used to differ
          only by whether a paragraph of prose was present: search, scroll
          through the results, look back up, and nothing on screen said whether
          these cards were vetted suggestions or whatever the Hub returned. It
          is the SECTION tier and not a fourth capability — `chrome.heading` is
          one string rather than two conditions, so the page cannot claim to be
          both at once. */}
      <section className="am-section">
        <div className="am-section-head">
          <h3 className="am-section-title">{chrome.heading}</h3>
          <span className="am-discover-headmeta">
            {summary && <span className="am-discover-summary">{summary}</span>}
            {/* The second way back, in the row somebody looking at results they
                did not want is already reading. The ✕ in the box is the one you
                find when you go looking for it; this is the one you cannot
                miss, and it says where it goes rather than what it erases —
                "clear" describes the mechanism, and the reader's question is
                "how do I get the suggestions back". */}
            {chrome.showsReset && (
              <button
                type="button"
                className="am-hub-back"
                onClick={clearSearch}
                title="Clear the search and the task filter"
              >
                ← Back to suggested models
              </button>
            )}
          </span>
        </div>

        {/* One grid at a time. A query replaces the shortlist; clearing the box
            brings it back, which is what makes the box safe to type in. */}
        {searching ? (
          <>
            {/* Said plainly, once, and only while a query is live: this is the
                one place in the app that asks a third party a question. Under
                the heading it belongs to, where the shortlist puts its own note
                — the two states are the same three things in the same order,
                which is what makes swapping one for the other legible. It no
                longer has to admit that the answers are useless: the sentence
                that used to end "Search results are read-only" is gone because
                the results are not. */}
            {chrome.showsSearchNote && (
              <p className="cc-caption am-hub-note">
                Searching{" "}
                <a
                  className="am-hub-host"
                  href={hostUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`Open ${host} in a new tab`}
                >
                  {host}
                </a>
                , limited to models an engine here can load.
              </p>
            )}
            {loading && models === null && <p className="cc-empty">Asking {host}…</p>}
            {models !== null && models.length === 0 && !error && (
              <p className="cc-empty">
                Nothing on {host} matches that — among the models this app can run.
              </p>
            )}
            {models !== null && models.length > 0 && (
              <div className={"cc-mdgrid am-grid" + (loading ? " am-hub-stale" : "")}>
                {models.map((m) => (
                  <HubCard
                    key={m.id}
                    model={m}
                    downloads={downloads}
                    authenticated={authenticated}
                  />
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            {/* Why these particular eleven, under the heading that named them.
                The heading says WHICH grid this is; this says what "suggested"
                was decided by, because "a handful somebody picked for this
                machine" and "everything you can install" are very different
                answers to the question the reader arrived with. It also names
                the way out, since the control that answers "and if I want
                something else?" is the box at the top of the tab.

                It renders with the sections and never over the results grid
                (`showsPreamble`, pinned in discoverView.test.ts): a line reading
                "picked to run on this machine" standing over a page of Hub
                search results describes cards that are not there. */}
            {chrome.showsPreamble && catalog !== null && catalog.length > 0 && (
              <p className="am-group-note am-suggested-lede">
                Picked to run on this machine with the engines you have. Search above for
                anything else on Hugging Face.
              </p>
            )}
            {catalog === null && <p className="cc-empty">Reading the model catalog…</p>}
            {catalog !== null && <Suggested catalog={catalog} downloads={downloads} />}
          </>
        )}
      </section>
    </>
  );
}
