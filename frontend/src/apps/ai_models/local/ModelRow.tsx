// The shared model row — one skeleton for both species a capability pane
// shows: a model already on this Mac, and one the catalog only recommends.
// Ported verbatim from the approved mockup's `row()`/`drawer()` functions.
//
// The only thing that distinguishes the two species on the face is the tick:
// a model we suggested (curated) carries it, one the user fetched themselves
// does not. Everything else — chips, facts, actions, the disclosure drawer —
// is driven by the caller's already-merged view model (`ModelRowModel`),
// which is deliberately NOT `AiModelRepo` or `AiCatalogModel`: the mockup's
// `row(m)` took one flat object for a disk row and a recommendation alike,
// and reproducing that here means the caller (`CapabilityPane`) does the one
// merge, instead of this file re-deriving "what counts as this row's size"
// twice.
import type { ReactNode } from "react";

import type { AiFitVerdict } from "@platform/lib/api";
import { navigateUrl } from "@platform/lib/router";

import { hubModelUrl } from "@apps/ai_models/local/hub";
import { tabHref } from "@apps/ai_models/routes";

/** D813 (item P): the pre-port curated mark, restored verbatim from
 *  `origin/main`'s `RepoCard.tsx` after the user asked for "the older green
 *  tick thing we were using before this PR" — the two-pane port had replaced
 *  it with a plain `<span className="tick">✔</span>` in `--success-bright`
 *  (the branch's own "loaded" green), which is a different claim in a
 *  different hue from what curation used to mean here. Filled rather than
 *  stroked: at 14px a hairline check is a smudge, and a solid accent mark is
 *  the one thing on the row that pops without competing with a "Loaded"
 *  badge (filled green — a genuinely different claim). Focusable and hinted
 *  rather than `title`d on purpose in the original; kept as `title` here
 *  since `ModelRow.tsx`'s other inline marks use `title`, and this file has
 *  no `data-hint` hover-card mechanism of its own to reuse. */
