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
import { ClockIcon, DownloadIcon, HeartIcon } from "./HitStatIcons";
import { SearchControls } from "./SearchControls";
import { hubModelUrl } from "./hub";
import { formatToken } from "@apps/ai_models/lib/formatToken";
import { type DiskCard, resultDisk } from "@apps/ai_models/lib/aiModelGroups";
import { capabilityMeta } from "@apps/ai_models/lib/capabilityMeta";
import {
  bySizeAscending,
  gateChrome,
  sortsOnPage,
  wireSort,
  type ResultSort,
} from "@apps/ai_models/lib/hubSearchView";
import { hubSizeBytes, knownTotalSize, lookupTotalSize } from "@apps/ai_models/lib/hubSize";
import {
  ageLabel,
  downloadedVariantLabel,
  matchCell,
  matchRowTip,
  nextPoolPhase,
  pagesFetchedLabel,
  paramsLabel,
  poolBuildBanner,
  popLabel,
  quantLabel,
  variantIsDownloadable,
  verdictGlyph,
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
  type HubSearchFacets,
  type HubSearchResult,
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
  fitLevel: HubFitLevel;
  paramsBand: HubParamsBand;
  quant: string;
  publisher: string;
}

const INITIAL_LIMIT = 24;
const LOAD_MORE = 20;
const SIZE_LOOKUPS = 4;

/** Item 1 (fix round 4): a Size sort must rank by the SAME bytes the row's
 *  own cell would show (`hubSizeBytes`, `lib/hubSize.ts`) — the resolved
 *  file's own size for a GGUF row, never the repo-wide `usedStorage` total
 *  that same function only trusts when `model.file` is set. Looking every
 *  row up with `file: null` (the old behavior) handed a GGUF row a
 *  repo-wide sum that `hubSizeBytes` then read as if it were that row's own
 *  file — the "17 bytes/param" bug the code review flagged. A row with no
 *  `file` is the opposite problem: `hubSizeBytes` always returns null for
 *  it regardless of what `total` says, so asking the Hub for one at all is a
 *  request spent on a lookup nobody can use — skipped here instead. */
