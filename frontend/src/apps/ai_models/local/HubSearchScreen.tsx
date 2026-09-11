// The full search screen: "Search Hugging Face", replacing a capability's
// pane the way the approved mockup's `advPane()` does. One row per REPO —
// no family grouping (the mockup's own `hit()` never groups) — ranked for
// this Mac.
//
// Reached only from a capability pane's own "Search Hugging Face for more
// {noun} →" link (`CapabilityPane`'s `onOpenSearch`), and left the same way
// it is entered: `onBack` returns to that same pane, never to a different
// capability — a search started from "Text generation" must not hand back
// "Image generation" open instead.
//
// Fetch/gate/login machinery ported from `HubResults.tsx` (D426's own
// history): the debounced settle, the size-sort pass, `HubLogin`'s
// device-code flow. What is NOT ported is family grouping
// (`hubFamilies.ts`) and the dense `<table>` (`HubResultsTable.tsx`) — the
// brief for this screen is one row per hit, the mockup's own `hit()`.
import { useEffect, useRef, useState } from "react";
import { SearchControls } from "./SearchControls";
import { hubModelUrl } from "./hub";
import { type DiskCard, type SectionRunner, resultDisk } from "@apps/ai_models/lib/aiModelGroups";
import { capabilityMeta } from "@apps/ai_models/lib/capabilityMeta";
import {
  bySizeAscending,
  gateChrome,
  needsHubLogin,
  resultsSummary,
  sortsOnPage,
  wireSort,
  type ResultSort,
} from "@apps/ai_models/lib/hubSearchView";
import { hubSizeBytes, knownTotalSize, lookupTotalSize } from "@apps/ai_models/lib/hubSize";
import {
  ageLabel,
  matchCell,
  matchTitle,
  paramsLabel,
  popLabel,
  quantLabel,
  splitRepoId,
} from "@apps/ai_models/lib/hubTableView";
import {
  cancelHfLogin,
  getHfAuth,
  searchHubModels,
  startHfLogin,
  type HfAuth,
  type HubFitLevel,
  type HubModel,
  type HubParamsBand,
} from "@platform/lib/api";
import { formatSize } from "@platform/lib/format";
import { type Job } from "@platform/lib/jobs";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

/** A settled query — what the debounce below hands over, and the only thing
 *  the fetch effect is keyed on. Ported from `HubResults.tsx`'s own
 *  `SettledQuery` verbatim. */
export interface SettledQuery {
  q: string;
  task: string;
  sort: ResultSort;
  includeUnfit: boolean;
  fitLevel: HubFitLevel;
  paramsBand: HubParamsBand;
  quant: string;
  publisher: string;
}

const INITIAL_LIMIT = 24;
const LOAD_MORE = 20;
const SIZE_LOOKUPS = 4;

async function measureSizes(
  ids: readonly string[],
  alive: () => boolean,
): Promise<Map<string, number | null>> {
  const out = new Map<string, number | null>();
  let next = 0;
  const worker = async () => {
    while (alive() && next < ids.length) {
      const id = ids[next++];
      out.set(id, await lookupTotalSize(id, null));
    }
  };
  await Promise.all(Array.from({ length: Math.min(SIZE_LOOKUPS, ids.length) }, () => worker()));
  return out;
}

/** Signing in to Hugging Face — ported from `HubResults.tsx`'s `HubLogin`
 *  verbatim; see that file's own doc for the device-code flow's rationale. */