function CuratedMark() {
  return (
    <span className="am-card-pick" tabIndex={0} aria-label="Curated by Fused" title="Curated by Fused">
      <svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
        {/* The seal. Lucide's `badge-check` outline, filled instead of stroked. */}
        <path
          d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"
          fill="currentColor"
        />
        {/* …and the check knocked out of it in the row's own ground, so the
            mark reads as one solid object rather than two overlapping ones. */}
        <path
          d="m9 12 2 2 4-4"
          fill="none"
          stroke="var(--bg-alt)"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

export interface ModelRowModel {
  id: string;
  /** Nickname, or the model half of the repo id — never the bare repo id. */
  name: string;
  /** True for a row the catalog suggested (carries the tick and a note). */
  curated: boolean;
  /** "Our pick" chip. */
  ourPick: boolean;
  /** A warning chip's text (e.g. "needs 32GB"), or null for none. */
  warnChip: string | null;
  /** D814 (item Q): this Mac's fit verdict for the row, or null when the
   *  catalog has no fit data for it (a repo the curation never heard of).
   *  Drives the fit chip in the row header — distinct from `warnChip`, which
   *  is reused for the "partially downloaded" state and no longer carries
   *  the fit-verdict text. */
  fit: AiFitVerdict["verdict"] | null;
  have: boolean;
  /** Formatted size, or "size not checked yet" when nothing is known. */
  sizeLabel: string;
  /** "17d ago", or null when never used / not on this Mac. */
  usedLabel: string | null;
  /** "MLX", or null when not recorded. */
  engine: string | null;
  params: string | null;
  quant: string | null;
  /** Always "safetensors" today — a field rather than a literal because a
   *  second format is a matter of when, not if. */
  format: string;
  /** File count on disk, for the drawer's "On disk" line. Null when `!have`. */
  fileCount: number | null;
  /** Cache path, for the drawer's "Path" line. Null when `!have`. */
  path: string | null;
}

export interface ModelRowProgress {
  doneLabel: string;
  totalLabel: string;
  etaLabel: string;
  /** 0…1 */
  fraction: number;
}

export interface ModelRowOptions {
  /** This is the most-recently-used model on this Mac for the pane's
   *  capability — carries the "Last used" chip. */
  last?: boolean;
  /** A download job is in flight for this row right now. */
  downloading?: boolean;
  progress?: ModelRowProgress;
  /** No engine on this device can run it — Try is disabled and there is no
   *  Download (a no-engine row is never a recommendation). */
  noEngine?: boolean;
  /** Give the Download button the primary treatment (the first suggestion
   *  in a group). */
  primary?: boolean;
  /** The disclosure drawer is open. */
  info?: boolean;
}

export interface ModelRowHandlers {
  onTry?: (id: string) => void;
  onDownload?: (id: string) => void;
  onStop?: (id: string) => void;
  onDelete?: (id: string) => void;
  onToggleInfo?: (id: string) => void;
  onShowInFinder?: (id: string) => void;
}

function Unknown() {
  return <span className="unknown">not recorded</span>;
}

function fact(value: string | null): ReactNode {
  return value ? value : <Unknown />;
}

/** D817 (item T): the drawer's "Why we suggest it" sentence, generated only
 *  from data the row already carries — never the catalog's hand-written
 *  `note` (`fused_render/ai/catalog.py`), which names a fact about ONE
 *  machine (a RAM figure, "this swaps rather than runs") baked into prose
 *  that then read as universal on every other Mac the app runs on. The
 *  catalog note itself is untouched — the Playground still reads it — this
 *  is only the two-pane drawer switching to its own, always-true sentence. */
function suggestionSentence(model: ModelRowModel, paneLabel: string): string {
  const lead = model.ourPick ? `Our pick for ${paneLabel}` : `One of our suggestions for ${paneLabel}`;
  const clauses: string[] = [lead];
  if (model.fit === "easy") clauses.push("fits comfortably in this Mac's memory");
  else if (model.fit === "tight") clauses.push("fits, but tightly");
  else if (model.fit === "no" && model.warnChip) {
    const footprint = model.warnChip.replace(/^Needs /, "");
    clauses.push(`needs ${footprint} — more than this Mac has`);
  }
  if (model.engine) clauses.push(`runs on ${model.engine}`);
  return clauses.join(" · ");
}

function Drawer({
  model,
  paneLabel,
  handlers,
}: {
  model: ModelRowModel;
  paneLabel: string;
  handlers: ModelRowHandlers;
}) {
  return (
    <div className="drawer" data-part="row.drawer">
      <dl>
        <dt>Repository</dt>
        <dd>{model.id}</dd>
        <dt>Engine</dt>
        <dd className="plain">{fact(model.engine)}</dd>
        <dt>Parameters</dt>
        <dd className="plain">{fact(model.params)}</dd>
        <dt>Quantization</dt>
        <dd className="plain">{fact(model.quant)}</dd>
        <dt>Format</dt>
        <dd className="plain">{model.format}</dd>
        <dt>On disk</dt>
        <dd className="plain">
          {model.sizeLabel}
          {model.have ? ` · ${model.fileCount ?? 0} files` : " (not downloaded)"}
        </dd>
        {model.have && (
          <>
            <dt>Last used</dt>
            <dd className="plain">{model.usedLabel || "never"}</dd>
            <dt>Path</dt>
            <dd>{model.path || <Unknown />}</dd>
          </>
        )}
      </dl>
      {model.curated ? (
        <p className="why">
          <b>Why we suggest it.</b> {suggestionSentence(model, paneLabel)}
        </p>
      ) : (
        <p className="why">
          Not one of our suggestions — you downloaded this one, so everything above is read off the
          cache directory.
        </p>
      )}
      <div className="acts">
        <a className="btn-link" href={hubModelUrl(model.id)} target="_blank" rel="noreferrer">
          View on Hugging Face ↗
        </a>
        {model.have && (
          <button type="button" className="btn-link" onClick={() => handlers.onShowInFinder?.(model.id)}>
            Show in Finder
          </button>
        )}
        <button type="button" className="btn-link" onClick={() => handlers.onToggleInfo?.(model.id)}>
          Close
        </button>
      </div>
    </div>
  );
}

function Actions({
  model,
  opts,
  handlers,
}: {
  model: ModelRowModel;
  opts: ModelRowOptions;
  handlers: ModelRowHandlers;
}) {
  if (opts.downloading) {
    return (
      <button type="button" className="btn" onClick={() => handlers.onStop?.(model.id)}>
        Stop
      </button>
    );
  }
  if (model.have && opts.noEngine) {
    return (
      <>
        <button type="button" className="btn" disabled title="No engine on this device can run it">
          Try
        </button>
        <button
          type="button"
          className="iconbtn"
          title="Details"
          data-info={model.id}
          onClick={() => handlers.onToggleInfo?.(model.id)}
        >
          ⓘ
        </button>
        <button type="button" className="iconbtn" title="Delete" onClick={() => handlers.onDelete?.(model.id)}>
          🗑
        </button>
      </>
    );
  }
  if (model.have) {
    // Item I: a real link to the Playground route, carrying `?model=` — so
    // middle-click and copy-link reach it the way `AiModelsPage`'s own tab
    // strip links do (see that file's own doc on this exact pattern). The
    // left-click still fires `onTry` (which starts the actual load) before
    // handing off to client-side navigation, same as a plain button did.
    const tryHref = tabHref("playground", `?model=${encodeURIComponent(model.id)}`);
    return (
      <>
        <a
          className="btn btn-primary"
          href={tryHref}
          onClick={(e) => {
            if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            e.preventDefault();
            handlers.onTry?.(model.id);
            navigateUrl(tryHref);
          }}
        >
          Try
        </a>
        <button
          type="button"
          className="iconbtn"
          title="Details"
          data-info={model.id}
          onClick={() => handlers.onToggleInfo?.(model.id)}
        >
          ⓘ
        </button>
        <button type="button" className="iconbtn" title="Delete" onClick={() => handlers.onDelete?.(model.id)}>
          🗑
        </button>
      </>
    );
  }
  return (
    <>
      <button
        type="button"
        className={`btn${opts.primary ? " btn-primary" : ""}`}
        onClick={() => handlers.onDownload?.(model.id)}
      >
        Download
      </button>
      <button
        type="button"
        className="iconbtn"
        title="Details"
        data-info={model.id}
        onClick={() => handlers.onToggleInfo?.(model.id)}
      >
        ⓘ
      </button>
    </>
  );
}

export function ModelRow({
  model,
  paneLabel = "",
  opts = {},
  handlers = {},
}: {
  model: ModelRowModel;
  /** The pane's own title, lowercased (e.g. "chat & writing") — the caller
   *  (`CapabilityPane`) already computes this via `capabilityLabel` for its
   *  own heading; threaded through here only for the drawer's generated
   *  sentence (item T), so it defaults to "" for the handful of call sites
   *  (a no-engine pane's `have` list) that never open a curated drawer. */
  paneLabel?: string;
  opts?: ModelRowOptions;
  handlers?: ModelRowHandlers;
}) {
  const chips: ReactNode[] = [];
  if (opts.last) chips.push(
    <span key="last" className="chip last-chip">
      Last used
    </span>,
  );
  if (model.ourPick) chips.push(
    <span key="rec" className="chip rec-chip">
      Our pick
    </span>,
  );
  // D814 (item Q): a fit chip for every row that carries a verdict — absent
  // for a repo the catalog has no fit data for (no "?" on the pane; that
  // glyph is reserved for search hits per the mockup). `warnChip` still
  // covers the partial-download case (PARTIAL_TAG) but no longer duplicates
  // the "needs N GB" text now that the fit chip owns it.
  if (model.fit === "easy") {
    chips.push(
      <span key="fit" className="chip fit-chip fit-easy" title="Fits comfortably in this Mac's memory">
        ● Fits
      </span>,
    );
  } else if (model.fit === "tight") {
    chips.push(
      <span key="fit" className="chip fit-chip fit-tight" title="Fits, but tightly">
        ▲ Tight fit
      </span>,
    );
  } else if (model.fit === "no") {
    chips.push(
      <span key="fit" className="chip fit-chip fit-no" title="Will not fit in this Mac's memory">
        ■ {model.warnChip}
      </span>,
    );
  } else if (model.warnChip) {
    chips.push(
      <span key="warn" className="chip warn-chip">
        {model.warnChip}
      </span>,
    );
  }

  return (
    <div className={`rowwrap${opts.info ? " open" : ""}`}>
      <div className={`row ${model.have ? "have" : ""}`} data-part="row">
        <div>
          <div className="row-name">
            <b>{model.name}</b>
            {model.curated && <CuratedMark />}
            {chips}
          </div>
          <p className="row-note mono">{model.id}</p>
          {opts.downloading && opts.progress && (
            <>
              <div className="bar-prog">
                <i style={{ width: `${Math.round(opts.progress.fraction * 100)}%` }} />
              </div>
              <p className="row-note mono" style={{ marginTop: 5 }}>
                {opts.progress.doneLabel} of {opts.progress.totalLabel} · about {opts.progress.etaLabel} left
              </p>
            </>
          )}
        </div>
        <span className="row-facts">
          {opts.downloading ? "downloading" : [model.sizeLabel, model.usedLabel ? `used ${model.usedLabel}` : null].filter(Boolean).join(" · ")}
        </span>
        <span className="row-act">
          <Actions model={model} opts={opts} handlers={handlers} />
        </span>
      </div>
      {opts.info && <Drawer model={model} paneLabel={paneLabel} handlers={handlers} />}
    </div>
  );
}