export async function measureSizes(
  models: readonly Pick<HubModel, "id" | "file">[],
  alive: () => boolean,
): Promise<Map<string, number | null>> {
  const out = new Map<string, number | null>();
  const withFile = models.filter((m) => m.file);
  for (const m of models) if (!m.file) out.set(m.id, null);
  let next = 0;
  const worker = async () => {
    while (alive() && next < withFile.length) {
      const m = withFile[next++];
      out.set(m.id, await lookupTotalSize(m.id, m.file));
    }
  };
  await Promise.all(Array.from({ length: Math.min(SIZE_LOOKUPS, withFile.length) }, () => worker()));
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

/** A hit's disclosure drawer (ⓘ) — the same `.drawer`/`dl`/`.acts` markup and
 *  CSS `ModelRow.tsx`'s own `Drawer` uses, but with a search hit's own facts
 *  rather than a pane row's (no curated/"why we suggest it" story — a search
 *  result was never suggested by us, per this screen's own "Nothing here is
 *  curated by us" line above). Kept local to this file rather than reusing
 *  `ModelRow`'s `Drawer` component directly: that component's "why" section
 *  is written entirely in terms of a pane row's `curated`/`ourPick` shape,
 *  which a Hub hit does not have and should not fake. */
function HitDrawer({
  model,
  authenticated,
  busy,
  onDownloadFile,
  onSignedIn,
  onClose,
}: {
  model: HubModel;
  authenticated: boolean;
  /** Item A: the row's own busy state (a download already in flight for
   *  THIS repo) — a per-variant button is disabled the same way the
   *  row-level one is, rather than letting a reader start a second
   *  concurrent download of the same repo under a different filename. */
  busy: boolean;
  /** Item A (per-variant download): fetches exactly `file` rather than
   *  whichever one the server's picker would otherwise choose. Absent for
   *  the row-level Download button, which keeps its unchanged behaviour. */
  onDownloadFile: (file: string) => void;
  onSignedIn: () => void;
  onClose: () => void;
}) {
  const sizeLabel = model.estimatedSize ? `≈${formatSize(model.estimatedSize)}` : "not recorded";
  return (
    <div className="drawer" data-part="hit.drawer">
      {/* Fix round 6 item 4: the footer "Close" link (below) read as buried
       *  at the bottom of a scrolled-past panel — moved to a quiet `×`
       *  control in the drawer's own top-right corner instead. */}
      <button
        type="button"
        className="btn-link drawer-close"
        onClick={onClose}
        aria-label="Close details"
      >
        ×
      </button>
      <dl>
        <dt>Repository</dt>
        <dd>{model.id}</dd>
        <dt>Task</dt>
        <dd className="plain">{model.task ?? <span className="unknown">not recorded</span>}</dd>
        <dt>Parameters</dt>
        <dd className="plain">{paramsLabel(model.params)}</dd>
        <dt>Quantization</dt>
        <dd className="plain">{quantLabel(model.quant)}</dd>
        <dt>Size</dt>
        <dd className="plain">{sizeLabel}</dd>
        {/* Item 6 (round 8): list the actual GGUF files a multi-variant repo
         *  offers, not just the count — `variants` is only ever populated for
         *  a GGUF row (null for safetensors/MLX, which don't have a per-file
         *  quant to pick between). The file this repo's own picker would fetch
         *  (`model.file`, unchanged from before) and the file already on this
         *  disk (`model.local.file`) each get a quiet marker so a reader can
         *  tell "what I'd get" and "what I already have" apart from the rest.
         *  Item A (per-variant download) turned this from a read-only list
         *  into a picker: `/api/ai/runtime/download` now accepts an explicit
         *  `file`, validated server-side against this same repo's own real
         *  GGUF candidates — see D1252 for the gap this closed. Each row
         *  reuses the row-level Download button's own `.btn` styling, and is
         *  disabled with "On disk" for whichever variant is already
         *  cached — the same fact `v.file === model.local?.file` already
         *  annotated in prose below, now also driving the control. */}
        {model.variants && model.variants.length > 1 && (
          <>
            <dt>Variants</dt>
            <dd className="plain">
              <ul className="variant-list">
                {model.variants.map((v) => {
                  const onDisk = v.file === model.local?.file;
                  return (
                    <li key={v.file}>
                      <code>{v.file}</code>
                      {v.quant ? ` — ${v.quant}` : ""}
                      {v.file === model.file ? " (default)" : ""}
                      {onDisk ? " (on disk)" : ""}
                      {variantIsDownloadable(v) ? (
                        <button
                          type="button"
                          className="btn variant-download"
                          disabled={busy || onDisk}
                          onClick={() => onDownloadFile(v.file)}
                        >
                          {onDisk ? "On disk" : "Download"}
                        </button>
                      ) : (
                        <span className="variant-download variant-unsupported">
                          multi-part, not supported
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </dd>
          </>
        )}
      </dl>
      {/* Item D: the search screen no longer shows a standing `.am-hub-login`
       *  banner over the whole results column — a login prompt over every
       *  search was recommending an account to a reader who never hit a
       *  wall. The device-code flow stays reachable, just moved to the one
       *  place it is actually relevant: a gated row's own drawer. */}
      {model.gated && !authenticated && (
        <div className="drawer-gate">
          <HubLogin onSignedIn={onSignedIn} />
        </div>
      )}
    </div>
  );
}

/** One hit — the mockup's own `hit()`, one repo, one row: match cell first,
 *  the full repo id in mono bold (no separate owner span — the mockup's own
 *  "no curated name to give them" reasoning), a `from <base>` line only when
 *  a base model is known, one `row-meta` facts line (params/quant/size/format/
 *  variants — no task label; the search screen is already scoped by the left
 *  pane's capability, so repeating it per row is redundant, fix round 8) with
 *  no dangling dash, popularity as its own right-hand cell, and
 *  Download/Accept-terms/Downloaded plus an ⓘ opening `HitDrawer` above. */
function HitRow({
  model,
  disk,
  authenticated,
  busy,
  infoOpen,
  onDownload,
  onDownloadFile,
  onCancel,
  onToggleInfo,
  onSignedIn,
  job,
}: {
  model: HubModel;
  disk: ReturnType<typeof resultDisk>;
  authenticated: boolean;
  busy: boolean;
  infoOpen: boolean;
  onDownload: () => void;
  /** Item A (per-variant download): fetches exactly `file`, threaded down to
   *  the drawer's own per-variant Download button. */
  onDownloadFile: (file: string) => void;
  onCancel: (job: Job) => void;
  onToggleInfo: () => void;
  onSignedIn: () => void;
  job: Job | undefined;
}) {
  const cell = matchCell(model.fit, model.matchScore);
  // Item 3 (fix round 5): `matchTitle`'s full sentence (still used for other
  // rows elsewhere) reads as a 60-word paragraph in a native `title=` —
  // small, unstyled, one-line-wrapped, ugly. This row instead carries a
  // short `data-match-tip` for a real (CSS-only) popover — see `.tp .match[data-match-tip]`
  // in ai-models.css — that keeps the score and the colour legend and drops
  // the rest.
  //
  // D1245/D1246/D1267: `matchRowTip` (not a generic ingredients list any
  // more) names only the one or two axes that cost THIS row the most, and
  // always gives fit its own second line — the fix for two rows that both
  // show "84" with a different colour reading as a bug. It carries `\n`
  // between its two lines; `matchTipLabel` below flattens that into one
  // sentence for the `aria-label` a screen reader reads (the CSS popover
  // itself, `data-match-tip`, is silent to assistive tech — it is not a `title`).
  const matchTip = matchRowTip(model.fit, model.matchScore, model.matchBreakdown);
  const matchTipLabel = matchTip.replace(/\n/g, ". ");
  const glyph = verdictGlyph(cell.verdict);
  const have = disk.state === "downloaded";
  const gate = have ? null : gateChrome(model.gated, authenticated);
  const sizeLabel = model.estimatedSize ? `≈${formatSize(model.estimatedSize)}` : null;

  // Item 2 (fix round 3): `paramsLabel`/`quantLabel` return the DASH glyph
  // ("—") for a value the Hub did not report, not null — a table CELL wants
  // that (an empty box reads as unfetched, not as "not applicable"), but this
  // line is built by joining known facts with a separator, and joining a
  // dash in produces a dangling "task · 27.4B · —" with nothing after it.
  // Filtered out here rather than in `paramsLabel`/`quantLabel` themselves,
  // which the drawer's own `<dd>` cells still call directly and still want
  // the dash for.
  // Item 9a (fix round 5), widened by fix round 6 item 2: a short format
  // token — see `formatToken`'s own docstring for why "gguf"/mlx/safetensors
  // alone left every embeddings row blank.
  const formatLabel = formatToken(model);
  // Item 9a: only worth a mention once there is more than one to count —
  // "1 variant" would be true of nearly every row and add noise, not signal.
  const variantsLabel =
    model.variantCount && model.variantCount > 1 ? `${model.variantCount} variants` : null;
  const metaParts = [
    paramsLabel(model.params),
    quantLabel(model.quant),
    sizeLabel,
    formatLabel,
    variantsLabel,
  ].filter((v): v is string => Boolean(v) && v !== "—");

  return (
    <div className="rowwrap" data-part="hit">
      <div
        className={`row hit rich${have ? " have" : ""}${model.fit?.verdict === "no" ? " unfit" : ""}`}
      >
        <span
          className={`match fit-${cell.verdict}`}
          data-match-tip={matchTip}
          data-verdict={cell.verdict}
          aria-label={matchTipLabel}
          tabIndex={0}
        >
          <span className="glyph">{glyph}</span>
          <span className="mbar">
            <i style={{ width: `${cell.scoreText === "—" ? 0 : cell.scoreText}%` }} />
          </span>
          {/* Item 12 (fix round 3): the bare number carried no label — a
           *  reader had no way to tell 84 was a score, out of what, or of
           *  what. The cell's own `data-tip` popover (round 5, above) already
           *  explains it on hover; this adds a caption that names it at a
           *  glance instead of only on hover. */}
          <span className="score">
            <b>{cell.scoreText}</b>
            <small>match</small>
          </span>
        </span>
        <div>
          <div className="row-name">
            {/* Item 14 (fix round 3): a real Hub repo id — link it to the
             *  model's page, as `origin/main`'s `RepoCard.tsx` did before
             *  this PR's port. `stopPropagation` because this heading sits
             *  in the same row as the (i) button that opens the drawer
             *  below — nothing on `.row.hit.rich` itself listens for a
             *  click today, but a link firing that toggle too would be a
             *  real regression, not a hypothetical one. */}
            <a
              className="mono-name"
              href={hubModelUrl(model.id)}
              target="_blank"
              rel="noreferrer"
              title="Open on huggingface.co"
              onClick={(e) => e.stopPropagation()}
            >
              {model.id}
            </a>
            {gate && (
              <span className="chip warn-chip" title={gate.title}>
                Gated
              </span>
            )}
          </div>
          {/* Item 9b (fix round 5): `relation` names WHAT this repo is
           *  relative to its base ("quantized", "finetune", "merge",
           *  "adapter" — free text off the Hub's own tag, see the field's
           *  doc comment in api.ts), so the line reads like the reason the
           *  repo exists rather than a bare cross-reference. Unrecognised
           *  or absent relation text falls back to the plain "from X" this
           *  line always said before. */}
          {model.baseModel && (
            <p className="row-note mono from">
              {model.relation === "quantized"
                ? "quantized from "
                : model.relation === "finetune"
                  ? "fine-tuned from "
                  : model.relation === "adapter"
                    ? "adapter for "
                    : model.relation === "merge"
                      ? "merge of "
                      : "from "}
              {model.baseModel}
            </p>
          )}
          {metaParts.length > 0 && (
            <p className="row-meta">
              {metaParts.map((part, i) => (
                <span key={i}>
                  {i > 0 && <span className="sep">·</span>}
                  {part}
                </span>
              ))}
            </p>
          )}
          {model.fit?.verdict === "no" && (
            <p className="row-meta reason">
              Will not fit — needs about {formatSize(model.fit.footprintBytes)} of memory.
            </p>
          )}
        </div>
        <span className="row-facts pop">
          <span title="Downloads in the last month">
            <DownloadIcon aria-label="downloads" /> {popLabel(model.downloads)}
          </span>
          <span title="Likes on the Hub">
            <HeartIcon aria-label="likes" /> {popLabel(model.likes)}
          </span>
          <span title="Last updated">
            <ClockIcon aria-label="updated" /> {ageLabel(model.updated ?? model.created)}
          </span>
        </span>
        <span className="row-act">
          {have ? (
            <span className="downloaded" title="Already on this Mac — use it from its capability">
              ✓{" "}
              {downloadedVariantLabel({
                variantCount: model.variantCount ?? null,
                variants: model.variants ?? null,
                file: model.file,
                quant: model.quant,
                localFile: model.local?.file ?? null,
              })}
            </span>
          ) : busy ? (
            <button type="button" className="btn" onClick={() => job && onCancel(job)}>
              Stop
            </button>
          ) : gate && !gate.canDownload ? (
            <a
              className="btn"
              href={hubModelUrl(model.id)}
              target="_blank"
              rel="noreferrer"
              title="Opens the model page on huggingface.co"
            >
              {gate.action}
            </a>
          ) : (
            <button type="button" className="btn" onClick={onDownload}>
              Download
            </button>
          )}
          <button type="button" className="iconbtn" title="Details" onClick={onToggleInfo}>
            ⓘ
          </button>
        </span>
      </div>
      {infoOpen && (
        <HitDrawer
          model={model}
          authenticated={authenticated}
          busy={busy}
          onDownloadFile={onDownloadFile}
          onSignedIn={onSignedIn}
          onClose={onToggleInfo}
        />
      )}
    </div>
  );
}

export function HubSearchScreen({
  capabilityKey,
  settled,
  cards,
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
  jobByModel: Map<string, Job>;
  pulling: (id: string) => boolean;
  /** Item A: `file` names one specific GGUF variant — absent for the
   *  row-level Download button, which keeps sending the exact request it
   *  always did. */
  onDownload: (id: string, capability: string, file?: string) => void;
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
  // Item 5 (fix round 6): facets computed server-side pre-narrowing, for the
  // Publisher/Quant dropdown option lists in `SearchControls`.
  const [facets, setFacets] = useState<HubSearchFacets | null>(null);
  const [authEpoch, setAuthEpoch] = useState(0);
  const [sizes, setSizes] = useState<ReadonlyMap<string, number | null> | null>(null);
  const [measuring, setMeasuring] = useState(false);
  const [openInfoId, setOpenInfoId] = useState<string | null>(null);
  // SPEC item 2: while this capability's on-device pool is still building (or
  // sitting out a 429 backoff), the server answers over the live Hub path and
  // reports that in `poolState`/`poolPagesDone` so the pane can say so rather
  // than silently look like the finished catalog.
  const [poolState, setPoolState] = useState<HubSearchResult["poolState"]>(undefined);
  const [poolPagesDone, setPoolPagesDone] = useState<number | null>(null);
  // The first-run build card's own state machine (`nextPoolPhase`): a pane
  // that opens already "ready" never enters "building", so it never sees the
  // "done" success beat either — only a build that finishes WHILE the pane
  // is open earns the celebration.
  const [poolPhase, setPoolPhase] = useState<"hidden" | "building" | "done">("hidden");
  // True for the ~250ms height/opacity collapse right before "done" clears
  // to "hidden" — kept separate from `poolPhase` so `nextPoolPhase` stays a
  // pure hidden/building/done machine with no timing baked into it.
  const [poolCardCollapsing, setPoolCardCollapsing] = useState(false);
  // Bumped to force a one-off refetch (see the poll effect below) without
  // otherwise touching `settled`/`limit`/`authEpoch`.
  const [pollEpoch, setPollEpoch] = useState(0);
  const debounce = useRef<number | null>(null);
  // Item 2 (fix round 4): the timer must merge into whatever `settled` is
  // CURRENT when it fires, not the value closed over when it was scheduled —
  // this effect's deps are `[liveQuery]` only (see below), so a plain
  // `settled` read inside the callback is a stale capture the moment a
  // filter/sort click updates `settled` within the 350ms window, reverting
  // that click when the timer runs. A ref updated every render (not inside
  // an effect — it must be current the instant the closure created THIS
  // render reads it) sidesteps that with no extra effect dependency.
  const settledRef = useRef(settled);
  settledRef.current = settled;

  useEffect(() => {
    if (debounce.current) window.clearTimeout(debounce.current);
    debounce.current = window.setTimeout(() => {
      onQuery(liveQuery);
      // Also item 2: opening the screen runs this effect once on mount with
      // `liveQuery === settled.q` (the initial value), and settling anyway
      // built a NEW `settled` object identity, which the fetch effect below
      // is keyed on — a duplicate `searchHubModels` request on every open,
      // for a query that never changed. Nothing to settle when the text
      // matches what is already settled.
      if (liveQuery === settledRef.current.q) return;
      setLimit(INITIAL_LIMIT);
      onSettle({ ...settledRef.current, q: liveQuery });
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
      // D843: the search screen is scoped by the CAPABILITY this pane was
      // opened from — `capabilityKey`, the same prop `CapabilityPane` reads,
      // not a Hub tag — so the server resolves it to every tag that
      // capability reaches (`embeddings` is three). The Task menu that used
      // to let a reader narrow independently is gone (item 2); `settled.task`
      // is kept only as the URL's `hubTask` mirror.
      capability: capabilityKey,
      sort: wireSort(settled.sort),
      limit,
      // Item 7 (fix round 5): the "Show models that will not fit" toggle is
      // gone — every row is always shown (the per-row red "Will not fit"
      // line is the only warning now). `searchHubModels` no longer sends
      // `includeUnfit`; the server defaults/ignores it and always behaves as
      // if it were true.
      fitLevel: settled.fitLevel,
      paramsBand: settled.paramsBand,
      quant: settled.quant || undefined,
      publisher: settled.publisher || undefined,
    }).then(
      (data: HubSearchResult) => {
        if (!alive) return;
        setLoading(false);
        setError(data.error ?? null);
        setModels(data.models);
        setEndpoint(data.endpoint ?? null);
        setAuthenticated(!!data.authenticated);
        setFacets(data.facets ?? null);
        setPoolState(data.poolState);
        setPoolPagesDone(data.poolPagesDone ?? null);
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
    // `pollEpoch` is intentionally in the deps: it is bumped ONLY by the
    // poll-while-building effect below (every 15s while `poolState ===
    // "building"`, and once more on the transition out of it), so it never
    // fires on its own outside that loop.
  }, [settled, limit, authEpoch, pollEpoch]);

  // SPEC item 2: re-poll while the pool is building so the banner's page
  // count moves and the pane picks up the finished catalog the moment it is
  // ready, without the reader doing anything. `blockedUntil` isn't part of
  // the response today, so the copy for that state just says "shortly" via
  // `poolBuildBanner`'s own fallback.
  useEffect(() => {
    if (poolState !== "building") return;
    const id = window.setInterval(() => setPollEpoch((e) => e + 1), 15_000);
    return () => window.clearInterval(id);
  }, [poolState]);

  // Drives the build card's phase off `poolState`. `nextPoolPhase` is pure —
  // it decides WHETHER to move, this effect just applies it whenever
  // `poolState` changes.
  useEffect(() => {
    setPoolPhase((phase) => nextPoolPhase(phase, poolState));
  }, [poolState]);

  // The success beat ("Catalog ready…") holds for ~2.5s, then the card
  // collapses (height + opacity, `.pool-build-card-collapsing`) for ~250ms
  // before finally unmounting. Only "done" schedules this — "building" and
  // "hidden" have nothing to collapse.
  useEffect(() => {
    if (poolPhase !== "done") {
      setPoolCardCollapsing(false);
      return;
    }
    const holdId = window.setTimeout(() => setPoolCardCollapsing(true), 2_500);
    return () => window.clearTimeout(holdId);
  }, [poolPhase]);

  useEffect(() => {
    if (!poolCardCollapsing) return;
    const id = window.setTimeout(() => setPoolPhase("hidden"), 250);
    return () => window.clearTimeout(id);
  }, [poolCardCollapsing]);

  useEffect(() => {
    if (!sortsOnPage(settled.sort) || !models || models.length === 0) {
      setSizes(null);
      setMeasuring(false);
      return;
    }
    const unmeasured = models.filter((m) => !m.estimatedSize);
    // A row with no `file` never needs a lookup at all (see `measureSizes`);
    // one with a `file` is "known" only once THAT (id, file) key has resolved
    // — `knownTotalSize(id)` (implicitly `file: null`) would ask the wrong
    // question for it.
    const known = (m: Pick<HubModel, "id" | "file">) => (m.file ? knownTotalSize(m.id, m.file) : null);
    if (unmeasured.every((m) => !m.file || known(m) !== undefined)) {
      setSizes(new Map(unmeasured.map((m) => [m.id, known(m) ?? null])));
      setMeasuring(false);
      return;
    }
    let alive = true;
    setSizes(null);
    setMeasuring(true);
    measureSizes(unmeasured, () => alive).then((got) => {
      if (!alive) return;
      setMeasuring(false);
      setSizes(got);
    });
    return () => {
      alive = false;
    };
  }, [models, settled.sort]);

  const host = (endpoint || "https://huggingface.co").replace(/^https?:\/\//, "");
  const shown =
    models && sortsOnPage(settled.sort) && sizes
      ? bySizeAscending(models, (m) => hubSizeBytes(m, sizes.get(m.id)))
      : models;
  return (
    <div className="tp-pane" data-part="adv">
      <button type="button" className="adv-back" data-adv-back="1" onClick={onBack}>
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
          className="btn btn-primary btn-lg"
          onClick={() => {
            setLimit(INITIAL_LIMIT);
            onSettle({ ...settled, q: liveQuery });
          }}
        >
          Search
        </button>
      </div>
      <SearchControls
        sort={settled.sort}
        fitLevel={settled.fitLevel}
        paramsBand={settled.paramsBand}
        quant={settled.quant}
        publisher={settled.publisher}
        onSort={(sort) => onSettle({ ...settled, sort })}
        onFitLevel={(fitLevel) => onSettle({ ...settled, fitLevel })}
        onParamsBand={(paramsBand) => onSettle({ ...settled, paramsBand })}
        onQuant={(quant) => onSettle({ ...settled, quant })}
        onPublisher={(publisher) => onSettle({ ...settled, publisher })}
        loading={loading && models === null}
        matchCount={shown?.length ?? null}
        facets={facets}
      />
      {/* Item 12c (fix round 3): the legend explained the ranking but never
       *  named the number itself — now opens by naming it, same as the
       *  cell's own `data-tip` popover (round 5) already does on hover. */}
      <p className="sorthint">
        Match score (0–100) is ranked for this Mac: memory fit first, then speed, freshness and popularity; models
        already here get a small bonus. Bar colour is the memory verdict: ● fits comfortably ▲ fits tightly ■ will
        not fit ? not measured yet.
      </p>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!error && poolState === "blocked" && (
        <p className="pool-build-banner pool-build-banner-blocked">
          {poolBuildBanner(poolState, poolPagesDone, null, Date.now())}
        </p>
      )}
      {!error && poolPhase === "building" && (
        <div className="pool-build-card" role="status">
          <div className="pool-build-head">
            <span className="pool-build-title">Building this Mac&rsquo;s catalog of {meta.searchNoun}</span>
          </div>
          <p className="pool-build-sub">
            Live Hub results are shown meanwhile — once the catalog is built, searches run on this Mac with no Hub
            requests.
          </p>
          <div className="pool-build-progress">
            <div className="pool-build-bar" aria-hidden="true">
              <div className="pool-build-bar-sweep" />
            </div>
            <span className="pool-build-count" key={poolPagesDone ?? 0}>
              {pagesFetchedLabel(poolPagesDone)}
            </span>
          </div>
        </div>
      )}
      {!error && poolPhase === "done" && (
        <div
          className={
            poolCardCollapsing ? "pool-build-card pool-build-card-done pool-build-card-collapsing" : "pool-build-card pool-build-card-done"
          }
          role="status"
        >
          <span className="pool-build-check" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="15" height="15">
              <path className="pool-build-check-path" d="M4 12.5 9.5 18 20 6" fill="none" strokeWidth="2.5" />
            </svg>
          </span>
          <span className="pool-build-title">Catalog ready. Searches now run on this Mac.</span>
        </div>
      )}
      {loading && models === null && <p className="cc-empty cc-empty-loading">Asking {host}</p>}
      {models !== null && models.length === 0 && !error && (
        <p className="cc-empty">
          {/* Item 3 (fix round 4): `settled.q` is often empty here — a task
           *  filter with no typed query is the default state reached from a
           *  pane's "Search Hugging Face for more…" door — and quoting an
           *  empty string read as literal `matches ""`, a search nobody
           *  typed claiming to have run. */}
          {settled.q.trim()
            ? `Nothing on ${host} matches "${settled.q}" — among the ${meta.searchNoun} this Mac can run.`
            : `No ${meta.searchNoun} the Hub knows about will run here.`}
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
              infoOpen={openInfoId === m.id}
              job={jobByModel.get(m.id)}
              onDownload={() => onDownload(m.id, m.capability)}
              onDownloadFile={(file) => onDownload(m.id, m.capability, file)}
              onCancel={onCancel}
              onToggleInfo={() => setOpenInfoId((cur) => (cur === m.id ? null : m.id))}
              onSignedIn={() => setAuthEpoch((n) => n + 1)}
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