function HubLogin({ onSignedIn }: { onSignedIn: () => void }) {
  const [auth, setAuth] = useState<HfAuth | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pending = auth?.pending ?? null;
  useEffect(() => {
    if (!pending) return;
    let alive = true;
    const id = setInterval(() => {
      getHfAuth().then(
        (a) => alive && setAuth(a),
        () => undefined,
      );
    }, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [pending !== null]);

  useEffect(() => {
    if (auth?.signedIn) onSignedIn();
  }, [auth?.signedIn]);

  const act = async (fn: () => Promise<HfAuth>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      setAuth(await fn());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="am-hub-login">
      {auth?.pending ? (
        <>
          <a
            className="am-card-power am-card-gate-link"
            href={auth.pending.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            Authorize on huggingface.co
          </a>
          <span className="cc-caption">
            Waiting for you to authorize. If asked for a code, enter <code>{auth.pending.userCode}</code> — it
            expires in {Math.max(1, Math.round(auth.pending.secondsLeft / 60))} min.
          </span>
          <button type="button" className="am-hub-back" disabled={busy} onClick={() => void act(cancelHfLogin)}>
            Cancel
          </button>
        </>
      ) : (
        <>
          <button
            type="button"
            className="am-card-power"
            disabled={busy}
            title="Start the Hugging Face device-code login. The token is stored by huggingface_hub, never by this app."
            onClick={() => void act(() => startHfLogin())}
          >
            Log in to Hugging Face
          </button>
          <span className="cc-caption">Some results below are gated. Downloading one needs a token on this machine.</span>
        </>
      )}
      {(error || auth?.error) && <ErrorBanner>{error ?? auth?.error}</ErrorBanner>}
    </div>
  );
}

/** One hit — the mockup's own `hit()`, one repo, one row. No family grouping
 *  and no disclosure drawer: everything worth knowing about a search result
 *  is already on the row. */
function HitRow({
  model,
  disk,
  authenticated,
  busy,
  onDownload,
  onCancel,
  job,
}: {
  model: HubModel;
  disk: ReturnType<typeof resultDisk>;
  authenticated: boolean;
  busy: boolean;
  onDownload: () => void;
  onCancel: (job: Job) => void;
  job: Job | undefined;
}) {
  const { owner, name } = splitRepoId(model.id);
  const cell = matchCell(model.fit, model.matchScore);
  const title = matchTitle(model.fit, model.matchScore);
  const gate = disk.state === "downloaded" ? null : gateChrome(model.gated, authenticated);
  const sizeLabel = model.estimatedSize ? `≈${formatSize(model.estimatedSize)}` : null;

  return (
    <div className={`rowwrap`} data-part="hit">
      <div className={`row hit${disk.state === "downloaded" ? " have" : ""}${disk.state === "partial" ? " unfit" : ""}`}>
        <div>
          <div className="row-name">
            <b>{name}</b>
            {owner && <span className="row-owner mono">{owner}</span>}
            {gate && (
              <span className="chip" title={gate.title}>
                {gate.pill}
              </span>
            )}
          </div>
          <p className="row-note mono">
            {[model.task, paramsLabel(model.params), quantLabel(model.quant), sizeLabel].filter(Boolean).join(" · ")}
          </p>
          {model.fit?.verdict === "no" && (
            <p className="row-reason" style={{ color: "var(--danger, #c0392b)" }}>
              Will not fit — needs about {formatSize(model.fit.footprintBytes)} of memory.
            </p>
          )}
          {!model.fit && (
            <p className="row-reason mono">Fit not known yet.</p>
          )}
        </div>
        <span className="match" title={title} data-verdict={cell.verdict}>
          <span className="mbar">
            <i style={{ width: `${cell.scoreText === "—" ? 0 : cell.scoreText}%` }} />
          </span>
          <span className="mnum">{cell.scoreText}</span>
        </span>
        <span className="row-facts">
          {popLabel(model.downloads)} dl · {popLabel(model.likes)} likes · {ageLabel(model.created)}
        </span>
        <span className="row-act">
          {disk.state === "downloaded" ? (
            <span className="chip">✓ Downloaded</span>
          ) : busy ? (
            <button type="button" className="btn" onClick={() => job && onCancel(job)}>
              Stop
            </button>
          ) : gate && !gate.canDownload ? (
            <a className="btn-link" href={hubModelUrl(model.id)} target="_blank" rel="noreferrer">
              {gate.action}
            </a>
          ) : (
            <button type="button" className="btn" onClick={onDownload}>
              Download
            </button>
          )}
        </span>
      </div>
    </div>
  );
}

export function HubSearchScreen({
  capabilityKey,
  settled,
  cards,
  runners,
  jobByModel,
  pulling,
  onDownload,
  onCancel,
  onQuery,
  onSettle,
  onBack,
}: {
  capabilityKey: string;
  settled: SettledQuery;
  cards: ReadonlyMap<string, DiskCard> | null;
  runners: ReadonlyMap<string, SectionRunner>;
  jobByModel: Map<string, Job>;
  pulling: (id: string) => boolean;
  onDownload: (id: string, capability: string) => void;
  onCancel: (job: Job) => void;
  /** Live query text — separate from `settled.q` for the same reason
   *  `LocalTab` used to keep them apart: a burst of typing is one request,
   *  not one per keystroke. */
  onQuery: (q: string) => void;
  onSettle: (next: SettledQuery) => void;
  onBack: () => void;
}) {
  const meta = capabilityMeta(capabilityKey);
  const [liveQuery, setLiveQuery] = useState(settled.q);
  const [limit, setLimit] = useState(INITIAL_LIMIT);
  const [models, setModels] = useState<HubModel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [endpoint, setEndpoint] = useState<string | null>(null);
  const [authenticated, setAuthenticated] = useState(false);
  const [authEpoch, setAuthEpoch] = useState(0);
  const [sizes, setSizes] = useState<ReadonlyMap<string, number | null> | null>(null);
  const [measuring, setMeasuring] = useState(false);
  const [hiddenUnfit, setHiddenUnfit] = useState(0);
  const debounce = useRef<number | null>(null);

  useEffect(() => {
    if (debounce.current) window.clearTimeout(debounce.current);
    debounce.current = window.setTimeout(() => {
      setLimit(INITIAL_LIMIT);
      onSettle({ ...settled, q: liveQuery });
      onQuery(liveQuery);
    }, 350);
    return () => {
      if (debounce.current) window.clearTimeout(debounce.current);
    };
    // Only the query is debounced here — every other filter (task/fit/size/
    // sort/quant/publisher) is a menu click or a Load-more, instantaneous
    // either way.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveQuery]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    searchHubModels({
      q: settled.q,
      task: settled.task,
      sort: wireSort(settled.sort),
      limit,
      includeUnfit: settled.includeUnfit,
      fitLevel: settled.fitLevel,
      paramsBand: settled.paramsBand,
      quant: settled.quant || undefined,
      publisher: settled.publisher || undefined,
    }).then(
      (data) => {
        if (!alive) return;
        setLoading(false);
        setError(data.error ?? null);
        setModels(data.models);
        setEndpoint(data.endpoint ?? null);
        setHiddenUnfit(data.hiddenUnfit ?? 0);
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
  }, [settled, limit, authEpoch]);

  useEffect(() => {
    if (!sortsOnPage(settled.sort) || !models || models.length === 0) {
      setSizes(null);
      setMeasuring(false);
      return;
    }
    const ids = models.filter((m) => !m.estimatedSize).map((m) => m.id);
    if (ids.every((id) => knownTotalSize(id) !== undefined)) {
      setSizes(new Map(ids.map((id) => [id, knownTotalSize(id) as number | null])));
      setMeasuring(false);
      return;
    }
    let alive = true;
    setSizes(null);
    setMeasuring(true);
    measureSizes(ids, () => alive).then((got) => {
      if (!alive) return;
      setMeasuring(false);
      setSizes(got);
    });
    return () => {
      alive = false;
    };
  }, [models, settled.sort]);

  const hostUrl = endpoint || "https://huggingface.co";
  const host = hostUrl.replace(/^https?:\/\//, "");
  const shown =
    models && sortsOnPage(settled.sort) && sizes
      ? bySizeAscending(models, (m) => hubSizeBytes(m, sizes.get(m.id)))
      : models;
  const summary = resultsSummary(settled.q, shown?.length ?? null, host, !!error);

  return (
    <div className="tp-pane" data-part="adv">
      <button type="button" className="btn-link" data-adv-back="1" onClick={onBack}>
        ← Back to {meta.plain}
      </button>
      <div className="adv-head">
        <h4>Search Hugging Face</h4>
        <p>Every model on the Hub this Mac can run, ranked for this Mac. Nothing here is curated by us.</p>
      </div>
      <div className="bigsearch">
        <input
          type="search"
          value={liveQuery}
          placeholder={`Search ${meta.searchNoun}…`}
          aria-label="Search models on the Hugging Face Hub"
          onChange={(e) => setLiveQuery(e.target.value)}
        />
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => {
            setLimit(INITIAL_LIMIT);
            onSettle({ ...settled, q: liveQuery });
          }}
        >
          Search
        </button>
      </div>
      <SearchControls
        task={settled.task}
        sort={settled.sort}
        fitLevel={settled.fitLevel}
        paramsBand={settled.paramsBand}
        quant={settled.quant}
        publisher={settled.publisher}
        includeUnfit={settled.includeUnfit}
        onTask={(task) => onSettle({ ...settled, task })}
        onSort={(sort) => onSettle({ ...settled, sort })}
        onFitLevel={(fitLevel) => onSettle({ ...settled, fitLevel })}
        onParamsBand={(paramsBand) => onSettle({ ...settled, paramsBand })}
        onQuant={(quant) => onSettle({ ...settled, quant })}
        onPublisher={(publisher) => onSettle({ ...settled, publisher })}
        onIncludeUnfit={(includeUnfit) => onSettle({ ...settled, includeUnfit })}
        loading={loading && models === null}
        matchCount={shown?.length ?? null}
        hiddenUnfit={hiddenUnfit}
      />
      <p className="sorthint">
        Ranked for this Mac: memory fit first, then speed, freshness and popularity; models already here get a
        small bonus. Bar colour is the memory verdict: ● fits comfortably ▲ fits tightly ■ will not fit ? not
        measured yet.
      </p>
      {chromeNote(settled, host, hostUrl)}
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {needsHubLogin(models, authenticated) && <HubLogin onSignedIn={() => setAuthEpoch((n) => n + 1)} />}
      {loading && models === null && <p className="cc-empty">Asking {host}…</p>}
      {models !== null && models.length === 0 && !error && (
        <p className="cc-empty">
          Nothing on {host} matches "{settled.q}" — among the {meta.searchNoun} this Mac can run.
        </p>
      )}
      {summary && (
        <p className="cc-caption" style={{ display: "none" }}>
          {summary}
        </p>
      )}
      {shown && shown.length > 0 && (
        <div className={loading || measuring ? "am-hub-stale hits" : "hits"}>
          {shown.map((m) => (
            <HitRow
              key={m.id}
              model={m}
              disk={resultDisk(m.id, cards)}
              authenticated={authenticated}
              busy={pulling(m.id)}
              job={jobByModel.get(m.id)}
              onDownload={() => onDownload(m.id, m.capability)}
              onCancel={onCancel}
            />
          ))}
        </div>
      )}
      {models !== null && models.length >= limit && !error && (
        <div className="advfoot">
          <button type="button" className="btn" onClick={() => setLimit((n) => n + LOAD_MORE)}>
            Load 20 more
          </button>
          <span className="mono">Downloads land in ~/.cache/huggingface/hub</span>
        </div>
      )}
    </div>
  );
}

/** The one place this screen asks a third party a question, stated plainly —
 *  ported from `HubResults.tsx`'s own note. Only while the runner registry
 *  serving `capabilityKey` is known here (unused today; `runners` is kept as
 *  a prop for a future per-capability engine note, same as `HubResults`
 *  read it for). */
function chromeNote(settled: SettledQuery, host: string, hostUrl: string) {
  if (!settled.q.trim() && !settled.task.trim()) return null;
  return (
    <p className="cc-caption am-hub-note">
      Searching{" "}
      <a className="am-hub-host" href={hostUrl} target="_blank" rel="noopener noreferrer" title={`Open ${host} in a new tab`}>
        {host}
      </a>
      , limited to models an engine here can load.
    </p>
  );
}
